from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression
from packaging.version import InvalidVersion, Version
from PIL import Image, UnidentifiedImageError

from omnipet import __version__
from omnipet.hatch.validation import ValidateAtlasConfig, validate_atlas
from omnipet.package import PackageError, check_package
from omnipet.security import (
    MAX_SCANNED_TEXT_LENGTH,
    contains_absolute_path_text,
    contains_credential_like_text,
    contains_prohibited_release_text,
)


class PublicReleaseError(RuntimeError):
    """Raised when a public release bundle violates the closed contract."""


_REQUIRED_FILES = frozenset({
    "pet.json",
    "spritesheet.webp",
    "preview.webp",
    "README.md",
    "LICENSE-ASSETS",
})
_OPTIONAL_FILES = frozenset({"README.zh-CN.md"})
_RELEASE_KEYS = frozenset({
    "schemaVersion",
    "petId",
    "version",
    "omnipetVersion",
    "spriteVersionNumber",
    "files",
    "license",
})
_PET_KEYS = frozenset({
    "id",
    "displayName",
    "description",
    "spriteVersionNumber",
    "spritesheetPath",
})
_SEMANTIC_VERSION = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:(?:0|[1-9]\d*)|(?:\d*[A-Za-z-][0-9A-Za-z-]*))"
    r"(?:\.(?:(?:0|[1-9]\d*)|(?:\d*[A-Za-z-][0-9A-Za-z-]*)))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_PET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_SPDX_LINE = re.compile(
    r"(?m)^SPDX-License-Identifier:\s*([A-Za-z0-9][A-Za-z0-9.+-]*)\s*$"
)


def export_public_release(project: Any, output: Path) -> Path:
    destination = _export_destination(project, output)
    try:
        check_package(project)
    except (PackageError, OSError, ValueError):
        raise PublicReleaseError("public release requires a current passing package") from None

    sources = {
        "pet.json": project.manifest_path,
        "spritesheet.webp": project.spritesheet_path,
        "preview.webp": project.preview_source_path,
        "README.md": project.release_readme_path,
        "LICENSE-ASSETS": project.asset_license_path,
    }
    if project.release_readme_zh_cn_path is not None:
        sources["README.zh-CN.md"] = project.release_readme_zh_cn_path
    for source in sources.values():
        _safe_source_file(project.root, source)

    release_root = destination.parent
    stage = Path(tempfile.mkdtemp(prefix=".release-stage-", dir=release_root))
    backup: Path | None = None
    try:
        for name, source in sources.items():
            shutil.copyfile(source, stage / name)
            _fsync_file(stage / name)
        record = {
            "schemaVersion": 1,
            "petId": project.pet_id,
            "version": project.release_version,
            "omnipetVersion": __version__,
            "spriteVersionNumber": 2,
            "files": {
                name: f"sha256:{_sha256(stage / name)}"
                for name in sorted(sources)
            },
            "license": project.asset_license,
        }
        _write_canonical_json(stage / "release.json", record)
        _fsync_directory(stage)
        verify_public_release(stage)

        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise PublicReleaseError("public release destination is unsafe")
            backup = Path(tempfile.mkdtemp(
                prefix=".release-backup-", dir=release_root
            ))
            backup.rmdir()
            os.replace(destination, backup)
        try:
            _fsync_directory(release_root)
            os.replace(stage, destination)
            _fsync_directory(release_root)
        except BaseException:
            if backup is not None and backup.exists():
                if destination.exists():
                    shutil.rmtree(destination)
                os.replace(backup, destination)
                _fsync_directory(release_root)
            elif destination.exists():
                os.replace(destination, stage)
                _fsync_directory(release_root)
            raise
        if backup is not None:
            shutil.rmtree(backup)
            _fsync_directory(release_root)
        return destination
    except PublicReleaseError:
        raise
    except (OSError, ValueError):
        raise PublicReleaseError("public release export failed") from None
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if (
            backup is not None
            and backup.exists()
            and destination.exists()
        ):
            shutil.rmtree(backup, ignore_errors=True)


def verify_public_release(bundle_directory: Path) -> dict[str, Any]:
    bundle = _safe_bundle_directory(bundle_directory)
    actual = _bundle_files(bundle)
    if "release.json" not in actual:
        raise PublicReleaseError("release record is missing")
    release = _read_json(bundle / "release.json", "release record")
    if set(release) != _RELEASE_KEYS:
        raise PublicReleaseError("release record schema is invalid")
    files = release.get("files")
    if not isinstance(files, dict) or not all(
        isinstance(name, str) and isinstance(digest, str)
        for name, digest in files.items()
    ):
        raise PublicReleaseError("release file record is invalid")
    declared = set(files)
    if (
        not _REQUIRED_FILES.issubset(declared)
        or not declared.issubset(_REQUIRED_FILES | _OPTIONAL_FILES)
        or actual != declared | {"release.json"}
    ):
        raise PublicReleaseError("release bundle file set is invalid")
    if (
        release.get("schemaVersion") != 1
        or type(release.get("spriteVersionNumber")) is not int
        or release["spriteVersionNumber"] != 2
        or not isinstance(release.get("petId"), str)
        or _PET_ID.fullmatch(release["petId"]) is None
        or not isinstance(release.get("version"), str)
        or _SEMANTIC_VERSION.fullmatch(release["version"]) is None
        or not isinstance(release.get("omnipetVersion"), str)
    ):
        raise PublicReleaseError("release identity is invalid")
    try:
        Version(release["omnipetVersion"])
    except InvalidVersion:
        raise PublicReleaseError("release engine version is invalid") from None
    license_id = _canonical_spdx(release.get("license"))

    expected_record = json.dumps(
        release, sort_keys=True, separators=(",", ":")
    ) + "\n"
    if (bundle / "release.json").read_text(encoding="utf-8") != expected_record:
        raise PublicReleaseError("release record is not canonical JSON")
    for name, expected in files.items():
        if _HASH.fullmatch(expected) is None:
            raise PublicReleaseError("release file hash is invalid")
        if expected != f"sha256:{_sha256(bundle / name)}":
            raise PublicReleaseError("release file hash does not match")

    manifest = _read_json(bundle / "pet.json", "pet manifest")
    if (
        set(manifest) != _PET_KEYS
        or manifest.get("id") != release["petId"]
        or manifest.get("spriteVersionNumber") != 2
        or manifest.get("spritesheetPath") != "spritesheet.webp"
        or not isinstance(manifest.get("displayName"), str)
        or not manifest["displayName"].strip()
        or not isinstance(manifest.get("description"), str)
    ):
        raise PublicReleaseError("public pet manifest is invalid")

    _verify_atlas(bundle / "spritesheet.webp")
    _verify_preview(bundle / "preview.webp")
    license_text = _read_portable_text(bundle / "LICENSE-ASSETS")
    match = _SPDX_LINE.search(license_text)
    if match is None or _canonical_spdx(match.group(1)) != license_id:
        raise PublicReleaseError("asset license is inconsistent")
    for name in sorted(actual):
        if (
            name.endswith((".json", ".md"))
            or name == "LICENSE-ASSETS"
        ):
            _read_portable_text(bundle / name)
    return release


def _export_destination(project: Any, output: Path) -> Path:
    supplied = Path(output)
    if ".." in supplied.parts:
        raise PublicReleaseError("public release destination must not traverse")
    destination = supplied if supplied.is_absolute() else Path.cwd() / supplied
    release_root = Path(project.repository_root).absolute() / "release-work"
    if release_root.is_symlink() or (
        release_root.exists() and not release_root.is_dir()
    ):
        raise PublicReleaseError("release-work directory is unsafe")
    release_root.mkdir(parents=True, exist_ok=True)
    if destination.parent != release_root or destination.name in {"", ".", ".."}:
        raise PublicReleaseError("public releases must be exported under release-work")
    if destination.is_symlink():
        raise PublicReleaseError("public release destination is unsafe")
    return destination


def _safe_source_file(project_root: Path, source: Path) -> None:
    root = Path(project_root).absolute()
    path = Path(source).absolute()
    if not path.is_relative_to(root):
        raise PublicReleaseError("public release source is outside the pet project")
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise PublicReleaseError("public release source is unsafe")
    if not path.is_file():
        raise PublicReleaseError("public release source is missing")


def _safe_bundle_directory(value: Path) -> Path:
    supplied = Path(value)
    if ".." in supplied.parts:
        raise PublicReleaseError("release bundle path must not traverse")
    bundle = supplied if supplied.is_absolute() else Path.cwd() / supplied
    current = Path(bundle.anchor)
    for part in bundle.parts[1:]:
        current /= part
        if current.is_symlink():
            raise PublicReleaseError("release bundle path is unsafe")
    if not bundle.is_dir():
        raise PublicReleaseError("release bundle is missing")
    return bundle


def _bundle_files(bundle: Path) -> set[str]:
    names: set[str] = set()
    try:
        entries = list(bundle.iterdir())
    except OSError:
        raise PublicReleaseError("release bundle cannot be read") from None
    for path in entries:
        if (
            path.name in {"", ".", ".."}
            or path.is_symlink()
            or not path.is_file()
        ):
            raise PublicReleaseError("release bundle contains unsafe content")
        names.add(path.name)
    return names


def _verify_atlas(path: Path) -> None:
    try:
        with Image.open(path) as opened:
            opened.load()
            if (
                opened.format != "WEBP"
                or opened.mode != "RGBA"
                or opened.size != (1536, 2288)
            ):
                raise PublicReleaseError("public atlas format is invalid")
        validation = validate_atlas(ValidateAtlasConfig(
            atlas=path,
            require_v2=True,
        ))
    except PublicReleaseError:
        raise
    except (OSError, ValueError, UnidentifiedImageError):
        raise PublicReleaseError("public atlas is unreadable") from None
    if not validation.ok:
        raise PublicReleaseError("public atlas validation failed")


def _verify_preview(path: Path) -> None:
    try:
        with Image.open(path) as opened:
            opened.load()
            if opened.format != "WEBP" or opened.width < 1 or opened.height < 1:
                raise PublicReleaseError("public preview format is invalid")
    except PublicReleaseError:
        raise
    except (OSError, ValueError, UnidentifiedImageError):
        raise PublicReleaseError("public preview is unreadable") from None


def _canonical_spdx(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise PublicReleaseError("asset license is invalid")
    try:
        canonical = str(canonicalize_license_expression(value))
    except InvalidLicenseExpression:
        raise PublicReleaseError("asset license is invalid") from None
    if canonical != value or canonical.lower().startswith("licenseref-"):
        raise PublicReleaseError("asset license is invalid")
    return canonical


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PublicReleaseError(f"{label} is invalid") from None
    if not isinstance(value, dict):
        raise PublicReleaseError(f"{label} is invalid")
    return value


def _read_portable_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_SCANNED_TEXT_LENGTH:
            raise PublicReleaseError("public release text is too large")
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise PublicReleaseError("public release text is invalid") from None
    if (
        contains_credential_like_text(value)
        or contains_absolute_path_text(value)
        or contains_prohibited_release_text(value)
    ):
        raise PublicReleaseError("public release text contains private material")
    return value


def _write_canonical_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _fsync_file(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
