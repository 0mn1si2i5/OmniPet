from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from omnipet.approvals import ApprovalError, load_approvals, required_artifacts
from omnipet.hatch.atlas import (
    AssembleExtendedAtlasConfig,
    ComposeAtlasConfig,
    assemble_extended_atlas,
    compose_atlas,
)
from omnipet.hatch.chroma import DespillConfig, despill
from omnipet.hatch.directions import (
    BlindQaSheetConfig,
    BlindVerdictConfig,
    CombineVerdictsConfig,
    ContinuityConfig,
    DirectionQaSheetConfig,
    combine_verdicts,
    make_direction_blind_qa_sheet,
    make_direction_qa_sheet,
    measure_direction_continuity,
    validate_blind_verdicts,
)
from omnipet.hatch.inspect import ContactSheetConfig, make_contact_sheet
from omnipet.hatch.validation import ValidateAtlasConfig, validate_atlas


class PackageError(RuntimeError):
    """Raised when package evidence or publication is invalid."""


def build_package_evidence(project: Any) -> str:
    run_dir = _run_dir(project)
    chroma = _read_json(_safe_file(run_dir, "pet_request.json"))["chroma_key"]["hex"]
    source = _assemble_package_source(run_dir, chroma)
    package_dir = run_dir / "qa/package-generated"
    cleaned_png = run_dir / "final/spritesheet-package.png"
    cleaned_webp = run_dir / "final/spritesheet-extended.webp"
    despill_report = package_dir / "despill.json"
    source_digest = _sha256(source)
    existing = _read_json_if_safe(despill_report)
    if existing is not None and existing.get("input_sha256") != source_digest:
        _invalidate_package_outputs(run_dir)
        existing = None
    reusable = (
        existing is not None
        and existing.get("ok") is True
        and existing.get("input_sha256") == source_digest
        and cleaned_png.is_file()
        and cleaned_webp.is_file()
        and existing.get("output_sha256") == _sha256(cleaned_webp)
    )
    if not reusable:
        result = despill(DespillConfig(
            input=source,
            output=cleaned_png,
            webp_output=cleaned_webp,
            json_out=despill_report,
            chroma_key=chroma,
        ))
        report = dict(result.report)
        report.update({"input_sha256": source_digest, "output_sha256": _sha256(cleaned_webp), "passes": 1})
        _write_json(despill_report, report)
    validation = validate_atlas(ValidateAtlasConfig(
        atlas=cleaned_webp,
        json_out=package_dir / "validation.json",
        require_v2=True,
    ))
    if not validation.ok:
        raise PackageError("v2 atlas validation failed")
    make_contact_sheet(ContactSheetConfig(cleaned_webp, package_dir / "contact-sheet.png"))
    make_direction_qa_sheet(DirectionQaSheetConfig(cleaned_webp, package_dir / "direction-sheet.png"))
    continuity = measure_direction_continuity(ContinuityConfig(cleaned_webp, package_dir / "continuity.json"))
    if continuity.report.get("ok") is not True:
        raise PackageError("direction continuity failed")
    make_direction_blind_qa_sheet(BlindQaSheetConfig(
        cleaned_webp,
        package_dir / "blind-sheet.png",
        package_dir / "blind-answer-key.json",
    ))
    _write_json(run_dir / "final/pet.json", {
        "id": project.pet_id,
        "displayName": project.display_name,
        "description": project.description,
        "spriteVersionNumber": 2,
        "spritesheetPath": project.spritesheet_path.name,
    })
    return "awaiting_external_verdict"


def _assemble_package_source(run_dir: Path, chroma: str) -> Path:
    base = compose_atlas(ComposeAtlasConfig(
        output=run_dir / "final/package-base.png",
        frames_root=run_dir / "frames",
        webp_output=run_dir / "final/package-base.webp",
    ))
    extended = assemble_extended_atlas(AssembleExtendedAtlasConfig(
        base_atlas=base.webp_output,
        registered_row_9=run_dir / "qa/directions/look-row-9-registered.png",
        row_9_registration=run_dir / "qa/directions/look-row-9-registration.json",
        look_row_10=run_dir / "decoded/look-row-10.png",
        neutral_cell=run_dir / "frames/idle/00.png",
        output=run_dir / "final/package-source.png",
        chroma_key=chroma,
    ))
    return extended.output


def _invalidate_package_outputs(run_dir: Path) -> None:
    for relative in (
        "final/spritesheet-package.png",
        "final/spritesheet-extended.webp",
        "final/pet.json",
        "package-complete.json",
    ):
        (run_dir / relative).unlink(missing_ok=True)
    for relative in ("qa/package-generated", "qa/package-reviewed"):
        path = run_dir / relative
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


def import_package_verdict(project: Any, verdict_file: Path) -> None:
    run_dir = _run_dir(project)
    _recover_review_publication(run_dir)
    verdict = _external_json(verdict_file)
    reports = _validate_package_verdict(run_dir, verdict)
    reviewed = run_dir / "qa/package-reviewed"
    staging = reviewed.with_name(".package-reviewed-stage")
    backup = reviewed.with_name(".package-reviewed-backup")
    journal = reviewed.parent / "package-review-publication.json"
    try:
        for path in (staging, backup):
            if path.exists():
                shutil.rmtree(path)
        staging.mkdir()
        for name, payload in reports.items():
            _write_json(staging / name, payload)
        _write_json(journal, _review_journal("prepared", reviewed, staging, backup))
        if reviewed.exists():
            os.replace(reviewed, backup)
            _fsync_directory(reviewed.parent)
        _write_json(journal, _review_journal("backed-up", reviewed, staging, backup))
        os.replace(staging, reviewed)
        _fsync_directory(reviewed.parent)
        _write_json(journal, _review_journal("installed", reviewed, staging, backup))
        if backup.exists():
            shutil.rmtree(backup)
            _fsync_directory(reviewed.parent)
        journal.unlink(missing_ok=True)
        _fsync_directory(reviewed.parent)
    except PackageError:
        raise
    except (OSError, ValueError):
        _rollback_review_publication(reviewed, staging, backup)
        journal.unlink(missing_ok=True)
        raise PackageError("package verdict publication failed") from None
    finally:
        if not journal.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _review_journal(state: str, reviewed: Path, staging: Path, backup: Path) -> dict[str, Any]:
    return {"schema_version": 1, "state": state, "reviewed": str(reviewed), "stage": str(staging), "backup": str(backup)}


def _recover_review_publication(run_dir: Path) -> None:
    qa = run_dir / "qa"
    reviewed = qa / "package-reviewed"
    staging = qa / ".package-reviewed-stage"
    backup = qa / ".package-reviewed-backup"
    journal = qa / "package-review-publication.json"
    if not journal.exists():
        return
    value = _read_json(journal)
    if value != _review_journal(value.get("state"), reviewed, staging, backup):
        raise PackageError("package review publication journal is unsafe")
    if value["state"] in {"prepared", "backed-up"}:
        _rollback_review_publication(reviewed, staging, backup)
    elif value["state"] == "installed":
        shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
    else:
        raise PackageError("package review publication journal is invalid")
    journal.unlink(missing_ok=True)
    _fsync_directory(qa)


def _rollback_review_publication(reviewed: Path, staging: Path, backup: Path) -> None:
    if backup.exists():
        if reviewed.exists():
            shutil.rmtree(reviewed)
        os.replace(backup, reviewed)
    elif reviewed.exists() and not staging.exists():
        shutil.rmtree(reviewed)
    shutil.rmtree(staging, ignore_errors=True)
    _fsync_directory(reviewed.parent)


def _validate_package_verdict(run_dir: Path, verdict: dict[str, Any]) -> dict[str, dict[str, Any]]:
    reviewers = verdict.get("reviewers") if isinstance(verdict, dict) else None
    visual = verdict.get("final_visual") if isinstance(verdict, dict) else None
    if (
        set(verdict) != {"schema_version", "stage", "reviewers", "directions", "direction_evidence", "final_visual"}
        or verdict.get("schema_version") != 1
        or verdict.get("stage") != "package"
        or not isinstance(reviewers, list)
        or len(reviewers) != 3
        or len({item.get("reviewer") for item in reviewers if isinstance(item, dict)}) != 3
    ):
        raise PackageError("package verdict schema is invalid")
    package_dir = run_dir / "qa/package-generated"
    normalized_reviews = []
    for reviewer in reviewers:
        if (
            not isinstance(reviewer, dict)
            or set(reviewer) != {"reviewer", "pairs"}
            or not isinstance(reviewer.get("reviewer"), str)
            or not reviewer["reviewer"].strip()
            or not isinstance(reviewer.get("pairs"), list)
        ):
            raise PackageError("package reviewer is invalid")
        normalized_reviews.append({"pairs": reviewer["pairs"]})
    if (
        not isinstance(visual, dict)
        or set(visual) != {"verdict", "note", "evidence"}
        or visual.get("verdict") != "pass"
        or not isinstance(visual.get("note"), str)
        or not visual["note"].strip()
    ):
        raise PackageError("final visual verdict is invalid")
    evidence = _verify_external_evidence(run_dir, visual.get("evidence"), (
        "qa/package-generated/blind-sheet.png", "final/spritesheet-extended.webp"
    ))
    staging = Path(tempfile.mkdtemp(prefix=".package-verdict-", dir=package_dir))
    try:
        reviewer_paths = []
        for index, review in enumerate(normalized_reviews, 1):
            path = staging / f"blind-review-{index}.json"
            _write_json(path, review)
            reviewer_paths.append(path)
        combined = combine_verdicts(CombineVerdictsConfig(
            tuple(reviewer_paths), staging / "blind-consensus.json"
        ))
        validated = validate_blind_verdicts(BlindVerdictConfig(
            package_dir / "blind-answer-key.json",
            combined.report_path,
            staging / "blind-validation.json",
        ))
        if not validated.ok:
            raise PackageError("blind direction validation failed")
        semantics = _validate_direction_semantics(run_dir, verdict.get("directions"), verdict.get("direction_evidence"))
        visual_report = {
            "ok": True,
            "verdict": "pass",
            "note": visual["note"].strip(),
            "evidence": evidence,
        }
        return {
            "blind-consensus.json": combined.report,
            "blind-validation.json": validated.report,
            "final-direction-semantics.json": semantics,
            "final-visual-review.json": visual_report,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _validate_direction_semantics(run_dir: Path, directions: Any, evidence: Any) -> dict[str, Any]:
    labels = ("000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5", "180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5")
    expected_axes = []
    for index, label in enumerate(labels):
        horizontal = "center" if index in (0, 8) else "right" if index < 8 else "left"
        vertical = "center" if index in (4, 12) else "up" if index < 8 or index > 12 else "down"
        expected_axes.append((label, {"horizontal": horizontal, "vertical": vertical}))
    if not isinstance(directions, list) or len(directions) != 16:
        raise PackageError("final direction semantics count is invalid")
    normalized = []
    for item, (label, expected) in zip(directions, expected_axes, strict=True):
        cardinal = label in {"000", "090", "180", "270"}
        if (
            not isinstance(item, dict)
            or set(item) != {"direction", "expected", "observed", "verdict", "note"}
            or item.get("direction") != label
            or item.get("expected") != expected
            or set(item.get("observed", {})) != {"horizontal", "vertical"}
            or any(item["observed"].get(axis) not in {"left", "right", "up", "down", "center", "ambiguous"} for axis in ("horizontal", "vertical"))
            or item.get("verdict") != "pass"
            or item.get("observed") != expected
            or not isinstance(item.get("note"), str)
            or not item["note"].strip()
        ):
            raise PackageError("final direction semantic verdict is invalid")
        normalized.append(item)
    bound = _verify_external_evidence(run_dir, evidence, (
        "qa/package-generated/direction-sheet.png", "final/spritesheet-extended.webp"
    ))
    return {"schema_version": 1, "ok": True, "directions": normalized, "evidence": bound}


def check_package(project: Any) -> dict[str, Any]:
    run_dir = _run_dir(project)
    if (
        (run_dir / "package-publication.json").exists()
        or (run_dir / "qa/package-review-publication.json").exists()
    ):
        raise PackageError("package recovery is required; run omnipet package --recover")
    _validated_package_approval(run_dir)
    manifest_path = _safe_file(run_dir, "final/pet.json")
    atlas_path = _safe_file(run_dir, "final/spritesheet-extended.webp")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PackageError("package manifest is invalid") from None
    expected = {
        "id": project.pet_id,
        "displayName": project.display_name,
        "description": project.description,
        "spriteVersionNumber": 2,
        "spritesheetPath": project.spritesheet_path.name,
    }
    if manifest != expected or not atlas_path.stat().st_size:
        raise PackageError("package candidate is invalid")
    try:
        validation = _read_json(_safe_file(run_dir, "qa/package-generated/validation.json"))
        despill_report = _read_json(_safe_file(run_dir, "qa/package-generated/despill.json"))
        continuity = _read_json(_safe_file(run_dir, "qa/package-generated/continuity.json"))
        blind = _read_json(_safe_file(run_dir, "qa/package-reviewed/blind-validation.json"))
        semantics = _read_json(_safe_file(run_dir, "qa/package-reviewed/final-direction-semantics.json"))
        visual = _read_json(_safe_file(run_dir, "qa/package-reviewed/final-visual-review.json"))
        if (
            validation.get("ok") is not True
            or validation.get("sprite_version_number") != 2
            or (validation.get("width"), validation.get("height")) != (1536, 2288)
            or validation.get("errors") != []
            or despill_report.get("ok") is not True
            or despill_report.get("passes") != 1
            or continuity.get("ok") is not True
            or continuity.get("reviewRequired") is not False
            or continuity.get("warnings") != []
            or blind.get("ok") is not True
            or blind.get("unconfirmed") != []
            or semantics.get("ok") is not True
            or visual.get("ok") is not True
            or visual.get("verdict") != "pass"
        ):
            raise PackageError("package QA is not accepted")
    except KeyError:
        raise PackageError("package QA is invalid") from None
    return manifest


def publish_package(project: Any) -> tuple[Path, Path]:
    run_dir = _run_dir(project)
    _recover_publication(project, run_dir)
    check_package(project)
    sources = (
        _safe_file(run_dir, "final/pet.json"),
        _safe_file(run_dir, "final/spritesheet-extended.webp"),
    )
    destinations = (project.manifest_path, project.spritesheet_path)
    dist = destinations[0].parent
    if any(destination.parent != dist for destination in destinations):
        raise PackageError("package destinations must share a directory")
    if dist.is_symlink() or (dist.exists() and not dist.is_dir()):
        raise PackageError("package destination directory is unsafe")
    if all(destination.is_file() and _sha256(destination) == _sha256(source) for source, destination in zip(sources, destinations, strict=True)):
        _mark_delivered(run_dir)
        return destinations
    if any(destination.exists() for destination in destinations):
        raise PackageError("package destination collides with existing content")
    stage = dist.with_name(f".{dist.name}-stage")
    backup = dist.with_name(f".{dist.name}-backup")
    journal = run_dir / "package-publication.json"
    workflow = run_dir / "workflow.json"
    marker = run_dir / "package-complete.json"
    snapshots = {path: path.read_bytes() if path.is_file() and not path.is_symlink() else None for path in (workflow, marker)}
    try:
        for path in (stage, backup):
            if path.exists():
                shutil.rmtree(path)
        if dist.exists():
            shutil.copytree(dist, stage)
        else:
            stage.mkdir(parents=True)
        shutil.copyfile(sources[0], stage / destinations[0].name)
        shutil.copyfile(sources[1], stage / destinations[1].name)
        _write_json(journal, {"schema_version": 1, "state": "prepared", "dist": str(dist), "stage": str(stage), "backup": str(backup)})
        if dist.exists():
            os.replace(dist, backup)
            _fsync_directory(dist.parent)
        _write_json(journal, {"schema_version": 1, "state": "backed-up", "dist": str(dist), "stage": str(stage), "backup": str(backup)})
        os.replace(stage, dist)
        _fsync_directory(dist.parent)
        _write_json(journal, {"schema_version": 1, "state": "installed", "dist": str(dist), "stage": str(stage), "backup": str(backup)})
        _mark_delivered(run_dir)
        if backup.exists():
            shutil.rmtree(backup)
        journal.unlink(missing_ok=True)
        _fsync_directory(dist.parent)
    except BaseException as error:
        if not isinstance(error, (KeyboardInterrupt, SystemExit)):
            _rollback_publication(dist, stage, backup)
            for path, content in snapshots.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            journal.unlink(missing_ok=True)
            if isinstance(error, PackageError):
                raise
            raise PackageError("package publication failed") from None
        raise
    finally:
        if not journal.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return destinations


def _recover_publication(project: Any, run_dir: Path) -> None:
    journal = run_dir / "package-publication.json"
    if not journal.exists():
        return
    value = _read_json(journal)
    expected = {"schema_version", "state", "dist", "stage", "backup"}
    if set(value) != expected or value.get("schema_version") != 1:
        raise PackageError("package publication journal is invalid")
    dist = project.manifest_path.parent
    stage = dist.with_name(f".{dist.name}-stage")
    backup = dist.with_name(f".{dist.name}-backup")
    if value != {
        "schema_version": 1,
        "state": value.get("state"),
        "dist": str(dist),
        "stage": str(stage),
        "backup": str(backup),
    }:
        raise PackageError("package publication journal is unsafe")
    if value["state"] in {"prepared", "backed-up"}:
        _rollback_publication(dist, stage, backup)
    elif value["state"] == "installed":
        if backup.exists():
            shutil.rmtree(backup)
        shutil.rmtree(stage, ignore_errors=True)
    else:
        raise PackageError("package publication journal is invalid")
    journal.unlink(missing_ok=True)
    _fsync_directory(dist.parent)


def recover_package(project: Any) -> None:
    run_dir = _run_dir(project)
    _recover_review_publication(run_dir)
    _recover_publication(project, run_dir)


def _rollback_publication(dist: Path, stage: Path, backup: Path) -> None:
    if backup.exists():
        if dist.exists():
            shutil.rmtree(dist)
        os.replace(backup, dist)
    elif dist.exists() and not stage.exists():
        shutil.rmtree(dist)
    shutil.rmtree(stage, ignore_errors=True)


def _validated_package_approval(run_dir: Path) -> None:
    try:
        records = load_approvals(run_dir)
        package = next((record for record in records if record.stage == "package"), None)
        if package is None:
            raise PackageError("package approval is missing")
        if package.artifacts != required_artifacts(run_dir, "package"):
            raise PackageError("package approval is stale")
    except ApprovalError:
        raise PackageError("package approval is invalid") from None


def _mark_delivered(run_dir: Path) -> None:
    from omnipet.workflow import mark_package_complete

    mark_package_complete(run_dir)


def _run_dir(project: Any) -> Path:
    path = project.repository_root / ".omnipet/runs" / project.pet_id
    if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
        raise PackageError("package run is missing")
    return path


def _safe_file(run_dir: Path, relative: str) -> Path:
    path = run_dir / relative
    current = run_dir
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise PackageError("package candidate is unsafe")
    if not path.is_file() or not path.resolve().is_relative_to(run_dir):
        raise PackageError("package candidate is missing")
    return path


def _external_json(path: Path) -> dict[str, Any]:
    value = Path(path).absolute()
    if value.is_symlink() or not value.is_file():
        raise PackageError("package verdict is unsafe")
    try:
        payload = json.loads(value.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PackageError("package verdict is invalid") from None
    if not isinstance(payload, dict):
        raise PackageError("package verdict is invalid")
    return payload


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PackageError("package JSON evidence is invalid") from None
    if not isinstance(value, dict):
        raise PackageError("package JSON evidence is invalid")
    return value


def _read_json_if_safe(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _verify_external_evidence(
    run_dir: Path, evidence: Any, expected: tuple[str, ...]
) -> list[dict[str, str]]:
    if not isinstance(evidence, list) or len(evidence) != len(expected):
        raise PackageError("package verdict evidence is invalid")
    result = []
    for item, relative in zip(evidence, expected, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or item.get("path") != relative
            or item.get("sha256") != _sha256(_safe_file(run_dir, relative))
        ):
            raise PackageError("package verdict evidence is stale")
        result.append({"path": relative, "sha256": item["sha256"]})
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
