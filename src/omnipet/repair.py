from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode

from omnipet.approvals import ApprovalError, load_approvals
from omnipet.guides import clear_generation_guides
from omnipet.run import EXPECTED_DEPENDENCIES, EXPECTED_JOB_IDS, STANDARD_JOB_IDS
from omnipet.security import contains_credential_like_text


class RepairError(RuntimeError):
    """Raised when a completed visual job cannot be repaired safely."""


@dataclass(frozen=True)
class Invalidation:
    jobs: tuple[str, ...]
    stages: tuple[str, ...]
    retained_approvals: tuple[str, ...]


@dataclass(frozen=True)
class RepairResult:
    archive_path: str
    repaired_job: str
    invalidated_jobs: tuple[str, ...]
    invalidated_stages: tuple[str, ...]


def _transitive_dependents(job_id: str) -> tuple[str, ...]:
    affected = {job_id}
    changed = True
    while changed:
        changed = False
        for candidate, dependencies in EXPECTED_DEPENDENCIES.items():
            if candidate not in affected and affected.intersection(dependencies):
                affected.add(candidate)
                changed = True
    return tuple(value for value in EXPECTED_JOB_IDS if value in affected and value != job_id)


INVALIDATION_GRAPH = {
    "base": Invalidation(
        _transitive_dependents("base"),
        ("base", "standard-rows", "directions", "package", "delivery"),
        (),
    ),
    **{
        job_id: Invalidation(
            _transitive_dependents(job_id),
            ("standard-rows", "directions", "package", "delivery"),
            ("base",),
        )
        for job_id in STANDARD_JOB_IDS
    },
    "look-cardinals": Invalidation(
        ("look-row-9", "look-row-10"),
        ("directions", "package", "delivery"),
        ("base", "standard-rows"),
    ),
    "look-row-9": Invalidation(
        ("look-row-10",),
        ("directions", "package", "delivery"),
        ("base", "standard-rows"),
    ),
    "look-row-10": Invalidation(
        (),
        ("directions", "package", "delivery"),
        ("base", "standard-rows"),
    ),
}

_GENERATED_KEYS = {
    "source_path", "completed_at", "derived_from", "mirror_decision",
    "repair_source_paths", "adoption_decision",
}
_PACKAGE_PATHS = (
    "final/pet.json",
    "final/package-source.png",
    "final/spritesheet-extended.webp",
    "qa/package-generated",
    "qa/package-reviewed",
)
_RECOVERY_JOURNALS = (
    "package-publication.json",
    "qa/package-review-publication.json",
)
_REPAIR_JOURNAL = "repair-publication.json"
_JOURNAL_KEYS = {
    "schema_version", "state", "pet_id", "job_id", "archive", "staging",
    "moves", "snapshots", "plan_sha256",
}


def repair_completed_job(project: Any, job_id: str, *, reason: str) -> RepairResult:
    from omnipet.release import _hatch_lock

    with _hatch_lock(project):
        return _repair_locked(project, job_id, reason)


def _repair_locked(project: Any, job_id: str, reason: str) -> RepairResult:
    reason = _validated_reason(reason)
    run_dir = project.repository_root / ".omnipet" / "runs" / project.pet_id
    try:
        _validate_directory(run_dir)
        recovery = _recover_repair(project, run_dir)
        if (
            isinstance(recovery, RepairResult)
            and recovery.repaired_job == job_id
        ):
            return recovery
        if recovery == "rolled-back":
            from omnipet.project import load_pet_project

            selector = "." if project.root == project.repository_root else project.pet_id
            project = load_pet_project(project.repository_root, selector)
        if any((run_dir / relative).exists() for relative in _RECOVERY_JOURNALS):
            raise ValueError("publication recovery is pending")
        spec = INVALIDATION_GRAPH.get(job_id)
        if spec is None:
            raise ValueError("repair job is invalid")
        manifest_path = _safe_file(run_dir, "imagegen-jobs.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        jobs = _validated_jobs(manifest)
        selected = jobs[job_id]
        if selected.get("status") not in {"complete", "failed"}:
            raise ValueError("job is not repairable")
        approvals_path = run_dir / "qa" / "approvals.json"
        workflow_path = run_dir / "workflow.json"
        project_manifest_path = project.root / "pet.yaml"
        state_paths = [manifest_path, approvals_path, workflow_path]
        if job_id == "base":
            state_paths.append(project_manifest_path)
        snapshots = {
            path: path.read_bytes() if path.is_file() and not path.is_symlink() else None
            for path in state_paths
        }
        project_manifest_after = (
            _without_approved_canonical(project_manifest_path)
            if job_id == "base"
            else None
        )
        approvals = _truncated_approvals(run_dir, spec.retained_approvals)
        timestamp = datetime.now(timezone.utc)
        stamp = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
        archives_root = project.repository_root / ".omnipet" / "archives" / "repairs"
        _prepare_archive_root(project.repository_root, archives_root)
        archive = archives_root / f"{project.pet_id}-{job_id}-{stamp}"
        moves = _repair_paths(project, run_dir, jobs, job_id, spec)
        staging = Path(tempfile.mkdtemp(prefix=f".{project.pet_id}-{job_id}-", dir=archives_root))
    except RepairError:
        raise
    except (
        ApprovalError, OSError, UnicodeError, json.JSONDecodeError, TypeError,
        ValueError,
    ):
        raise RepairError("repair preflight failed") from None

    journal_path = run_dir / _REPAIR_JOURNAL
    journal_published = False
    try:
        state_root = staging / "state-before"
        snapshot_records = []
        for path, content in snapshots.items():
            relative_path = path.relative_to(project.repository_root)
            backup = None
            if content is not None:
                relative = (
                    path.relative_to(run_dir)
                    if path.is_relative_to(run_dir)
                    else Path("project") / path.relative_to(project.root)
                )
                destination = state_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                _write_new_file(destination, content)
                backup = str(destination.relative_to(staging))
            snapshot_records.append({
                "path": str(relative_path),
                "backup": backup,
                "existed": content is not None,
                "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
            })

        move_records = []
        for source, relative in moves:
            destination = staging / "artifacts" / relative
            node, digest = _node_digest(source)
            move_records.append({
                "source": str(source.relative_to(project.repository_root)),
                "destination": str(destination.relative_to(staging)),
                "node": node,
                "sha256": digest,
            })
        plan_sha256 = _plan_sha256(move_records, snapshot_records)
        journal = {
            "schema_version": 1,
            "state": "prepared",
            "pet_id": project.pet_id,
            "job_id": job_id,
            "archive": str(archive.relative_to(project.repository_root)),
            "staging": str(staging.relative_to(project.repository_root)),
            "moves": move_records,
            "snapshots": snapshot_records,
            "plan_sha256": plan_sha256,
        }
        _validate_repair_journal(project, run_dir, journal)
        _write_json_atomic(journal_path, journal)
        journal_published = True

        if (
            project_manifest_after is not None
            and project_manifest_after != snapshots[project_manifest_path]
        ):
            _write_bytes_atomic(project_manifest_path, project_manifest_after)
        for source, relative in moves:
            destination = staging / "artifacts" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            _fsync_directory(source.parent)
            _fsync_directory(destination.parent)

        repaired_ids = {job_id, *spec.jobs}
        for current_id in repaired_ids:
            _reset_job(jobs[current_id])
        repaired = jobs[job_id]
        metadata = (
            repaired["metadata"]
            if isinstance(repaired.get("metadata"), dict)
            else {}
        )
        metadata["repair"] = {
            "reason": reason,
            "recorded_at": timestamp.isoformat(),
            "archive": str(archive.relative_to(project.repository_root)),
        }
        repaired["metadata"] = metadata
        _write_json_atomic(manifest_path, manifest)
        _write_json_atomic(approvals_path, approvals)
        workflow = {
            "schema_version": 1,
            "state": (
                "preparing"
                if job_id == "base"
                else "generating_standard_rows"
                if job_id in STANDARD_JOB_IDS
                else "generating_directions"
            ),
            "blocked": None,
        }
        _write_json_atomic(workflow_path, workflow)
        _write_json_atomic(staging / "repair.json", {
            "schema_version": 1,
            "pet_id": project.pet_id,
            "repaired_job": job_id,
            "reason": reason,
            "recorded_at": timestamp.isoformat(),
            "invalidated_jobs": list(spec.jobs),
            "invalidated_stages": list(spec.stages),
        })
        os.replace(staging, archive)
        _fsync_directory(archive.parent)
        journal_path.unlink()
        _fsync_directory(run_dir)
        clear_generation_guides(run_dir, job_id)
        return RepairResult(
            archive_path=str(archive.relative_to(project.repository_root)),
            repaired_job=job_id,
            invalidated_jobs=spec.jobs,
            invalidated_stages=spec.stages,
        )
    except BaseException as error:
        try:
            if archive.is_dir() and not staging.exists():
                _validate_artifact_tree(archive)
                if journal_path.exists():
                    _recover_repair(project, run_dir)
                return RepairResult(
                    archive_path=str(archive.relative_to(project.repository_root)),
                    repaired_job=job_id,
                    invalidated_jobs=spec.jobs,
                    invalidated_stages=spec.stages,
                )
            if journal_published:
                recovery = _recover_repair(project, run_dir)
                if isinstance(recovery, RepairResult):
                    return recovery
            else:
                shutil.rmtree(staging, ignore_errors=True)
        except RepairError:
            raise RepairError("repair recovery is required") from None
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise RepairError("repair transaction failed") from None


def _validated_reason(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 240
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or contains_credential_like_text(value)
    ):
        raise RepairError("repair reason is invalid")
    return value


def _validated_jobs(manifest: Any) -> dict[str, dict[str, Any]]:
    values = manifest.get("jobs") if isinstance(manifest, dict) else None
    if (
        not isinstance(values, list)
        or tuple(item.get("id") for item in values if isinstance(item, dict))
        != EXPECTED_JOB_IDS
    ):
        raise ValueError("repair manifest is invalid")
    jobs = {item["id"]: item for item in values}
    for job_id, job in jobs.items():
        if (
            job.get("status") not in {"pending", "running", "complete", "failed"}
            or tuple(job.get("depends_on", ())) != EXPECTED_DEPENDENCIES[job_id]
        ):
            raise ValueError("repair graph is invalid")
        if job["status"] in {"running", "complete", "failed"} and any(
            jobs[dependency].get("status") != "complete"
            for dependency in EXPECTED_DEPENDENCIES[job_id]
        ):
            raise ValueError("repair graph is inconsistent")
    return jobs


def _reset_job(job: dict[str, Any]) -> None:
    job["status"] = "pending"
    for key in _GENERATED_KEYS:
        job.pop(key, None)
    metadata = job.get("metadata")
    if isinstance(metadata, dict):
        for key in (
            "attempts", "started_at", "sha256", "format", "last_error",
            "diagnostic", "provider",
        ):
            metadata.pop(key, None)
        if not metadata:
            job.pop("metadata", None)
    else:
        job.pop("metadata", None)


def _repair_paths(
    project: Any,
    run_dir: Path,
    jobs: dict[str, dict[str, Any]],
    job_id: str,
    spec: Invalidation,
) -> tuple[tuple[Path, Path], ...]:
    relative_paths: set[Path] = set()
    for current_id in (job_id, *spec.jobs):
        relative_paths.update(_job_artifacts(run_dir, jobs[current_id], current_id))
    if "standard-rows" in spec.stages:
        relative_paths.add(Path("qa/standard"))
    relative_paths.update(Path(value) for value in _PACKAGE_PATHS)
    relative_paths.add(Path("package-complete.json"))
    candidates: list[tuple[Path, Path]] = []
    for relative in sorted(relative_paths, key=lambda path: (len(path.parts), path.as_posix())):
        path = run_dir / relative
        if not path.exists() and not path.is_symlink():
            continue
        _validate_move_source(run_dir, path)
        candidates.append((path, Path("run") / relative))
    for path in (project.manifest_path, project.spritesheet_path):
        if not path.exists() and not path.is_symlink():
            continue
        _validate_move_source(project.repository_root, path)
        candidates.append(
            (path, Path("delivery") / path.relative_to(project.repository_root))
        )
    if job_id == "base" and project.canonical_base_path is not None:
        path = project.canonical_base_path
        _validate_move_source(project.root, path)
        candidates.append((path, Path("project") / path.relative_to(project.root)))
    return _without_nested_sources(candidates)


def _job_artifacts(run_dir: Path, job: dict[str, Any], job_id: str) -> set[Path]:
    values = {
        Path(f"generated-sources/{job_id}.png"),
        Path(f"decoded/{job_id}.png"),
        Path(f"qa/visual-jobs/{job_id}.result.json"),
    }
    for key in ("source_path", "output_path"):
        value = job.get(key)
        if isinstance(value, str):
            path = Path(value)
            if path.is_absolute():
                path = path.relative_to(run_dir)
            if path.parts and ".." not in path.parts:
                values.add(path)
    if job_id in STANDARD_JOB_IDS:
        values.update({Path(f"qa/rows/{job_id}"), Path(f"previews/{job_id}.gif")})
    elif job_id == "base":
        values.update({
            Path("references/canonical-base.png"),
            Path("qa/base"),
            Path("qa/candidates/base.json"),
        })
    elif job_id == "look-cardinals":
        values.update({
            Path("decoded/look-cardinals-approved.png"),
            Path("decoded/look-anchors"),
            Path("qa/directions/cardinals"),
        })
    elif job_id == "look-row-9":
        values.update(
            path.relative_to(run_dir)
            for path in (run_dir / "qa/directions").glob("look-row-9*")
        )
    elif job_id == "look-row-10":
        values.update({
            Path("qa/directions/look-row-10-registration.json"),
            Path("qa/directions/direction-semantics.json"),
            Path("qa/directions/continuity.json"),
            Path("qa/directions/contact-sheet.png"),
            Path("final/spritesheet-extended.png"),
        })
    return values


def _without_nested_sources(
    candidates: list[tuple[Path, Path]],
) -> tuple[tuple[Path, Path], ...]:
    selected: list[tuple[Path, Path]] = []
    for source, relative in sorted(candidates, key=lambda item: len(item[0].parts)):
        if any(source.is_relative_to(parent) for parent, _ in selected):
            continue
        selected.append((source, relative))
    return tuple(selected)


def _truncated_approvals(run_dir: Path, retained: tuple[str, ...]) -> dict[str, Any]:
    records = load_approvals(run_dir)
    keep = min(len(records), len(retained))
    if tuple(record.stage for record in records[:keep]) != retained[:keep]:
        raise ValueError("approval prefix is invalid")
    values = []
    for record in records[:keep]:
        value = {
            "stage": record.stage,
            "artifacts": [
                {"path": artifact.path, "sha256": artifact.sha256}
                for artifact in record.artifacts
            ],
            "approved_at": record.approved_at,
        }
        if record.note is not None:
            value["note"] = record.note
        values.append(value)
    return {"schema_version": 1, "approvals": values}


def _without_approved_canonical(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("project manifest is unsafe")
    text = path.read_text(encoding="utf-8")
    document = yaml.compose(text)
    if not isinstance(document, MappingNode):
        raise ValueError("project manifest is invalid")
    approved_pair = next((
        (key, value)
        for key, value in document.value
        if isinstance(key, ScalarNode) and key.value == "approved"
    ), None)
    if approved_pair is None:
        return text.encode()
    key, value = approved_pair
    if not isinstance(value, MappingNode) or not any(
        isinstance(child_key, ScalarNode) and child_key.value == "canonical_base"
        for child_key, _child_value in value.value
    ):
        return text.encode()
    start, end = key.start_mark.index, value.end_mark.index
    if end < len(text) and text[end] == "\n":
        end += 1
    result = text[:start] + text[end:]
    loaded = yaml.safe_load(result)
    if not isinstance(loaded, dict) or "approved" in loaded:
        raise ValueError("project manifest repair is invalid")
    return result.encode()


def _recover_repair(
    project: Any, run_dir: Path
) -> RepairResult | str | None:
    journal_path = run_dir / _REPAIR_JOURNAL
    if not journal_path.exists():
        return None
    try:
        if journal_path.is_symlink() or not journal_path.is_file():
            raise ValueError("repair journal is unsafe")
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        staging, archive = _validate_repair_journal(project, run_dir, journal)
        if archive.exists():
            if staging.exists() or archive.is_symlink() or not archive.is_dir():
                raise ValueError("repair publication is inconsistent")
            _validate_artifact_tree(archive)
            _validate_prepared_staging(project, run_dir, archive, journal)
            journal_path.unlink()
            _fsync_directory(run_dir)
            spec = INVALIDATION_GRAPH[journal["job_id"]]
            return RepairResult(
                archive_path=journal["archive"],
                repaired_job=journal["job_id"],
                invalidated_jobs=spec.jobs,
                invalidated_stages=spec.stages,
            )
        if journal["state"] == "rolled-back":
            _validate_rolled_back_state(project, run_dir, journal)
            if staging.exists():
                if staging.is_symlink() or not staging.is_dir():
                    raise ValueError("repair cleanup staging is unsafe")
                shutil.rmtree(staging)
                _fsync_directory(staging.parent)
            journal_path.unlink()
            _fsync_directory(run_dir)
            return "rolled-back"
        if staging.is_symlink() or not staging.is_dir():
            raise ValueError("repair staging is missing")
        _validate_artifact_tree(staging)
        _validate_prepared_staging(project, run_dir, staging, journal)
        repo_root = project.repository_root
        for item in journal["snapshots"]:
            path = repo_root / item["path"]
            _validate_destination_path(repo_root, path)
            if item["existed"]:
                backup = staging / item["backup"]
                if backup.is_symlink() or not backup.is_file():
                    raise ValueError("repair snapshot is missing")
                if _file_sha256(backup) != item["sha256"]:
                    raise ValueError("repair snapshot changed")
                _write_bytes_atomic(path, backup.read_bytes())
            else:
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise ValueError("repair snapshot destination is unsafe")
                path.unlink(missing_ok=True)
                _fsync_directory(path.parent)
        for item in reversed(journal["moves"]):
            source = repo_root / item["source"]
            archived = staging / item["destination"]
            _validate_destination_path(repo_root, source)
            if archived.exists():
                if source.exists() or source.is_symlink():
                    raise ValueError("repair move is inconsistent")
                _validate_artifact_tree(archived)
                if _node_digest(archived) != (item["node"], item["sha256"]):
                    raise ValueError("repair archived artifact changed")
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(archived, source)
                _fsync_directory(archived.parent)
                _fsync_directory(source.parent)
            elif not source.exists() or source.is_symlink():
                raise ValueError("repair move cannot be recovered")
            elif _node_digest(source) != (item["node"], item["sha256"]):
                raise ValueError("repair restored artifact changed")
        rolled_back = dict(journal)
        rolled_back["state"] = "rolled-back"
        _write_json_atomic(journal_path, rolled_back)
        shutil.rmtree(staging)
        _fsync_directory(staging.parent)
        journal_path.unlink()
        _fsync_directory(run_dir)
        return "rolled-back"
    except RepairError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise RepairError("repair recovery failed") from None


def _validate_repair_journal(
    project: Any, run_dir: Path, value: Any
) -> tuple[Path, Path]:
    if (
        not isinstance(value, dict)
        or set(value) != _JOURNAL_KEYS
        or value.get("schema_version") != 1
        or value.get("state") not in {"prepared", "rolled-back"}
        or value.get("pet_id") != project.pet_id
        or value.get("job_id") not in INVALIDATION_GRAPH
        or not isinstance(value.get("moves"), list)
        or not isinstance(value.get("snapshots"), list)
        or not _valid_sha256(value.get("plan_sha256"))
    ):
        raise ValueError("repair journal schema is invalid")
    repo_root = project.repository_root
    archives_root = repo_root / ".omnipet/archives/repairs"
    staging_relative = _safe_relative_path(value.get("staging"))
    archive_relative = _safe_relative_path(value.get("archive"))
    staging = repo_root / staging_relative
    archive = repo_root / archive_relative
    prefix = f"{project.pet_id}-{value['job_id']}-"
    if (
        staging.parent != archives_root
        or archive.parent != archives_root
        or not staging.name.startswith(f".{prefix}")
        or not archive.name.startswith(prefix)
    ):
        raise ValueError("repair journal paths are invalid")

    run_relative = run_dir.relative_to(repo_root)
    project_relative = project.root.relative_to(repo_root)
    allowed_delivery = {
        project.manifest_path.relative_to(repo_root),
        project.spritesheet_path.relative_to(repo_root),
    }
    plan_root = staging if staging.exists() else archive
    expected_canonical = _journal_canonical_source(plan_root, project, repo_root)
    if expected_canonical is None and project.canonical_base_path is not None:
        expected_canonical = project.canonical_base_path.relative_to(repo_root)
    allowed_run_roots = {
        "generated-sources", "decoded", "qa", "previews", "final",
    }
    seen_sources, seen_destinations = set(), set()
    for item in value["moves"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"source", "destination", "node", "sha256"}
            or item.get("node") not in {"file", "directory"}
            or not _valid_sha256(item.get("sha256"))
        ):
            raise ValueError("repair journal move is invalid")
        source = _safe_relative_path(item.get("source"))
        destination = _safe_relative_path(item.get("destination"))
        expected = None
        if source.is_relative_to(run_relative):
            run_source = source.relative_to(run_relative)
            if (
                run_source.parts[0] in allowed_run_roots
                or run_source == Path("package-complete.json")
                or run_source == Path("references/canonical-base.png")
            ):
                expected = Path("artifacts/run") / run_source
        elif (
            value["job_id"] == "base"
            and expected_canonical is not None
            and source == expected_canonical
            and source.is_relative_to(project_relative)
        ):
            expected = Path("artifacts/project") / source.relative_to(project_relative)
        elif source in allowed_delivery:
            expected = Path("artifacts/delivery") / source
        if (
            destination != expected
            or source in seen_sources
            or destination in seen_destinations
        ):
            raise ValueError("repair journal move paths are invalid")
        seen_sources.add(source)
        seen_destinations.add(destination)

    expected_snapshot_paths = {
        (run_dir / "imagegen-jobs.json").relative_to(repo_root),
        (run_dir / "qa/approvals.json").relative_to(repo_root),
        (run_dir / "workflow.json").relative_to(repo_root),
    }
    if value["job_id"] == "base":
        expected_snapshot_paths.add((project.root / "pet.yaml").relative_to(repo_root))
    actual_snapshot_paths = set()
    for item in value["snapshots"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "backup", "existed", "sha256"}
            or type(item.get("existed")) is not bool
            or (
                item.get("sha256") is not None
                and not _valid_sha256(item.get("sha256"))
            )
        ):
            raise ValueError("repair journal snapshot is invalid")
        path = _safe_relative_path(item.get("path"))
        backup_value = item.get("backup")
        backup = (
            _safe_relative_path(backup_value)
            if isinstance(backup_value, str)
            else None
        )
        expected_backup = (
            Path("state-before") / path.relative_to(run_relative)
            if path.is_relative_to(run_relative)
            else Path("state-before/project") / path.relative_to(project_relative)
        )
        if (
            path in actual_snapshot_paths
            or (item["existed"] and backup != expected_backup)
            or (item["existed"] and item["sha256"] is None)
            or (not item["existed"] and backup is not None)
            or (not item["existed"] and item["sha256"] is not None)
        ):
            raise ValueError("repair journal snapshot paths are invalid")
        actual_snapshot_paths.add(path)
    if actual_snapshot_paths != expected_snapshot_paths:
        raise ValueError("repair journal snapshots are incomplete")
    if value["plan_sha256"] != _plan_sha256(
        value["moves"], value["snapshots"]
    ):
        raise ValueError("repair journal plan changed")
    return staging, archive


def _validate_prepared_staging(
    project: Any, run_dir: Path, staging: Path, journal: dict[str, Any]
) -> None:
    expected_backups = {
        item["backup"] for item in journal["snapshots"] if item["existed"]
    }
    actual_backups = {
        str(path.relative_to(staging))
        for path in (staging / "state-before").rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_backups != expected_backups:
        raise ValueError("repair snapshot inventory changed")
    for item in journal["snapshots"]:
        if item["existed"]:
            backup = staging / item["backup"]
            if _file_sha256(backup) != item["sha256"]:
                raise ValueError("repair snapshot bytes changed")

    declared = [Path(item["destination"]) for item in journal["moves"]]
    artifacts = staging / "artifacts"
    actual_nodes = (
        {
            path.relative_to(staging)
            for path in artifacts.rglob("*")
        }
        if artifacts.exists()
        else set()
    )
    if any(
        not any(
            node == root
            or node.is_relative_to(root)
            or root.is_relative_to(node)
            for root in declared
        )
        for node in actual_nodes
    ):
        raise ValueError("repair staging contains undeclared artifacts")
    repo_root = project.repository_root
    for item in journal["moves"]:
        source = repo_root / item["source"]
        archived = staging / item["destination"]
        present = int(source.exists() and not source.is_symlink()) + int(
            archived.exists() and not archived.is_symlink()
        )
        if present != 1:
            raise ValueError("repair move inventory is inconsistent")
        current = archived if archived.exists() else source
        if _node_digest(current) != (item["node"], item["sha256"]):
            raise ValueError("repair move bytes changed")


def _validate_rolled_back_state(
    project: Any, run_dir: Path, journal: dict[str, Any]
) -> None:
    repo_root = project.repository_root
    for item in journal["snapshots"]:
        path = repo_root / item["path"]
        if item["existed"]:
            if path.is_symlink() or not path.is_file() or _file_sha256(path) != item["sha256"]:
                raise ValueError("repair live snapshot is not restored")
        elif path.exists() or path.is_symlink():
            raise ValueError("repair absent snapshot was recreated")
    for item in journal["moves"]:
        source = repo_root / item["source"]
        if (
            source.is_symlink()
            or not source.exists()
            or _node_digest(source) != (item["node"], item["sha256"])
        ):
            raise ValueError("repair live artifact is not restored")


def _plan_sha256(
    moves: list[dict[str, Any]], snapshots: list[dict[str, Any]]
) -> str:
    payload = json.dumps(
        {"moves": moves, "snapshots": snapshots},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _node_digest(path: Path) -> tuple[str, str]:
    _validate_artifact_tree(path)
    if path.is_file():
        return "file", _file_sha256(path)
    records = []
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = child.relative_to(path).as_posix()
        if child.is_dir():
            records.append({"path": relative, "kind": "directory"})
        else:
            records.append({
                "path": relative,
                "kind": "file",
                "sha256": _file_sha256(child),
            })
    payload = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return "directory", hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _journal_canonical_source(
    staging: Path, project: Any, repo_root: Path
) -> Path | None:
    manifest = staging / "state-before/project/pet.yaml"
    if manifest.is_symlink() or not manifest.is_file():
        return None
    value = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    approved = value.get("approved") if isinstance(value, dict) else None
    canonical = (
        approved.get("canonical_base")
        if isinstance(approved, dict)
        else None
    )
    if canonical is None:
        return None
    relative = _safe_relative_path(canonical)
    path = project.root / relative
    if not path.is_relative_to(project.root):
        raise ValueError("repair canonical snapshot is unsafe")
    return path.relative_to(repo_root)


def _safe_relative_path(value: Any) -> Path:
    if not isinstance(value, str):
        raise ValueError("repair path is invalid")
    path = Path(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "." in path.parts
        or path.as_posix() != value
    ):
        raise ValueError("repair path is unsafe")
    return path


def _prepare_archive_root(repo_root: Path, root: Path) -> None:
    current = repo_root
    for part in root.relative_to(repo_root).parts:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ValueError("repair archive root is unsafe")
        current.mkdir(exist_ok=True)
        if current.resolve() != current.absolute():
            raise ValueError("repair archive root is unsafe")


def _validate_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
        raise ValueError("repair run is unsafe")


def _safe_file(root: Path, relative: str) -> Path:
    path = root / relative
    _validate_move_source(root, path)
    if not path.is_file():
        raise ValueError("repair state file is missing")
    return path


def _validate_move_source(root: Path, path: Path) -> None:
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("repair artifact is unsafe")
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("repair artifact is unsafe")
    _validate_artifact_tree(path)


def _validate_destination_path(root: Path, path: Path) -> None:
    if not path.is_relative_to(root):
        raise ValueError("repair destination escapes repository")
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("repair destination is unsafe")
        if not current.exists():
            break


def _validate_artifact_tree(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError("repair artifact has an unsupported node")
        if stat.S_ISREG(mode):
            return
        pending = [path]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    child_mode = entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISLNK(child_mode):
                        raise ValueError("repair artifact contains a symlink")
                    if stat.S_ISDIR(child_mode):
                        pending.append(Path(entry.path))
                    elif not stat.S_ISREG(child_mode):
                        raise ValueError("repair artifact contains a special node")
    except OSError as error:
        raise ValueError("repair artifact tree is invalid") from error


def _write_json_atomic(path: Path, value: Any) -> None:
    content = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _write_bytes_atomic(path, content)


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_new_file(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
