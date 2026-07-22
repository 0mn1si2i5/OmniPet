"""Immutable values used by built-in image generation."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any


_MAX_METADATA_DEPTH = 64
_SECRET_MARKERS = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
    "authorization",
    "privatekey",
    "signingkey",
    "sessioncookie",
    "authcookie",
    "password",
    "credential",
    "secret",
)


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return (
        normalized in {"auth", "bearer", "cookie", "token"}
        or any(marker in normalized for marker in _SECRET_MARKERS)
        or normalized.endswith(("token", "secret"))
    )


def _validate_metadata(value: Any) -> None:
    active: set[int] = set()
    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    while stack:
        item, depth, exiting = stack.pop()
        if exiting:
            active.remove(id(item))
            continue
        if depth > _MAX_METADATA_DEPTH:
            raise ValueError("generated image metadata exceeds maximum nesting")
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise ValueError("recursive generated image metadata is not allowed")
            active.add(identity)
            stack.append((item, depth, True))
            for key, child in reversed(tuple(item.items())):
                if not isinstance(key, str):
                    raise ValueError("generated image metadata keys must be strings")
                if _is_secret_key(key):
                    raise ValueError("generated image metadata must not contain secrets")
                stack.append((child, depth + 1, False))
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            identity = id(item)
            if identity in active:
                raise ValueError("recursive generated image metadata is not allowed")
            active.add(identity)
            stack.append((item, depth, True))
            for child in reversed(item):
                stack.append((child, depth + 1, False))
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("invalid generated image metadata value")


def _freeze_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_metadata(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_metadata(item) for item in value)
    return value


@dataclass(frozen=True)
class GroundingImage:
    path: Path
    role: str = "reference"
    content: bytes | None = field(default=None, repr=False)
    mime_type: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        snapshot = (self.content, self.mime_type, self.content_sha256)
        if snapshot == (None, None, None):
            return
        if (
            not isinstance(self.content, bytes)
            or self.mime_type not in {"image/png", "image/jpeg", "image/webp"}
            or not isinstance(self.content_sha256, str)
            or len(self.content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.content_sha256)
            or hashlib.sha256(self.content).hexdigest() != self.content_sha256
        ):
            if self.mime_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise ValueError("snapshot mime_type is invalid")
            raise ValueError("snapshot content_sha256 does not match content")


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    destination: Path
    run_root: Path
    grounding_images: tuple[GroundingImage, ...] = ()
    aspect_ratio: str = "1:1"
    image_size: str = "1K"
    task: str | None = None
    force: bool = False
    _run_root_identity: tuple[tuple[int, int], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        run_root = self.run_root
        if (
            not isinstance(run_root, Path)
            or not run_root.is_absolute()
            or not run_root.is_dir()
            or run_root.is_symlink()
            or run_root.resolve() != run_root
        ):
            raise ValueError("run_root must be a canonical real directory")
        identities = []
        current = Path(run_root.anchor)
        try:
            for part in run_root.parts[1:]:
                current /= part
                entry = os.stat(current, follow_symlinks=False)
                if not stat.S_ISDIR(entry.st_mode):
                    raise ValueError
                identities.append((entry.st_dev, entry.st_ino))
        except OSError:
            raise ValueError("run_root must be a canonical real directory") from None
        object.__setattr__(self, "_run_root_identity", tuple(identities))
        if not isinstance(self.grounding_images, tuple) or not all(
            isinstance(grounding, GroundingImage)
            for grounding in self.grounding_images
        ):
            raise ValueError("grounding_images must be an immutable tuple of GroundingImage")
        for grounding in self.grounding_images:
            if grounding.content is not None and (
                not grounding.path.is_absolute()
                or ".." in grounding.path.parts
                or not grounding.path.is_relative_to(run_root)
            ):
                raise ValueError("snapshot provenance path must be under run_root")


@dataclass(frozen=True)
class GeneratedImage:
    path: Path
    mime_type: str
    sha256: str
    width: int
    height: int
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, Mapping):
            raise ValueError("generated image metadata must be a mapping")
        _validate_metadata(self.metadata)
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))
