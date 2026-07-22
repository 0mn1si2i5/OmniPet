from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGES = ("base", "standard-rows", "directions", "package")
ACCEPTED_BASE_DECISION = "approved durable canonical"
_BASE_REVIEW_KEYS = {"adoption_decision", "canvas", "completed_at", "job_id", "ok", "sha256"}
_STANDARD_IDS = ("idle", "running-right", "running-left", "waving", "jumping", "failed", "waiting", "running", "review")
STAGE_EVIDENCE_PATHS = {
    "base": ("qa/base/review.json",),
    "standard-rows": tuple(
        path
        for job_id in _STANDARD_IDS
        for path in (
            f"qa/rows/{job_id}/deterministic.json",
            f"qa/rows/{job_id}/review.json",
            f"previews/{job_id}.gif",
        )
    ) + ("qa/standard/contact-sheet.png", "qa/standard/previews.json", "qa/standard/review.json"),
    "directions": (
        "decoded/look-cardinals-approved.png",
        "qa/directions/cardinals/deterministic.json",
        "qa/directions/cardinals/review.json",
        "qa/directions/cardinals/sheet.png",
        "qa/directions/look-row-9-registered.png",
        "qa/directions/look-row-9-registration.json",
        "qa/directions/look-row-9-continuity.json",
        "qa/directions/look-row-9-contact-sheet.png",
        "final/spritesheet-extended.png",
        "qa/directions/look-row-10-registration.json",
        "qa/directions/direction-semantics.json",
        "qa/directions/continuity.json",
        "qa/directions/contact-sheet.png",
    ),
    "package": (
        "final/pet.json",
        "final/package-source.png",
        "final/spritesheet-extended.webp",
        "qa/package-generated/validation.json",
        "qa/package-generated/despill.json",
        "qa/package-generated/contact-sheet.png",
        "qa/package-generated/direction-sheet.png",
        "qa/package-generated/continuity.json",
        "qa/package-generated/blind-sheet.png",
        "qa/package-generated/blind-answer-key.json",
        "qa/package-reviewed/blind-consensus.json",
        "qa/package-reviewed/blind-validation.json",
        "qa/package-reviewed/final-direction-semantics.json",
        "qa/package-reviewed/final-visual-review.json",
    ),
}
_DOCUMENT_KEYS = {"schema_version", "approvals"}
_RECORD_KEYS = {"stage", "artifacts", "approved_at"}
_ARTIFACT_KEYS = {"path", "sha256"}


class ApprovalError(RuntimeError):
    """Raised when approval evidence or persistence is invalid."""


@dataclass(frozen=True)
class ArtifactHash:
    path: str
    sha256: str


@dataclass(frozen=True)
class StageApproval:
    stage: str
    artifacts: tuple[ArtifactHash, ...]
    approved_at: str
    note: str | None = None


def load_approvals(run_dir: Path) -> tuple[StageApproval, ...]:
    run_dir = _validated_run_dir(run_dir)
    path = run_dir / "qa" / "approvals.json"
    if not path.exists():
        return ()
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("approval document is unsafe")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != _DOCUMENT_KEYS or data.get("schema_version") != 1:
            raise ValueError("approval document schema is invalid")
        values = data.get("approvals")
        if not isinstance(values, list):
            raise ValueError("approval records are invalid")
        records = tuple(_parse_record(item) for item in values)
        stages = [record.stage for record in records]
        if stages != list(STAGES[:len(stages)]):
            raise ValueError("approval order is invalid")
        return records
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ApprovalError("approvals are invalid") from None


def required_artifacts(run_dir: Path, stage: str) -> tuple[ArtifactHash, ...]:
    run_dir = _validated_run_dir(run_dir)
    if stage not in STAGES:
        raise ApprovalError("approval stage is invalid")
    try:
        relative_paths = _required_paths(run_dir, stage)
        _validate_qa_decisions(run_dir, stage)
        return tuple(
            ArtifactHash(path, _sha256_safe(run_dir, path))
            for path in sorted(relative_paths)
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ApprovalError("approval evidence is invalid") from None


def validate_direction_checkpoint(run_dir: Path, phase: str) -> None:
    run_dir = _validated_run_dir(run_dir)
    try:
        _validate_cardinal_review(run_dir)
        if phase == "row9":
            _validate_row9_review(run_dir)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ApprovalError("direction checkpoint is invalid") from None


def approve_stage(run_dir: Path, stage: str, *, note: str | None = None) -> StageApproval:
    from omnipet.workflow import _approve_stage_operation
    return _approve_stage_operation(run_dir, stage, note=note)


def _approve_stage_unlocked(run_dir: Path, stage: str, *, note: str | None = None) -> StageApproval:
    run_dir = _validated_run_dir(run_dir)
    if note is not None and (not isinstance(note, str) or not note.strip()):
        raise ApprovalError("approval note is invalid")
    records = list(load_approvals(run_dir))
    index = STAGES.index(stage)
    if [record.stage for record in records] != list(STAGES[:index]):
        raise ApprovalError("approval stage is out of order")
    artifacts = required_artifacts(run_dir, stage)
    record = StageApproval(
        stage=stage,
        artifacts=artifacts,
        approved_at=_utc_now().isoformat(),
        note=note.strip() if note is not None else None,
    )
    if required_artifacts(run_dir, stage) != artifacts:
        raise ApprovalError("approval evidence changed during approval")
    _write_approvals(run_dir, (*records, record))
    return record


def invalidate_stale_approvals(run_dir: Path) -> tuple[StageApproval, ...]:
    run_dir = _validated_run_dir(run_dir)
    records = load_approvals(run_dir)
    valid: list[StageApproval] = []
    for record in records:
        try:
            current = required_artifacts(run_dir, record.stage)
        except ApprovalError:
            break
        if current != record.artifacts:
            break
        valid.append(record)
    result = tuple(valid)
    if result != records:
        _write_approvals(run_dir, result)
    return result


def migrate_checkpoint_base_approval(run_dir: Path, checkpoint: dict[str, Any]) -> None:
    if load_approvals(run_dir) or "base" not in checkpoint.get("completed_jobs", ()):
        return
    roles = {
        item.get("role")
        for item in checkpoint.get("artifacts", ())
        if isinstance(item, dict) and item.get("job_id") == "base"
    }
    if roles != {"canonical", "decoded", "source"}:
        return
    if not any(
        isinstance(item, dict) and item.get("job_id") == "base"
        for item in checkpoint.get("accepted_qa", ())
    ):
        return
    accepted = {
        item.get("path")
        for item in checkpoint.get("accepted_qa", ())
        if isinstance(item, dict)
    }
    records = []
    for stage in STAGES:
        if not set(STAGE_EVIDENCE_PATHS[stage]).issubset(accepted):
            break
        records.append(StageApproval(
            stage=stage,
            artifacts=required_artifacts(run_dir, stage),
            approved_at=_checkpoint_approved_at(checkpoint),
            note="Migrated from accepted checkpoint evidence.",
        ))
    if not records:
        record = StageApproval(
            stage="base",
            artifacts=required_artifacts(run_dir, "base"),
            approved_at=_checkpoint_approved_at(checkpoint),
            note="Migrated from accepted checkpoint base evidence.",
        )
        records.append(record)
    _write_approvals(_validated_run_dir(run_dir), tuple(records))


def _parse_record(value: Any) -> StageApproval:
    if not isinstance(value, dict):
        raise ValueError("approval record is invalid")
    keys = set(value)
    if keys not in (_RECORD_KEYS, _RECORD_KEYS | {"note"}):
        raise ValueError("approval record schema is invalid")
    stage, approved_at, artifacts = value.get("stage"), value.get("approved_at"), value.get("artifacts")
    if stage not in STAGES or not isinstance(approved_at, str) or not isinstance(artifacts, list) or not artifacts:
        raise ValueError("approval record is invalid")
    parsed_time = datetime.fromisoformat(approved_at)
    if parsed_time.tzinfo is None or parsed_time.utcoffset() != timezone.utc.utcoffset(parsed_time):
        raise ValueError("approval timestamp is not UTC")
    note = value.get("note")
    if note is not None and (not isinstance(note, str) or not note.strip()):
        raise ValueError("approval note is invalid")
    parsed_artifacts = []
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != _ARTIFACT_KEYS:
            raise ValueError("approval artifact schema is invalid")
        path, digest = item.get("path"), item.get("sha256")
        if not _safe_relative(path) or not _valid_sha(digest):
            raise ValueError("approval artifact is invalid")
        parsed_artifacts.append(ArtifactHash(path, digest))
    if [item.path for item in parsed_artifacts] != sorted(item.path for item in parsed_artifacts):
        raise ValueError("approval artifacts are not sorted")
    if len({item.path for item in parsed_artifacts}) != len(parsed_artifacts):
        raise ValueError("approval artifact is duplicated")
    return StageApproval(stage, tuple(parsed_artifacts), approved_at, note)


def _required_paths(run_dir: Path, stage: str) -> set[str]:
    if stage == "package":
        return set(STAGE_EVIDENCE_PATHS[stage])
    manifest_path = run_dir / "imagegen-jobs.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("run manifest is unsafe")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    jobs = manifest.get("jobs") if isinstance(manifest, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("run jobs are invalid")
    ids = {
        "base": {"base"},
        "standard-rows": {"idle", "running-right", "running-left", "waving", "jumping", "failed", "waiting", "running", "review"},
        "directions": {"look-cardinals", "look-row-9", "look-row-10"},
    }[stage]
    selected = [job for job in jobs if isinstance(job, dict) and job.get("id") in ids]
    if {job.get("id") for job in selected} != ids or any(job.get("status") != "complete" for job in selected):
        raise ValueError("stage jobs are incomplete")
    paths = set(STAGE_EVIDENCE_PATHS[stage])
    for job in selected:
        output = job.get("output_path", f"decoded/{job['id']}.png")
        paths.add(_manifest_relative(run_dir, output))
        source = job.get("source_path")
        if not isinstance(source, str):
            raise ValueError("job source is missing")
        paths.add(_manifest_relative(run_dir, source))
    if stage == "base":
        paths.add("references/canonical-base.png")
    return paths


def _manifest_relative(run_dir: Path, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("artifact path is invalid")
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(run_dir)
        except ValueError:
            raise ValueError("artifact path escapes run") from None
    text = path.as_posix()
    if not _safe_relative(text):
        raise ValueError("artifact path is unsafe")
    return text


def _validate_qa_decisions(run_dir: Path, stage: str) -> None:
    for relative in STAGE_EVIDENCE_PATHS[stage]:
        if not relative.endswith(".json"):
            continue
        path = run_dir / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("QA decision is missing")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("QA decision is not accepted")
        package_data = {
            "final/pet.json",
            "qa/package-generated/blind-answer-key.json",
            "qa/package-reviewed/blind-consensus.json",
        }
        if relative not in package_data and value.get("ok") is not True:
            raise ValueError("QA decision is not accepted")
        if relative == "final/pet.json" and (
            set(value) != {"id", "displayName", "description", "spriteVersionNumber", "spritesheetPath"}
            or not isinstance(value.get("id"), str)
            or not isinstance(value.get("displayName"), str)
            or not isinstance(value.get("description"), str)
            or value.get("spriteVersionNumber") != 2
            or value.get("spritesheetPath") != "spritesheet.webp"
        ):
            raise ValueError("package manifest is invalid")
        if relative == "qa/package-generated/blind-answer-key.json" and (
            value.get("schema_version") != 3
            or value.get("atlas_sha256") != _sha256_safe(run_dir, "final/spritesheet-extended.webp")
            or not isinstance(value.get("pairs"), list)
            or not value["pairs"]
        ):
            raise ValueError("package blind answer key is invalid")
        if relative == "qa/package-reviewed/blind-consensus.json" and (
            set(value) != {"pairs"} or not isinstance(value.get("pairs"), list) or not value["pairs"]
        ):
            raise ValueError("package blind consensus is invalid")
        if relative == "qa/base/review.json" and (
            set(value) != _BASE_REVIEW_KEYS
            or value.get("job_id") != "base"
            or value.get("adoption_decision") != ACCEPTED_BASE_DECISION
            or value.get("canvas") != {"aspect_ratio": "1:1", "image_size": "1K"}
            or not _valid_sha(value.get("sha256"))
            or not _valid_utc_timestamp(value.get("completed_at"))
        ):
            raise ValueError("base QA decision is invalid")
        if stage == "standard-rows" and relative.startswith("qa/rows/") and relative.endswith("/review.json"):
            job_id = Path(relative).parts[2]
            if (
                set(value) != {"ok", "id", "verdict", "note", "evidence"}
                or value.get("id") != job_id
                or value.get("verdict") != "pass"
                or not isinstance(value.get("note"), str)
                or not value["note"].strip()
            ):
                raise ValueError("standard row review is invalid")
            _validate_bound_evidence(run_dir, value.get("evidence"), (
                f"generated-sources/{job_id}.png",
                f"decoded/{job_id}.png",
                f"qa/rows/{job_id}/deterministic.json",
                f"previews/{job_id}.gif",
            ))
        if stage == "directions" and relative == "qa/directions/cardinals/review.json":
            _validate_cardinal_review(run_dir, value)
        if stage == "directions" and relative == "qa/directions/direction-semantics.json":
            reviews = value.get("reviews")
            expected_rows = {
                "row9": ("000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5"),
                "row10": ("180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5"),
            }
            if (
                set(value) != {"schema_version", "reviews", "ok"}
                or value.get("schema_version") != 1
                or not isinstance(reviews, dict)
                or set(reviews) != set(expected_rows)
                or any(
                    not isinstance(reviews[row], dict)
                    or set(reviews[row]) != {"directions", "evidence"}
                    or not isinstance(reviews[row]["directions"], list)
                    or len(reviews[row]["directions"]) != 8
                    or any(
                        not isinstance(item, dict)
                        or set(item) != {"direction", "verdict", "note"}
                        or item.get("direction") != label
                        or item.get("verdict") != "pass"
                        or not isinstance(item.get("note"), str)
                        or not item["note"].strip()
                        for item, label in zip(reviews[row]["directions"], labels, strict=True)
                    )
                    for row, labels in expected_rows.items()
                )
            ):
                raise ValueError("direction semantic review is invalid")
            _validate_bound_evidence(run_dir, reviews["row9"].get("evidence"), (
                "generated-sources/look-row-9.png",
                "decoded/look-row-9.png",
                "qa/directions/look-row-9-registered.png",
                "qa/directions/look-row-9-registration.json",
                "qa/directions/look-row-9-continuity.json",
                "qa/directions/look-row-9-contact-sheet.png",
            ))
            _validate_bound_evidence(run_dir, reviews["row10"].get("evidence"), (
                "generated-sources/look-row-10.png",
                "decoded/look-row-10.png",
                "qa/directions/look-row-10-registration.json",
                "qa/directions/continuity.json",
                "qa/directions/contact-sheet.png",
            ))
        if stage == "directions" and relative == "qa/directions/look-row-9-registration.json":
            if (
                set(value) != {"ok", "scale", "source_sha256", "registered_sha256"}
                or not isinstance(value.get("scale"), int | float)
                or value["scale"] <= 0
                or not _valid_sha(value.get("source_sha256"))
                or not _valid_sha(value.get("registered_sha256"))
                or value["source_sha256"] != _sha256_safe(run_dir, "decoded/look-row-9.png")
                or value["registered_sha256"] != _sha256_safe(run_dir, "qa/directions/look-row-9-registered.png")
            ):
                raise ValueError("row 9 registration is invalid")
        if stage == "directions" and relative == "qa/directions/look-row-10-registration.json":
            if (
                set(value) != {"ok", "scale", "source_sha256", "registered_row_9_sha256", "atlas_sha256"}
                or not isinstance(value.get("scale"), int | float)
                or value["scale"] <= 0
                or any(not _valid_sha(value.get(key)) for key in ("source_sha256", "registered_row_9_sha256", "atlas_sha256"))
                or value["source_sha256"] != _sha256_safe(run_dir, "decoded/look-row-10.png")
                or value["registered_row_9_sha256"] != _sha256_safe(run_dir, "qa/directions/look-row-9-registered.png")
                or value["atlas_sha256"] != _sha256_safe(run_dir, "final/spritesheet-extended.png")
            ):
                raise ValueError("row 10 registration is invalid")
        if stage == "package" and relative == "qa/package-generated/despill.json":
            if (
                value.get("passes") != 1
                or not _valid_sha(value.get("input_sha256"))
                or not _valid_sha(value.get("output_sha256"))
                or value["input_sha256"] != _sha256_safe(run_dir, "final/package-source.png")
                or value["output_sha256"] != _sha256_safe(run_dir, "final/spritesheet-extended.webp")
            ):
                raise ValueError("package despill evidence is invalid")
        if stage == "package" and relative == "qa/package-generated/validation.json":
            if (
                value.get("sprite_version_number") != 2
                or value.get("width") != 1536
                or value.get("height") != 2288
                or value.get("errors") != []
            ):
                raise ValueError("package validation is invalid")
        if stage == "package" and relative == "qa/package-reviewed/blind-validation.json":
            if value.get("errors") != [] or value.get("unconfirmed") != []:
                raise ValueError("package blind validation is invalid")
        if stage == "package" and relative == "qa/package-reviewed/final-direction-semantics.json":
            directions = value.get("directions")
            if (
                value.get("schema_version") != 1
                or not isinstance(directions, list)
                or len(directions) != 16
                or any(not isinstance(item, dict) or item.get("verdict") != "pass" or item.get("observed") != item.get("expected") for item in directions)
            ):
                raise ValueError("package final direction semantics are invalid")
            _validate_bound_evidence(run_dir, value.get("evidence"), (
                "qa/package-generated/direction-sheet.png",
                "final/spritesheet-extended.webp",
            ))
        if stage == "package" and relative == "qa/package-reviewed/final-visual-review.json":
            if (
                set(value) != {"ok", "verdict", "note", "evidence"}
                or value.get("verdict") != "pass"
                or not isinstance(value.get("note"), str)
                or not value["note"].strip()
            ):
                raise ValueError("package visual review is invalid")
            _validate_bound_evidence(run_dir, value.get("evidence"), (
                "qa/package-generated/blind-sheet.png",
                "final/spritesheet-extended.webp",
            ))


def _valid_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _validate_bound_evidence(run_dir: Path, value: Any, expected: tuple[str, ...]) -> None:
    if not isinstance(value, list) or len(value) != len(expected):
        raise ValueError("review evidence is invalid")
    for item, relative in zip(value, expected, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or item.get("path") != relative
            or item.get("sha256") != _sha256_safe(run_dir, relative)
        ):
            raise ValueError("review evidence is invalid")


def _validate_cardinal_review(run_dir: Path, value: Any = None) -> None:
    if value is None:
        value = _read_json_object(run_dir / "qa/directions/cardinals/review.json")
    directions = value.get("directions") if isinstance(value, dict) else None
    expected = (("000", "up"), ("090", "right"), ("180", "down"), ("270", "left"))
    if set(value) != {"ok", "directions", "evidence"} or value.get("ok") is not True or not isinstance(directions, list) or len(directions) != 4 or any(
        not isinstance(item, dict)
        or set(item) != {"direction", "expected", "verdict", "note"}
        or item.get("direction") != label
        or item.get("expected") != semantic
        or item.get("verdict") != "pass"
        or not isinstance(item.get("note"), str)
        or not item["note"].strip()
        for item, (label, semantic) in zip(directions, expected, strict=True)
    ):
        raise ValueError("cardinal review is invalid")
    _validate_bound_evidence(run_dir, value.get("evidence"), (
        "generated-sources/look-cardinals.png",
        "decoded/look-cardinals.png",
        "qa/directions/cardinals/deterministic.json",
        "qa/directions/cardinals/sheet.png",
        "decoded/look-cardinals-approved.png",
    ))


def _validate_row9_review(run_dir: Path) -> None:
    value = _read_json_object(run_dir / "qa/directions/direction-semantics.json")
    reviews = value.get("reviews")
    if (
        set(value) != {"schema_version", "reviews", "ok"}
        or value.get("schema_version") != 1
        or not isinstance(reviews, dict)
        or "row9" not in reviews
    ):
        raise ValueError("row 9 review is invalid")
    review = reviews["row9"]
    labels = ("000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5")
    directions = review.get("directions") if isinstance(review, dict) else None
    if set(review) != {"directions", "evidence"} or not isinstance(directions, list) or len(directions) != 8 or any(
        not isinstance(item, dict)
        or set(item) != {"direction", "verdict", "note"}
        or item.get("direction") != label
        or item.get("verdict") != "pass"
        or not isinstance(item.get("note"), str)
        or not item["note"].strip()
        for item, label in zip(directions, labels, strict=True)
    ):
        raise ValueError("row 9 review is invalid")
    _validate_bound_evidence(run_dir, review.get("evidence"), (
        "generated-sources/look-row-9.png",
        "decoded/look-row-9.png",
        "qa/directions/look-row-9-registered.png",
        "qa/directions/look-row-9-registration.json",
        "qa/directions/look-row-9-continuity.json",
        "qa/directions/look-row-9-contact-sheet.png",
    ))
    registration = _read_json_object(run_dir / "qa/directions/look-row-9-registration.json")
    if (
        set(registration) != {"ok", "scale", "source_sha256", "registered_sha256"}
        or registration.get("ok") is not True
        or not isinstance(registration.get("scale"), int | float)
        or registration["scale"] <= 0
        or registration.get("source_sha256") != _sha256_safe(run_dir, "decoded/look-row-9.png")
        or registration.get("registered_sha256") != _sha256_safe(run_dir, "qa/directions/look-row-9-registered.png")
    ):
        raise ValueError("row 9 registration is invalid")


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("QA decision is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("QA decision is invalid")
    return value


def _sha256_safe(run_dir: Path, relative: str) -> str:
    path = run_dir / relative
    current = run_dir
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("approval artifact contains a symlink")
    if not path.is_file() or not path.resolve().is_relative_to(run_dir):
        raise ValueError("approval artifact is missing")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_approvals(run_dir: Path, records: tuple[StageApproval, ...]) -> None:
    qa = run_dir / "qa"
    if qa.is_symlink() or (qa.exists() and not qa.is_dir()):
        raise ApprovalError("approval directory is unsafe")
    created = not qa.exists()
    qa.mkdir(exist_ok=True)
    if created:
        _fsync_directory(run_dir)
    payload = {
        "schema_version": 1,
        "approvals": [_record_json(record) for record in records],
    }
    _atomic_json(qa / "approvals.json", payload)


def _record_json(record: StageApproval) -> dict[str, Any]:
    value: dict[str, Any] = {
        "stage": record.stage,
        "artifacts": [{"path": item.path, "sha256": item.sha256} for item in record.artifacts],
        "approved_at": record.approved_at,
    }
    if record.note is not None:
        value["note"] = record.note
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
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


def _validated_run_dir(run_dir: Path) -> Path:
    path = Path(run_dir).absolute()
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise ApprovalError("run directory is unsafe")
    return path


def _safe_relative(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts and path.parts not in ((), (".",))


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _checkpoint_approved_at(checkpoint: dict[str, Any]) -> str:
    for item in checkpoint.get("provenance", ()):
        if isinstance(item, dict) and item.get("job_id") == "base":
            value = item.get("completed_at")
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value)
                    if parsed.tzinfo is not None:
                        return parsed.astimezone(timezone.utc).isoformat()
                except ValueError:
                    pass
    return _utc_now().isoformat()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
