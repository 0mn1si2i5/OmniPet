from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_VERDICT_KEYS = {
    "schema_version",
    "warning_ids",
    "reviewer",
    "disposition",
    "note",
    "visual_evidence",
}
_RESOLUTION_KEYS = (
    (_VERDICT_KEYS - {"schema_version"}) | {"source_report", "created_at"}
)
_WARNING_KEYS = {"id", "text"}
_EVIDENCE_KEYS = {"path", "sha256"}
_WARNING_ID = re.compile(r"[a-z0-9][a-z0-9.:-]{2,199}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VISUAL_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


class ResolutionError(RuntimeError):
    """Raised when a warning resolution is unsafe, incomplete, or stale."""


def create_warning_resolution(
    run_dir: Path,
    report_path: str,
    verdict_file: Path,
) -> Path:
    run_dir = _run_root(run_dir)
    report_relative, report = _source_report(run_dir, report_path)
    warning_ids = _warning_ids(report)
    if not warning_ids:
        raise ResolutionError("source report has no warnings")
    verdict_path = Path(verdict_file).absolute()
    if (
        verdict_path.is_symlink()
        or not verdict_path.is_file()
        or verdict_path.suffix != ".json"
        or verdict_path.name in {".", ".."}
    ):
        raise ResolutionError("resolution verdict is unsafe")
    verdict = _json_object(verdict_path, "resolution verdict")
    if set(verdict) != _VERDICT_KEYS or verdict.get("schema_version") != 1:
        raise ResolutionError("resolution verdict schema is invalid")
    submitted_ids = verdict.get("warning_ids")
    if (
        not isinstance(submitted_ids, list)
        or any(not isinstance(value, str) for value in submitted_ids)
        or len(set(submitted_ids)) != len(submitted_ids)
        or set(submitted_ids) != set(warning_ids)
    ):
        raise ResolutionError("resolution warning coverage is invalid")
    reviewer = verdict.get("reviewer")
    note = verdict.get("note")
    if (
        not isinstance(reviewer, str)
        or not reviewer.strip()
        or len(reviewer.strip()) > 160
        or not isinstance(note, str)
        or not note.strip()
        or len(note.strip()) > 4000
    ):
        raise ResolutionError("resolution review is incomplete")
    disposition = verdict.get("disposition")
    if disposition not in {"pass", "fail"}:
        raise ResolutionError("resolution disposition is invalid")
    evidence = _validated_evidence(run_dir, verdict.get("visual_evidence"))
    payload = {
        "schema_version": 1,
        "source_report": {
            "path": report_relative,
            "sha256": _sha256(run_dir / report_relative),
        },
        "warning_ids": sorted(submitted_ids),
        "reviewer": reviewer.strip(),
        "disposition": disposition,
        "note": note.strip(),
        "visual_evidence": evidence,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    destination = run_dir / "qa/resolutions" / verdict_path.name
    if destination.resolve().parent != (run_dir / "qa/resolutions").resolve():
        raise ResolutionError("resolution destination is unsafe")
    _atomic_json(destination, payload)
    return destination


def validate_report_resolutions(run_dir: Path, report_path: str) -> tuple[Path, ...]:
    run_dir = _run_root(run_dir)
    report_relative, report = _source_report(run_dir, report_path)
    current_ids = _warning_ids(report)
    if not current_ids:
        return ()
    directory = run_dir / "qa/resolutions"
    if directory.is_symlink() or not directory.is_dir():
        raise ResolutionError("passing warning resolution is missing")
    matches: list[Path] = []
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise ResolutionError("resolution store is unsafe")
        value = _json_object(path, "warning resolution")
        if set(value) != _RESOLUTION_KEYS | {"schema_version"}:
            raise ResolutionError("warning resolution schema is invalid")
        source = value.get("source_report")
        if not isinstance(source, dict) or set(source) != {"path", "sha256"}:
            raise ResolutionError("warning resolution source is invalid")
        if source.get("path") != report_relative:
            continue
        matches.append(path)
        _validate_stored_resolution(run_dir, value, report_relative, current_ids)
    if len(matches) != 1:
        raise ResolutionError("warning resolution coverage is ambiguous")
    return tuple(matches)


def resolution_artifact_paths(run_dir: Path, report_path: str) -> set[str]:
    paths = validate_report_resolutions(run_dir, report_path)
    result: set[str] = set()
    for path in paths:
        value = _json_object(path, "warning resolution")
        result.add(path.relative_to(run_dir).as_posix())
        result.update(item["path"] for item in value["visual_evidence"])
    return result


def _validate_stored_resolution(
    run_dir: Path,
    value: dict[str, Any],
    report_relative: str,
    current_ids: tuple[str, ...],
) -> None:
    source = value["source_report"]
    if (
        value.get("schema_version") != 1
        or source.get("sha256") != _sha256(run_dir / report_relative)
        or value.get("disposition") != "pass"
        or not isinstance(value.get("reviewer"), str)
        or not value["reviewer"].strip()
        or not isinstance(value.get("note"), str)
        or not value["note"].strip()
    ):
        raise ResolutionError("warning resolution is stale or incomplete")
    ids = value.get("warning_ids")
    if (
        not isinstance(ids, list)
        or any(not isinstance(item, str) for item in ids)
        or len(set(ids)) != len(ids)
        or set(ids) != set(current_ids)
    ):
        raise ResolutionError("warning resolution coverage is invalid")
    created_at = value.get("created_at")
    try:
        parsed = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        raise ResolutionError("warning resolution timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ResolutionError("warning resolution timestamp is invalid")
    _validated_evidence(run_dir, value.get("visual_evidence"))


def _source_report(run_dir: Path, value: str) -> tuple[str, dict[str, Any]]:
    relative = _safe_relative(value)
    if relative is None or not relative.startswith("qa/") or not relative.endswith(".json"):
        raise ResolutionError("source report path is unsafe")
    report = _json_object(_safe_file(run_dir, relative), "source report")
    if relative == "qa/package-generated/continuity.json":
        atlas = _safe_file(run_dir, "final/spritesheet-extended.webp")
        if report.get("atlasSha256") != _sha256(atlas):
            raise ResolutionError("source report atlas binding is stale")
    return relative, report


def _warning_ids(report: dict[str, Any]) -> tuple[str, ...]:
    warnings = report.get("warnings")
    if not isinstance(warnings, list):
        raise ResolutionError("source report warnings are invalid")
    result = []
    for warning in warnings:
        if (
            not isinstance(warning, dict)
            or set(warning) != _WARNING_KEYS
            or not isinstance(warning.get("id"), str)
            or _WARNING_ID.fullmatch(warning["id"]) is None
            or not isinstance(warning.get("text"), str)
            or not warning["text"].strip()
        ):
            raise ResolutionError("source report warning is invalid")
        result.append(warning["id"])
    if len(set(result)) != len(result):
        raise ResolutionError("source report warning IDs are duplicated")
    return tuple(result)


def _validated_evidence(run_dir: Path, value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ResolutionError("visual evidence is missing")
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _EVIDENCE_KEYS:
            raise ResolutionError("visual evidence schema is invalid")
        relative = _safe_relative(item.get("path"))
        digest = item.get("sha256")
        if (
            relative is None
            or Path(relative).suffix.lower() not in _VISUAL_SUFFIXES
            or relative in seen
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or _sha256(_safe_file(run_dir, relative)) != digest
        ):
            raise ResolutionError("visual evidence is stale or invalid")
        seen.add(relative)
        result.append({"path": relative, "sha256": digest})
    return result


def _run_root(path: Path) -> Path:
    value = Path(path).absolute()
    if value.is_symlink() or not value.is_dir() or value.resolve() != value:
        raise ResolutionError("run directory is unsafe")
    return value


def _safe_relative(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _safe_file(run_dir: Path, relative: str) -> Path:
    path = run_dir / relative
    current = run_dir
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise ResolutionError("bound evidence is unsafe")
    if not path.is_file() or not path.resolve().is_relative_to(run_dir):
        raise ResolutionError("bound evidence is missing")
    return path


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ResolutionError(f"{label} is invalid") from None
    if not isinstance(value, dict):
        raise ResolutionError(f"{label} is invalid")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    directory = path.parent
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ResolutionError("resolution directory is unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
