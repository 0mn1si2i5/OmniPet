from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from omnipet.run import EXPECTED_JOB_IDS
from omnipet.security import contains_credential_like_text, is_credential_like_key


_AUTHORITIES = {"identity", "pose-only", "layout-only"}
_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
_RECORD_KEYS = {"path", "sha256", "role", "target_job", "authority"}


def add_generation_guide(
    project: Any,
    job_id: str,
    source: Path,
    *,
    role: str,
    authority: str,
) -> dict[str, str]:
    run_dir = _run_dir(project)
    if job_id not in EXPECTED_JOB_IDS:
        raise ValueError("guide target job is invalid")
    manifest = _read_json(run_dir / "imagegen-jobs.json")
    job = next(
        (item for item in manifest.get("jobs", ()) if item.get("id") == job_id),
        None,
    )
    if job is None or job.get("status") != "pending":
        raise ValueError("guide target job is not pending")
    if (
        not isinstance(role, str)
        or not role.strip()
        or is_credential_like_key(role)
        or contains_credential_like_text(role)
        or (authority != "identity" and "identity" in role.casefold())
    ):
        raise ValueError("guide role is invalid")
    if authority not in _AUTHORITIES:
        raise ValueError("guide authority is invalid")

    source = Path(source).absolute()
    suffix = source.suffix.lower()
    if source.is_symlink() or not source.is_file() or suffix not in _FORMATS:
        raise ValueError("guide source is invalid")
    _validate_image(source, suffix)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    current = list(load_generation_guides(run_dir))
    destination = (
        run_dir
        / "references"
        / "repair-guides"
        / job_id
        / f"{len(current) + 1:02d}-{digest[:12]}{suffix}"
    )
    if destination.exists() or destination.is_symlink():
        raise ValueError("guide destination already exists")
    _write_bytes_atomic(destination, data)
    record = {
        "path": str(destination.relative_to(run_dir)),
        "sha256": digest,
        "role": role.strip(),
        "target_job": job_id,
        "authority": authority,
    }
    registry_path = run_dir / "qa" / "guides.json"
    try:
        registry = _read_registry(run_dir)
        registry["guides"].append(record)
        _write_json_atomic(registry_path, registry)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return record


def load_generation_guides(
    run_dir: Path,
    job_id: str | None = None,
) -> tuple[dict[str, str], ...]:
    run_dir = Path(run_dir)
    registry = _read_registry(run_dir)
    records = []
    for value in registry["guides"]:
        record = _validated_record(run_dir, value)
        if job_id is None or record["target_job"] == job_id:
            records.append(record)
    return tuple(records)


def clear_generation_guides(run_dir: Path, job_id: str) -> None:
    run_dir = Path(run_dir)
    registry_path = run_dir / "qa" / "guides.json"
    if not registry_path.exists() and not registry_path.is_symlink():
        return
    registry = _read_registry(run_dir, validate_records=False)
    retained = []
    for value in registry["guides"]:
        record = _closed_record(value)
        if record["target_job"] != job_id:
            retained.append(record)
            continue
        relative = Path(record["path"])
        expected = Path("references") / "repair-guides" / job_id
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.parent != expected
        ):
            raise ValueError("guide path is invalid")
        _unlink_guide(run_dir, relative, job_id)
    _write_json_atomic(
        registry_path,
        {"schema_version": 1, "guides": retained},
    )
    guide_dir = run_dir / "references" / "repair-guides" / job_id
    try:
        guide_dir.rmdir()
    except OSError:
        pass


def _read_registry(
    run_dir: Path,
    *,
    validate_records: bool = True,
) -> dict[str, Any]:
    registry_path = run_dir / "qa" / "guides.json"
    if not registry_path.exists() and not registry_path.is_symlink():
        return {"schema_version": 1, "guides": []}
    registry = _read_json(registry_path)
    if (
        not isinstance(registry, dict)
        or set(registry) != {"schema_version", "guides"}
        or registry.get("schema_version") != 1
        or not isinstance(registry.get("guides"), list)
    ):
        raise ValueError("guide registry is invalid")
    if validate_records:
        for value in registry["guides"]:
            _validated_record(run_dir, value)
    return registry


def _validated_record(run_dir: Path, value: Any) -> dict[str, str]:
    record = _closed_record(value)
    target_job = record["target_job"]
    relative = Path(record["path"])
    expected_parent = Path("references") / "repair-guides" / target_job
    if (
        target_job not in EXPECTED_JOB_IDS
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.parent != expected_parent
        or relative.suffix.lower() not in _FORMATS
    ):
        raise ValueError("guide record path is invalid")
    path = run_dir / relative
    if (
        path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to(run_dir.resolve())
    ):
        raise ValueError("guide file is unsafe")
    _validate_image(path, relative.suffix.lower())
    if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
        raise ValueError("guide hash changed")
    return record


def _closed_record(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS:
        raise ValueError("guide record schema is invalid")
    if (
        not all(isinstance(value[key], str) for key in _RECORD_KEYS)
        or len(value["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in value["sha256"])
        or not value["role"].strip()
        or is_credential_like_key(value["role"])
        or contains_credential_like_text(value["role"])
        or value["authority"] not in _AUTHORITIES
        or (
            value["authority"] != "identity"
            and "identity" in value["role"].casefold()
        )
    ):
        raise ValueError("guide record is invalid")
    return {key: value[key] for key in ("path", "sha256", "role", "target_job", "authority")}


def _run_dir(project: Any) -> Path:
    path = (
        Path(project.repository_root)
        / ".omnipet"
        / "runs"
        / project.pet_id
    )
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise ValueError("guide run is invalid")
    return path


def _validate_image(path: Path, suffix: str) -> None:
    try:
        with Image.open(path) as image:
            if image.format != _FORMATS[suffix]:
                raise ValueError("guide image format is invalid")
            image.verify()
    except (OSError, UnidentifiedImageError):
        raise ValueError("guide image is invalid") from None


def _unlink_guide(run_dir: Path, relative: Path, job_id: str) -> None:
    run_root = run_dir.resolve()
    repair_root = run_dir / "references" / "repair-guides"
    guide_root = repair_root / job_id
    for directory in (repair_root, guide_root):
        try:
            entry = os.lstat(directory)
        except OSError:
            raise ValueError("guide cleanup path is invalid") from None
        if (
            stat.S_ISLNK(entry.st_mode)
            or not stat.S_ISDIR(entry.st_mode)
            or not directory.resolve().is_relative_to(run_root)
        ):
            raise ValueError("guide cleanup path is unsafe")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(guide_root, flags)
    try:
        try:
            entry = os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(entry.st_mode):
            raise ValueError("guide cleanup file is unsafe")
        path = guide_root / relative.name
        if not path.resolve().is_relative_to(guide_root.resolve()):
            raise ValueError("guide cleanup file is unsafe")
        os.unlink(relative.name, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("guide JSON is unsafe")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, value: Any) -> None:
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
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
    finally:
        temporary.unlink(missing_ok=True)
