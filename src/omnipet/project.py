from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
import warnings

from omnipet.security import is_credential_like_key


class ProjectValidationError(ValueError):
    """Raised when durable pet project data violates the project contract."""


@dataclass(frozen=True)
class PetReference:
    path: Path
    role: str


@dataclass(frozen=True)
class ProjectLocator:
    repository_root: Path
    project_root: Path
    expected_pet_id: str | None


@dataclass(frozen=True)
class PetProject:
    pet_id: str
    root: Path
    repository_root: Path
    display_name: str
    description: str
    style_preset: str
    style_notes: str
    brief_path: Path
    references: tuple[PetReference, ...]
    image_generation_model: str
    image_generation_quality: str
    image_generation_deprecated: bool
    minimum_sprite_version: int
    hatch_engine_requirements: Mapping[str, Any]
    spritesheet_path: Path
    manifest_path: Path
    canonical_base_path: Path | None

    @property
    def reference_paths(self) -> tuple[Path, ...]:
        return tuple(reference.path for reference in self.references)


def locate_pet_project(repo_root: Path, pet_id_or_path: str | Path) -> ProjectLocator:
    supplied_root = _validated_repository_root(repo_root)
    canonical_root = supplied_root
    selector_text = str(pet_id_or_path)
    selector = Path(pet_id_or_path)

    if (supplied_root / "pet.yaml").is_file():
        expected_pet_id = None if selector_text == "." else _valid_pet_id(selector_text)
        return ProjectLocator(canonical_root, canonical_root, expected_pet_id)

    is_path_selector = (
        selector.is_absolute()
        or len(selector.parts) != 1
        or selector_text.startswith("./")
    )
    if is_path_selector or _is_standalone_directory(supplied_root, selector):
        project_root = _standalone_project_root(supplied_root, selector)
        return ProjectLocator(project_root, project_root, None)

    pet_id = _valid_pet_id(selector_text)
    pets_root = supplied_root / "pets"
    project_root = pets_root / pet_id
    if pets_root.is_symlink() or project_root.is_symlink() or not project_root.is_dir():
        raise ProjectValidationError("invalid pet project root")
    return ProjectLocator(supplied_root, project_root, pet_id)


def load_pet_project(repo_root: Path, pet_id_or_path: str | Path) -> PetProject:
    locator = locate_pet_project(repo_root, pet_id_or_path)
    pet_root = locator.project_root

    manifest_path = pet_root / "pet.yaml"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ProjectValidationError("missing pet manifest")

    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except RecursionError as error:
        raise ProjectValidationError("pet manifest is too deeply nested") from error
    if not isinstance(data, dict):
        raise ProjectValidationError("pet manifest must be a mapping")
    schema_version = data.get("schema_version")
    pet_id = data.get("id")
    if (
        type(schema_version) is not int
        or schema_version != 1
        or not isinstance(pet_id, str)
        or _is_invalid_pet_id(pet_id)
        or (locator.expected_pet_id is not None and pet_id != locator.expected_pet_id)
    ):
        raise ProjectValidationError("pet manifest identity does not match")

    for key in ("display_name", "description"):
        _required_string(data.get(key), key)

    style = _required_mapping(data, "style")
    image_generation, image_generation_deprecated = _image_generation_config(data)
    hatch_engine = _required_mapping(data, "hatch_engine")
    package = _required_mapping(data, "package")
    _required_string(style.get("preset"), "style preset")
    style_notes = style.get("notes", "")
    if not isinstance(style_notes, str):
        raise ProjectValidationError("invalid style")
    minimum_sprite_version = hatch_engine.get("minimum_sprite_version")
    if type(minimum_sprite_version) is not int or minimum_sprite_version < 2:
        raise ProjectValidationError("invalid hatch engine")

    hatch_requirements = {
        key: value
        for key, value in hatch_engine.items()
        if key != "minimum_sprite_version"
    }
    try:
        _validate_structure(hatch_requirements)
        frozen_hatch_requirements = _freeze_mapping(hatch_requirements)
    except RecursionError as error:
        raise ProjectValidationError("hatch requirements are too deeply nested") from error

    brief_path = _project_path(pet_root, data.get("brief"), must_exist=True)

    references = data.get("references")
    if not isinstance(references, list):
        raise ProjectValidationError("references must be a list")
    pet_references: list[PetReference] = []
    seen_references: set[Path] = set()
    for reference in references:
        if not isinstance(reference, dict):
            raise ProjectValidationError("invalid reference")
        _required_string(reference.get("role"), "reference role")
        path = _project_path(pet_root, reference.get("path"), must_exist=True)
        if path in seen_references:
            raise ProjectValidationError("duplicate reference path")
        seen_references.add(path)
        pet_references.append(PetReference(path=path, role=reference["role"]))

    spritesheet_path = _project_path(pet_root, package.get("spritesheet"))
    package_manifest_path = _project_path(pet_root, package.get("manifest"))
    approved = data.get("approved")
    if approved is None:
        canonical_base_path = None
    elif not isinstance(approved, dict):
        raise ProjectValidationError("invalid approved assets")
    elif approved.get("canonical_base") is None:
        canonical_base_path = None
    else:
        canonical_base_path = _project_path(
            pet_root,
            approved["canonical_base"],
            must_exist=True,
        )
    durable_inputs = (
        manifest_path,
        brief_path,
        *(reference.path for reference in pet_references),
        *((canonical_base_path,) if canonical_base_path is not None else ()),
    )
    _validate_package_paths(
        pet_root,
        (spritesheet_path, package_manifest_path),
        durable_inputs,
    )

    return PetProject(
        pet_id=pet_id,
        root=pet_root,
        repository_root=locator.repository_root,
        display_name=data["display_name"],
        description=data["description"],
        style_preset=style["preset"],
        style_notes=style_notes,
        brief_path=brief_path,
        references=tuple(pet_references),
        image_generation_model=image_generation["model"],
        image_generation_quality=image_generation["quality"],
        image_generation_deprecated=image_generation_deprecated,
        minimum_sprite_version=minimum_sprite_version,
        hatch_engine_requirements=frozen_hatch_requirements,
        spritesheet_path=spritesheet_path,
        manifest_path=package_manifest_path,
        canonical_base_path=canonical_base_path,
    )


def _valid_pet_id(value: str) -> str:
    if _is_invalid_pet_id(value):
        raise ProjectValidationError("invalid pet id")
    return value


def _is_invalid_pet_id(value: str) -> bool:
    path = Path(value)
    return path.is_absolute() or len(path.parts) != 1 or value in {"", ".", ".."}


def _validated_repository_root(repo_root: Path) -> Path:
    supplied = Path(repo_root)
    if ".." in supplied.parts:
        raise ProjectValidationError("repository root must not traverse")
    absolute = supplied if supplied.is_absolute() else Path.cwd() / supplied
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise ProjectValidationError("invalid repository root")
    return current


def _standalone_project_root(repository_root: Path, selector: Path) -> Path:
    if ".." in selector.parts:
        raise ProjectValidationError("project path must not traverse")
    project_root = _walk_standalone_path(repository_root, selector)
    manifest = project_root / "pet.yaml"
    if manifest.is_symlink() or not manifest.is_file():
        raise ProjectValidationError("missing pet manifest")
    return project_root


def _is_standalone_directory(repository_root: Path, selector: Path) -> bool:
    try:
        project_root = _walk_standalone_path(repository_root, selector)
    except ProjectValidationError:
        return False
    manifest = project_root / "pet.yaml"
    return not manifest.is_symlink() and manifest.is_file()


def _walk_standalone_path(repository_root: Path, selector: Path) -> Path:
    if ".." in selector.parts:
        raise ProjectValidationError("project path must not traverse")
    if selector.is_absolute():
        current = Path(selector.anchor)
        parts = selector.parts[1:]
    else:
        current = repository_root.resolve()
        parts = selector.parts
    for part in parts:
        current /= part
        if current.is_symlink():
            raise ProjectValidationError("project paths cannot contain symlinks")
        if not current.exists():
            raise ProjectValidationError("project path component is missing")
    if not current.is_dir():
        raise ProjectValidationError("invalid pet project root")
    return current


def _required_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ProjectValidationError(f"missing {key}")
    return value


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectValidationError(f"missing {name}")
    return value


def _freeze_mapping(
    value: Mapping[str, Any], active: set[int] | None = None
) -> Mapping[str, Any]:
    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        raise ProjectValidationError("recursive configuration is not allowed")
    if not all(isinstance(key, str) for key in value):
        raise ProjectValidationError("configuration keys must be strings")
    active.add(identity)
    try:
        return MappingProxyType(
            {key: _freeze_value(item, active) for key, item in value.items()}
        )
    finally:
        active.remove(identity)


def _freeze_value(value: Any, active: set[int]) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value, active)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in active:
            raise ProjectValidationError("recursive configuration is not allowed")
        active.add(identity)
        try:
            return tuple(_freeze_value(item, active) for item in value)
        finally:
            active.remove(identity)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ProjectValidationError("invalid configuration value")


def _image_generation_config(data: dict[str, Any]) -> tuple[dict[str, str], bool]:
    current = data.get("image_generation")
    legacy = data.get("provider")
    if current is not None and legacy is not None:
        raise ProjectValidationError("image generation configuration is ambiguous")
    if current is not None:
        if (
            not isinstance(current, dict)
            or set(current) != {"model", "quality"}
            or current.get("model") != "gpt-image-2"
            or current.get("quality") not in {"low", "medium", "high"}
        ):
            raise ProjectValidationError("invalid image generation configuration")
        return current, False
    if not isinstance(legacy, dict) or legacy.get("name") != "openai":
        raise ProjectValidationError("legacy provider must be openai")
    legacy_options = {key: value for key, value in legacy.items() if key != "name"}
    try:
        _validate_structure(legacy_options, reject_secrets=True)
    except RecursionError as error:
        raise ProjectValidationError("legacy provider options are too deeply nested") from error
    warnings.warn(
        "provider.name is deprecated; use image_generation",
        DeprecationWarning,
        stacklevel=2,
    )
    return {"model": "gpt-image-2", "quality": "low"}, True


def _validate_structure(value: Any, *, reject_secrets: bool = False) -> None:
    active: set[int] = set()
    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    while stack:
        item, depth, exiting = stack.pop()
        if exiting:
            active.remove(id(item))
            continue
        if depth > 64:
            raise ProjectValidationError("provider options exceed maximum nesting")

        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise ProjectValidationError("recursive configuration is not allowed")
            active.add(identity)
            stack.append((item, depth, True))
            for key, child in reversed(tuple(item.items())):
                if not isinstance(key, str):
                    raise ProjectValidationError("configuration keys must be strings")
                if reject_secrets and is_credential_like_key(key):
                    raise ProjectValidationError("provider secrets are not allowed")
                stack.append((child, depth + 1, False))
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            identity = id(item)
            if identity in active:
                raise ProjectValidationError("recursive configuration is not allowed")
            active.add(identity)
            stack.append((item, depth, True))
            for child in reversed(item):
                stack.append((child, depth + 1, False))
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ProjectValidationError("invalid configuration value")


def _validate_package_paths(
    pet_root: Path,
    destinations: tuple[Path, Path],
    durable_inputs: tuple[Path, ...],
) -> None:
    dist_root = pet_root / "dist"
    for destination in destinations:
        if destination == dist_root or not destination.is_relative_to(dist_root):
            raise ProjectValidationError("package destinations must be beneath dist")

    first, second = destinations
    if first == second or first.is_relative_to(second) or second.is_relative_to(first):
        raise ProjectValidationError("package destinations must not overlap")
    for destination in destinations:
        for durable_input in durable_inputs:
            if (
                destination == durable_input
                or destination.is_relative_to(durable_input)
                or durable_input.is_relative_to(destination)
            ):
                raise ProjectValidationError("package destination overlaps durable input")


def _project_path(pet_root: Path, value: Any, *, must_exist: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ProjectValidationError("invalid project path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProjectValidationError("project path must be relative")

    candidate = pet_root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise ProjectValidationError("project paths cannot contain symlinks")

    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(pet_root.resolve()):
        raise ProjectValidationError("project path escapes pet root")
    if must_exist and not candidate.is_file():
        raise ProjectValidationError("project file is missing")
    return candidate
