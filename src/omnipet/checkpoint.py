from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omnipet.project import PetProject
from omnipet.run import EXPECTED_JOB_IDS, RunState, load_run_state, prepare_run
from omnipet.security import contains_credential_like_text, is_credential_like_key


class CheckpointError(RuntimeError):
    """Raised when a portable accepted-state checkpoint is unsafe or invalid."""


_CHECKPOINT_KEYS = {
    "schema_version", "pet_id", "sprite_version", "completed_jobs", "status_frontier",
    "artifacts", "provenance", "accepted_qa",
}
_ARTIFACT_KEYS = {"job_id", "role", "path", "sha256"}
_PROVENANCE_KEYS = {
    "job_id", "completed_at", "derived_from", "mirror_decision", "metadata",
    "adoption_decision",
}
_QA_KEYS = {"job_id", "path", "sha256"}
_PROVENANCE_FIELDS = _PROVENANCE_KEYS - {"job_id"}
_QA_EVIDENCE_FIELDS = {
    "ok", "job_id", "completed_at", "sha256", "canvas", "adoption_decision",
    "mirror_decision", "metadata",
}
_NESTED_PROVENANCE_KEYS = {
    "metadata": {"sha256", "format", "mode", "width", "height"},
    "mirror_decision": {"approved", "transform", "note", "approved_at"},
}


def export_checkpoint(project: PetProject, *, force: bool = False) -> Path:
    repo_root = project.repository_root
    destination = project.root / "checkpoint"
    staging: Path | None = None
    backup = project.root / f".checkpoint-backup-{uuid.uuid4().hex}"
    try:
        _validate_destination(project.root, destination)
        if destination.exists() and not force:
            raise ValueError("checkpoint destination exists")
        _cleanup_stale_checkpoint_backups(project)

        state = load_run_state(repo_root, project.pet_id, display_name=project.display_name)
        run_dir = state.run_dir
        manifest = _read_json(run_dir / "imagegen-jobs.json")
        jobs = manifest["jobs"]
        completed = [job for job in jobs if job["status"] == "complete"]
        if not completed or project.canonical_base_path is None:
            raise ValueError("checkpoint has no completed jobs")

        staging = Path(tempfile.mkdtemp(prefix=".checkpoint-", dir=project.root))
        artifacts: list[dict[str, str]] = []
        provenance: list[dict[str, Any]] = []
        for job in completed:
            job_id = job["id"]
            output = run_dir / job["output_path"]
            artifacts.append(_copy_artifact(run_dir, output, staging, job_id, "decoded"))
            source_value = job["source_path"]
            if Path(source_value).is_absolute():
                source = Path(source_value)
                artifacts.append(_copy_artifact(run_dir, source, staging, job_id, "source"))
            item = {"job_id": job_id}
            for key in _PROVENANCE_FIELDS:
                if key in job:
                    value = job[key]
                    if key == "metadata" and isinstance(value, dict):
                        value = {
                            field: value[field]
                            for field in _NESTED_PROVENANCE_KEYS["metadata"]
                            if field in value
                        }
                    if key != "metadata" or value:
                        item[key] = value
            _validate_safe_metadata(item)
            provenance.append(item)
        canonical = project.canonical_base_path
        artifacts.append(_copy_artifact(project.root, canonical, staging, "base", "canonical"))

        accepted_qa = _export_accepted_qa(run_dir, staging, {job["id"] for job in completed})
        payload = {
            "schema_version": 1,
            "pet_id": project.pet_id,
            "sprite_version": project.minimum_sprite_version,
            "completed_jobs": [job["id"] for job in completed],
            "status_frontier": _status_frontier(jobs),
            "artifacts": artifacts,
            "provenance": provenance,
            "accepted_qa": accepted_qa,
        }
        _write_json(staging / "checkpoint.json", payload)
        _validated_checkpoint(project, staging)

        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(staging, destination)
            staging = None
        except Exception:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            try:
                shutil.rmtree(backup)
            except OSError:
                pass
        return destination
    except Exception:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise CheckpointError("checkpoint export failed") from None


def restore_checkpoint(
    project: PetProject,
    *,
    force: bool = False,
) -> RunState:
    checkpoint = project.root / "checkpoint"
    run_dir = project.repository_root / ".omnipet" / "runs" / project.pet_id
    archive: Path | None = None
    try:
        payload = _validated_checkpoint(project, checkpoint)
        _validate_runtime_chain(project)
        if run_dir.exists() or run_dir.is_symlink():
            if not force:
                raise ValueError("run destination exists")
            archive = _archive_existing_run(project, run_dir)

        try:
            prepare_run(project, project.repository_root)
            _inject_checkpoint(project, checkpoint, payload, run_dir)
            from omnipet.approvals import migrate_checkpoint_base_approval
            migrate_checkpoint_base_approval(run_dir, payload)
            return load_run_state(
                project.repository_root, project.pet_id, display_name=project.display_name
            )
        except Exception:
            if run_dir.exists() and not run_dir.is_symlink():
                shutil.rmtree(run_dir)
            if archive is not None and archive.exists():
                os.replace(archive, run_dir)
            raise
    except Exception:
        raise CheckpointError("checkpoint restore failed") from None


def _validated_checkpoint(project: PetProject, root: Path) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir() or root.parent != project.root:
        raise ValueError("checkpoint root is invalid")
    _reject_tree_links(root)
    data = _read_json(root / "checkpoint.json")
    _validate_safe_metadata(data)
    if not isinstance(data, dict) or set(data) != _CHECKPOINT_KEYS:
        raise ValueError("checkpoint schema is invalid")
    completed = data["completed_jobs"]
    frontier = data["status_frontier"]
    artifacts = data["artifacts"]
    provenance = data["provenance"]
    accepted_qa = data["accepted_qa"]
    if (
        data["schema_version"] != 1
        or data["pet_id"] != project.pet_id
        or data["sprite_version"] != project.minimum_sprite_version
        or not isinstance(completed, list)
        or not completed
        or completed != [job_id for job_id in EXPECTED_JOB_IDS if job_id in completed]
        or len(set(completed)) != len(completed)
        or not isinstance(frontier, list)
        or not all(job_id in EXPECTED_JOB_IDS and job_id not in completed for job_id in frontier)
        or not isinstance(artifacts, list)
        or not isinstance(provenance, list)
        or not isinstance(accepted_qa, list)
    ):
        raise ValueError("checkpoint identity is invalid")

    artifact_jobs: dict[str, set[str]] = {job_id: set() for job_id in completed}
    artifact_hashes: dict[tuple[str, str], str] = {}
    listed_paths: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != _ARTIFACT_KEYS:
            raise ValueError("checkpoint artifact schema is invalid")
        job_id, role = item["job_id"], item["role"]
        if job_id not in artifact_jobs or role not in {"canonical", "decoded", "source"}:
            raise ValueError("checkpoint artifact identity is invalid")
        path = _relative_checkpoint_path(item["path"], "artifacts")
        if item["path"] in listed_paths or not _valid_sha(item["sha256"]):
            raise ValueError("checkpoint artifact record is invalid")
        listed_paths.add(item["path"])
        if _sha256(root / path) != item["sha256"]:
            raise ValueError("checkpoint artifact hash mismatch")
        artifact_jobs[job_id].add(role)
        artifact_hashes[(job_id, role)] = item["sha256"]
    if any("decoded" not in roles for roles in artifact_jobs.values()):
        raise ValueError("checkpoint decoded artifact is missing")
    if project.canonical_base_path is None or "canonical" not in artifact_jobs["base"]:
        raise ValueError("checkpoint canonical artifact is missing")
    canonical_hash = _sha256(project.canonical_base_path)
    if (
        artifact_hashes.get(("base", "canonical")) != canonical_hash
        or artifact_hashes.get(("base", "decoded")) != canonical_hash
        or artifact_hashes.get(("base", "source")) != canonical_hash
    ):
        raise ValueError("checkpoint canonical artifacts are inconsistent")

    statuses = [
        {
            "id": job_id,
            "status": "complete" if job_id in completed else "pending",
            "depends_on": list(_checkpoint_dependencies(job_id)),
        }
        for job_id in EXPECTED_JOB_IDS
    ]
    if frontier != _status_frontier(statuses):
        raise ValueError("checkpoint status frontier is invalid")

    if len(provenance) != len(completed):
        raise ValueError("checkpoint provenance is incomplete")
    for item, job_id in zip(provenance, completed, strict=True):
        if (
            not isinstance(item, dict)
            or not set(item).issubset(_PROVENANCE_KEYS)
            or set(item) < {"job_id", "completed_at"}
            or item["job_id"] != job_id
            or not isinstance(item["completed_at"], str)
            or not item["completed_at"]
        ):
            raise ValueError("checkpoint provenance is invalid")
        for key, allowed in _NESTED_PROVENANCE_KEYS.items():
            nested = item.get(key)
            if nested is not None and (not isinstance(nested, dict) or not set(nested).issubset(allowed)):
                raise ValueError("checkpoint nested provenance is invalid")
        _validate_safe_metadata(item)

    for item in accepted_qa:
        if not isinstance(item, dict) or set(item) != _QA_KEYS or item["job_id"] not in completed:
            raise ValueError("checkpoint QA schema is invalid")
        path = _accepted_evidence_path(item["path"])
        if item["path"] in listed_paths or not _valid_sha(item["sha256"]):
            raise ValueError("checkpoint QA record is invalid")
        listed_paths.add(item["path"])
        if _sha256(root / path) != item["sha256"]:
            raise ValueError("checkpoint QA hash mismatch")
        if path.suffix == ".json":
            _validate_safe_metadata(_read_json(root / path))

    actual = {
        str(path.relative_to(root)) for path in root.rglob("*")
        if path.is_file() and path.name != "checkpoint.json"
    }
    if actual != listed_paths:
        raise ValueError("checkpoint contains unlisted files")
    return data


def _inject_checkpoint(
    project: PetProject, checkpoint: Path, payload: dict[str, Any], run_dir: Path
) -> None:
    manifest_path = run_dir / "imagegen-jobs.json"
    manifest = _read_json(manifest_path)
    jobs = {job["id"]: job for job in manifest["jobs"]}
    records = {(item["job_id"], item["role"]): item for item in payload["artifacts"]}
    provenance = {item["job_id"]: item for item in payload["provenance"]}
    canonical_record = records[("base", "canonical")]
    _copy_checked(
        checkpoint / canonical_record["path"],
        run_dir / "references" / "canonical-base.png",
    )
    for job_id in payload["completed_jobs"]:
        job = jobs[job_id]
        decoded = run_dir / "decoded" / f"{job_id}.png"
        _copy_checked(checkpoint / records[(job_id, "decoded")]["path"], decoded)
        source_record = records.get((job_id, "source"))
        if source_record is not None:
            source = run_dir / "generated-sources" / f"{job_id}.png"
            _copy_checked(checkpoint / source_record["path"], source)
            job["source_path"] = str(source)
        else:
            job["source_path"] = f"decoded/{job['derived_from']}.png"
        job["status"] = "complete"
        for key, value in provenance[job_id].items():
            if key != "job_id":
                job[key] = value

    for item in payload["accepted_qa"]:
        relative = Path(item["path"])
        destination = (
            run_dir / "qa" / "visual-jobs" / relative.name
            if relative.parent == Path("qa") and relative.name.endswith(".result.json")
            else run_dir / relative
        )
        _copy_checked(checkpoint / item["path"], destination)
        if item["job_id"] == "base" and relative.parent == Path("qa"):
            _copy_checked(checkpoint / item["path"], run_dir / "qa" / "base" / "review.json")
    manifest["run_dir"] = str(run_dir)
    _write_json(manifest_path, manifest)


def _copy_artifact(
    run_dir: Path, source: Path, staging: Path, job_id: str, role: str
) -> dict[str, str]:
    if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(run_dir.resolve()):
        raise ValueError("accepted artifact is unsafe")
    relative = Path("artifacts") / role / f"{job_id}.png"
    _copy_checked(source, staging / relative)
    return {"job_id": job_id, "role": role, "path": str(relative), "sha256": _sha256(staging / relative)}


def _export_accepted_qa(run_dir: Path, staging: Path, completed: set[str]) -> list[dict[str, str]]:
    from omnipet.approvals import STAGE_EVIDENCE_PATHS, load_approvals

    result = []
    anchors = {
        "base": "base",
        "standard-rows": "review",
        "directions": "look-row-10",
        "package": "look-row-10",
    }
    guided_records = []
    for approval in load_approvals(run_dir):
        for value in STAGE_EVIDENCE_PATHS[approval.stage]:
            relative = Path(value)
            source = run_dir / relative
            normalized = _normalized_evidence_bytes(run_dir, source)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(normalized)
            record = {
                "job_id": anchors[approval.stage],
                "path": value,
                "sha256": "",
            }
            guided_records.append(record)
            result.append(record)

    selected = {record["path"] for record in guided_records}
    for record in guided_records:
        path = staging / record["path"]
        if path.suffix == ".json":
            value = _refresh_evidence_hashes(_read_json(path), staging, selected)
            _validate_safe_metadata(value)
            _write_json(path, value)
        record["sha256"] = _sha256(path)

    source_root = run_dir / "qa" / "visual-jobs"
    if not source_root.exists():
        return result
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError("accepted QA root is unsafe")
    for source in sorted(source_root.glob("*.result.json")):
        value = _read_json(source)
        job_id = value.get("job_id") if isinstance(value, dict) else None
        if job_id not in completed or value.get("ok") is not True:
            continue
        evidence = {key: value[key] for key in _QA_EVIDENCE_FIELDS if key in value}
        _validate_safe_metadata(evidence)
        relative = Path("qa") / source.name
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_json(destination, evidence)
        result.append({"job_id": job_id, "path": str(relative), "sha256": _sha256(staging / relative)})
    return result


def _normalized_evidence_bytes(run_dir: Path, source: Path) -> bytes:
    if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(run_dir.resolve()):
        raise ValueError("accepted QA evidence is unsafe")
    if source.suffix == ".json":
        normalized = _normalize_run_paths(_read_json(source), run_dir.resolve())
        _validate_safe_metadata(normalized)
        return (json.dumps(normalized, indent=2, sort_keys=True) + "\n").encode()
    if source.suffix.lower() in {".md", ".markdown"}:
        text = source.read_text(encoding="utf-8")
        root = str(run_dir.resolve())
        normalized = text.replace(root + os.sep, "")
        for match in re.finditer(r"(?<![A-Za-z0-9:])(/[^\s`\"'<>]+)", normalized):
            if Path(match.group(1)).is_absolute():
                raise ValueError("accepted QA evidence contains an external absolute path")
        _validate_safe_metadata(normalized)
        return normalized.encode()
    return source.read_bytes()


def _normalize_run_paths(value: Any, run_dir: Path) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_run_paths(child, run_dir) for key, child in value.items()}
    if isinstance(value, list):
        return [_normalize_run_paths(child, run_dir) for child in value]
    if isinstance(value, str) and Path(value).is_absolute():
        path = Path(value)
        if path == run_dir or not path.is_relative_to(run_dir):
            raise ValueError("accepted QA evidence contains an external absolute path")
        return str(path.relative_to(run_dir))
    return value


def _refresh_evidence_hashes(value: Any, staging: Path, selected: set[str]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"path", "sha256"} and value.get("path") in selected:
            return {"path": value["path"], "sha256": _sha256(staging / value["path"])}
        return {key: _refresh_evidence_hashes(child, staging, selected) for key, child in value.items()}
    if isinstance(value, list):
        return [_refresh_evidence_hashes(child, staging, selected) for child in value]
    return value


def _accepted_evidence_path(value: Any) -> Path:
    from omnipet.approvals import STAGE_EVIDENCE_PATHS

    if not isinstance(value, str):
        raise ValueError("checkpoint QA path is invalid")
    path = Path(value)
    guided = {item for paths in STAGE_EVIDENCE_PATHS.values() for item in paths}
    legacy = len(path.parts) == 2 and path.parts[0] == "qa" and path.name.endswith(".result.json")
    if path.is_absolute() or ".." in path.parts or (value not in guided and not legacy):
        raise ValueError("checkpoint QA path is unsafe")
    return path


def _status_frontier(jobs: list[dict[str, Any]]) -> list[str]:
    complete = {job["id"] for job in jobs if job["status"] == "complete"}
    if "base" not in complete:
        candidates = {"base"}
    elif not {"idle", "running-right"}.issubset(complete):
        candidates = {"idle", "running-right"}
    elif not set(EXPECTED_JOB_IDS[1:10]).issubset(complete):
        candidates = set(EXPECTED_JOB_IDS[3:10])
    elif "look-cardinals" not in complete:
        candidates = {"look-cardinals"}
    elif "look-row-9" not in complete:
        candidates = {"look-row-9"}
    elif "look-row-10" not in complete:
        candidates = {"look-row-10"}
    else:
        candidates = set()
    return [
        job["id"] for job in jobs
        if job["id"] in candidates
        and job["status"] == "pending"
        and all(dependency in complete for dependency in job["depends_on"])
    ]


def _checkpoint_dependencies(job_id: str) -> tuple[str, ...]:
    if job_id == "base":
        return ()
    if job_id == "running-left":
        return ("base", "running-right")
    if job_id == "look-cardinals":
        return EXPECTED_JOB_IDS[1:10]
    if job_id == "look-row-9":
        return ("look-cardinals",)
    if job_id == "look-row-10":
        return ("look-cardinals", "look-row-9")
    return ("base",)


def _archive_existing_run(project: PetProject, run_dir: Path) -> Path:
    archives = project.repository_root / ".omnipet" / "archives"
    if archives.is_symlink() or (archives.exists() and not archives.is_dir()):
        raise ValueError("runtime archive root is invalid")
    archives.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = archives / f"{project.pet_id}-checkpoint-restore-{stamp}"
    os.replace(run_dir, archive)
    return archive


def _validate_runtime_chain(project: PetProject) -> None:
    root = project.repository_root
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ValueError("repository root is not canonical")
    omnipet = root / ".omnipet"
    runs = omnipet / "runs"
    run_dir = runs / project.pet_id
    archives = omnipet / "archives"
    for path in (omnipet, runs, run_dir, archives):
        if path.is_symlink():
            raise ValueError("runtime path contains a symlink")
        if path.exists() and (not path.is_dir() or path.resolve() != path):
            raise ValueError("runtime path is not an owned canonical directory")
        if not path.resolve(strict=False).is_relative_to(root):
            raise ValueError("runtime path escapes repository root")


def _cleanup_stale_checkpoint_backups(project: PetProject) -> None:
    for backup in project.root.glob(".checkpoint-backup-*"):
        if backup.is_symlink() or not backup.is_dir() or backup.parent != project.root:
            continue
        try:
            _validated_checkpoint(project, backup)
            shutil.rmtree(backup)
        except (OSError, ValueError):
            pass


def _validate_destination(root: Path, destination: Path) -> None:
    if destination.parent != root or destination.is_symlink():
        raise ValueError("checkpoint destination is unsafe")
    if destination.exists() and not destination.is_dir():
        raise ValueError("checkpoint destination is invalid")


def _reject_tree_links(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("checkpoint contains a symlink")


def _relative_checkpoint_path(value: Any, prefix: str) -> Path:
    if not isinstance(value, str):
        raise ValueError("checkpoint path is invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) < 2 or path.parts[0] != prefix:
        raise ValueError("checkpoint path is unsafe")
    return path


def _validate_safe_metadata(value: Any) -> None:
    stack = [value]
    visited = 0
    while stack:
        visited += 1
        if visited > 10_000:
            raise ValueError("checkpoint metadata exceeds scan limit")
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or is_credential_like_key(key):
                    raise ValueError("checkpoint metadata is unsafe")
                normalized = key.casefold().replace("-", "_")
                if normalized in {"raw_response", "provider_response", "raw_provider_response"}:
                    raise ValueError("checkpoint metadata is unsafe")
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            if Path(item).is_absolute():
                raise ValueError("checkpoint metadata contains an absolute path")
            if contains_credential_like_text(item):
                raise ValueError("checkpoint metadata contains credential text")
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("checkpoint metadata value is invalid")


def _copy_checked(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError("checkpoint source is unsafe")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        os.close(descriptor)
        shutil.copyfile(source, temporary)
        if _sha256(temporary) != _sha256(source):
            raise ValueError("checkpoint copy failed validation")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("checkpoint metadata is missing")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("checkpoint file is unsafe")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
