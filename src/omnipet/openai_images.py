"""Built-in image generation using the official OpenAI Images API."""

import base64
import binascii
import hashlib
import io
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Mapping

import openai
from PIL import Image, UnidentifiedImageError

from omnipet.diagnostics import SafeDiagnostic, openai_diagnostic
from omnipet.generation import GeneratedImage, ImageRequest


MODEL = "gpt-image-2"
QUALITY = "low"
TIMEOUT = 120.0
_QUALITIES = {"low", "medium", "high"}
_SIZES = {("1:1", "1K"): "1024x1024", ("21:9", "2K"): "1536x1024"}


class OpenAIImageError(RuntimeError):
    """Base class for sanitized image generation failures."""

    diagnostic: SafeDiagnostic


class OpenAIValidationError(OpenAIImageError):
    """Local validation failed before an API request was sent."""

    def __init__(self, diagnostic: SafeDiagnostic | None = None) -> None:
        self.diagnostic = diagnostic or SafeDiagnostic("local-validation")
        super().__init__("OpenAI image request validation failed")


class OpenAIRequestError(OpenAIImageError):
    """The API request failed."""

    def __init__(self, diagnostic: SafeDiagnostic | None = None) -> None:
        self.diagnostic = diagnostic or SafeDiagnostic("provider-request")
        super().__init__("OpenAI image request failed")


class OpenAIResponseError(OpenAIImageError):
    """The response or output could not be safely processed."""

    def __init__(self, diagnostic: SafeDiagnostic | None = None) -> None:
        self.diagnostic = diagnostic or SafeDiagnostic("provider-response")
        super().__init__("OpenAI image response failed")


class OpenAIImageGenerator:
    """Generate one PNG per explicit request with SDK retries disabled."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: Any = None,
        model: str = MODEL,
        quality: str = QUALITY,
    ) -> None:
        if model != MODEL or quality not in _QUALITIES:
            raise OpenAIValidationError()
        self.api_key = api_key
        self.client = client
        self.model = model
        self.quality = quality

    def generate(self, request: ImageRequest) -> GeneratedImage:
        return self._request(request, edit=False)

    def edit(self, request: ImageRequest) -> GeneratedImage:
        return self._request(request, edit=True)

    def _request(self, request: ImageRequest, *, edit: bool) -> GeneratedImage:
        parent_descriptor = None
        destination = Path(request.destination)
        try:
            try:
                parent_descriptor = _validate_destination(
                    request, destination, request.force
                )
                prompt, images = _map_snapshots(request)
                if edit != bool(images):
                    raise ValueError("operation does not match references")
                size = _provider_size(request)
            except Exception:
                raise OpenAIValidationError() from None

            try:
                if self.client is None:
                    api_key = self.api_key
                    if api_key is None:
                        try:
                            api_key = get_api_key()
                        except ValueError:
                            raise OpenAIValidationError(
                                SafeDiagnostic("missing-credentials")
                            ) from None
                    client = build_client(api_key)
                else:
                    with_options = getattr(self.client, "with_options", None)
                    if not callable(with_options):
                        raise ValueError("injected client cannot disable retries")
                    client = with_options(max_retries=0)
                    if getattr(client, "max_retries", None) != 0:
                        raise ValueError("configured client did not disable retries")
                    images_api = getattr(client, "images", None)
                    operation = getattr(images_api, "edit" if edit else "generate", None)
                    if not callable(operation):
                        raise ValueError("configured client lacks Images operation")
            except OpenAIValidationError:
                raise
            except Exception:
                raise OpenAIValidationError() from None

            parameters = {
                "model": self.model,
                "prompt": prompt,
                "quality": self.quality,
                "size": size,
                "output_format": "png",
                "background": "opaque",
                "n": 1,
            }
            try:
                if edit:
                    image: io.BytesIO | list[io.BytesIO]
                    image = images[0] if len(images) == 1 else images
                    response = client.images.edit(image=image, **parameters)
                else:
                    response = client.images.generate(**parameters)
            except Exception as error:
                raise OpenAIRequestError(openai_diagnostic(error)) from None

            try:
                image_data, width, height = _extract_png(response)
                installed_descriptor = _write_image(
                    destination, parent_descriptor, image_data, request.force
                )
                try:
                    _verify_output_identity(
                        request, destination, installed_descriptor, image_data
                    )
                finally:
                    os.close(installed_descriptor)
            except Exception:
                raise OpenAIResponseError() from None
        finally:
            if parent_descriptor is not None:
                os.close(parent_descriptor)

        return GeneratedImage(
            destination,
            "image/png",
            hashlib.sha256(image_data).hexdigest(),
            width,
            height,
            {
                "model": self.model,
                "quality": self.quality,
                "task": request.task,
                "requested_size": size,
            },
        )


def get_api_key() -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or not api_key.strip():
        raise ValueError("Set OPENAI_API_KEY")
    return api_key


def build_client(api_key: str) -> openai.OpenAI:
    return openai.OpenAI(api_key=api_key, max_retries=0, timeout=TIMEOUT)


def _provider_size(request: ImageRequest) -> str:
    try:
        return _SIZES[(request.aspect_ratio, request.image_size)]
    except KeyError:
        raise ValueError("unsupported image geometry") from None


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _map_snapshots(request: ImageRequest) -> tuple[str, list[io.BytesIO]]:
    formats = {
        "image/png": ("PNG", "png"),
        "image/jpeg": ("JPEG", "jpg"),
        "image/webp": ("WEBP", "webp"),
    }
    prompt_lines = [request.prompt]
    uploads = []
    for index, grounding in enumerate(request.grounding_images, 1):
        data = grounding.content
        expected = formats.get(grounding.mime_type)
        if data is None or expected is None:
            raise ValueError("unsupported snapshot")
        if grounding.content_sha256 != hashlib.sha256(data).hexdigest():
            raise ValueError("snapshot hash mismatch")
        try:
            with Image.open(io.BytesIO(data)) as image:
                image_format = image.format
                image.verify()
        except (OSError, SyntaxError, UnidentifiedImageError):
            raise ValueError("invalid snapshot") from None
        expected_format, extension = expected
        if image_format != expected_format:
            raise ValueError("snapshot MIME mismatch")
        role = " ".join(grounding.role.split()) or "reference"
        prompt_lines.append(f"Reference image {index} role: {role}.")
        upload = io.BytesIO(data)
        upload.name = f"reference-{index:02d}.{extension}"
        uploads.append(upload)
    return "\n".join(prompt_lines), uploads


def _extract_png(response: Any) -> tuple[bytes, int, int]:
    data = _get(response, "data")
    if not isinstance(data, list) or len(data) != 1:
        raise ValueError("response must contain one image")
    try:
        image_data = base64.b64decode(_get(data[0], "b64_json"), validate=True)
    except (binascii.Error, TypeError, ValueError):
        raise ValueError("invalid image encoding") from None
    try:
        with Image.open(io.BytesIO(image_data)) as image:
            image_format = image.format
            width, height = image.size
            image.verify()
    except (OSError, SyntaxError, UnidentifiedImageError):
        raise ValueError("invalid response image") from None
    if image_format != "PNG":
        raise ValueError("response image must be PNG")
    return image_data, width, height


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_destination(request: ImageRequest, destination: Path, force: bool) -> int:
    run_root = request.run_root
    if destination.suffix.lower() != ".png":
        raise ValueError("output must use .png")
    generated_root = run_root / "generated-sources"
    if (
        not destination.is_absolute()
        or ".." in destination.parts
        or not _contained(destination, generated_root)
    ):
        raise ValueError("output is outside generated-sources")
    _require_secure_directory_operations()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        current = _open_run_root(request)
        descriptors.append(current)
        for part in destination.parent.relative_to(run_root).parts:
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=current)
                child = os.open(part, flags, dir_fd=current)
            descriptors.append(child)
            current = child
        try:
            destination_stat = os.stat(
                destination.name, dir_fd=current, follow_symlinks=False
            )
        except FileNotFoundError:
            destination_stat = None
        if destination_stat is not None:
            if stat.S_ISLNK(destination_stat.st_mode):
                raise ValueError("output must not be a symlink")
            if not force:
                raise FileExistsError(destination)
        descriptors.pop()
        return current
    except (FileExistsError, ValueError):
        raise
    except OSError:
        raise ValueError("unable to establish secure output path") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _require_secure_directory_operations() -> None:
    dir_fd_names = {operation.__name__ for operation in os.supports_dir_fd}
    follow_names = {operation.__name__ for operation in os.supports_follow_symlinks}
    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "pread")
        or not {"open", "mkdir", "stat", "rename", "link", "unlink"}.issubset(
            dir_fd_names
        )
        or "stat" not in follow_names
    ):
        raise ValueError("platform lacks secure directory operations")


def _open_run_root(request: ImageRequest) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        current = os.open(request.run_root.anchor, flags)
        descriptors.append(current)
        parts = request.run_root.parts[1:]
        if len(parts) != len(request._run_root_identity):
            raise ValueError("run root identity mismatch")
        for part, expected_identity in zip(
            parts, request._run_root_identity, strict=True
        ):
            child = os.open(part, flags, dir_fd=current)
            descriptors.append(child)
            entry = os.fstat(child)
            if (entry.st_dev, entry.st_ino) != expected_identity:
                raise ValueError("run root identity mismatch")
            current = child
        descriptors.pop()
        return current
    except (OSError, ValueError):
        raise ValueError("run root identity mismatch") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_image(
    destination: Path, parent_descriptor: int, image_data: bytes, force: bool
) -> int:
    temporary_name = f".{destination.name}.tmp-{secrets.token_hex(8)}"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
        dir_fd=parent_descriptor,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(image_data)
            output.flush()
            os.fsync(output.fileno())
        if force:
            os.rename(
                temporary_name,
                destination.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        else:
            try:
                os.link(
                    temporary_name,
                    destination.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
            except FileExistsError:
                raise FileExistsError(destination) from None
        return os.open(
            destination.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
    finally:
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass


def _verify_output_identity(
    request: ImageRequest,
    destination: Path,
    installed_descriptor: int,
    expected_data: bytes,
) -> None:
    lexical_descriptor = None
    try:
        lexical_descriptor = _open_lexical_output(request, destination)
        installed_stat = os.fstat(installed_descriptor)
        lexical_stat = os.fstat(lexical_descriptor)
        expected_hash = hashlib.sha256(expected_data).digest()
        if (
            (installed_stat.st_dev, installed_stat.st_ino)
            != (lexical_stat.st_dev, lexical_stat.st_ino)
            or _descriptor_hash(installed_descriptor) != expected_hash
            or _descriptor_hash(lexical_descriptor) != expected_hash
        ):
            raise ValueError("output identity mismatch")
    except (OSError, ValueError):
        raise ValueError("output identity mismatch") from None
    finally:
        if lexical_descriptor is not None:
            os.close(lexical_descriptor)


def _open_lexical_output(request: ImageRequest, destination: Path) -> int:
    run_root = request.run_root
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        current = _open_run_root(request)
        descriptors.append(current)
        for part in destination.parent.relative_to(run_root).parts:
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
        return os.open(
            destination.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _descriptor_hash(descriptor: int) -> bytes:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(descriptor, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.digest()
