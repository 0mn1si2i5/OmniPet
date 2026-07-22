import base64
import hashlib
import io
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import openai
from PIL import Image

from omnipet.openai_images import (
    OpenAIImageGenerator,
    OpenAIRequestError,
    OpenAIResponseError,
    OpenAIValidationError,
    build_client,
    get_api_key,
)
from omnipet.generation import GroundingImage, ImageRequest


def image_bytes(image_format="PNG", size=(2, 2)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=image_format)
    return buffer.getvalue()


def response(data=None):
    return SimpleNamespace(
        data=[SimpleNamespace(
            b64_json=base64.b64encode(data or image_bytes()).decode("ascii")
        )]
    )


class FakeImages:
    def __init__(self, result=None, error=None):
        self.result = result or response()
        self.error = error
        self.calls = []

    def edit(self, **kwargs):
        self.calls.append(("edit", kwargs))
        if self.error:
            raise self.error
        return self.result

    def generate(self, **kwargs):
        self.calls.append(("generate", kwargs))
        if self.error:
            raise self.error
        return self.result


class FakeClient:
    def __init__(self, images=None):
        self.images = images or FakeImages()
        self.options = []
        self.max_retries = 2

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        self.max_retries = kwargs["max_retries"]
        return self


class RetryCapableFakeClient(FakeClient):
    pass


class BareFakeClient:
    def __init__(self, images=None):
        self.images = images or FakeImages()


class ReturningFakeClient:
    def __init__(self, configured):
        self.images = FakeImages(error=AssertionError("unconfigured client used"))
        self.configured = configured
        self.options = []

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        self.configured.max_retries = kwargs["max_retries"]
        return self.configured


class IgnoringOptionsFakeClient(FakeClient):
    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self


class OpenAIImageGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.generated = self.root / "generated-sources"
        self.generated.mkdir()

    def request(self, destination=None, references=(), force=False):
        return ImageRequest(
            prompt="Draw Sample Pet waving",
            destination=destination or self.generated / "result.png",
            run_root=self.root,
            grounding_images=references,
            aspect_ratio="21:9",
            image_size="2K",
            task="idle",
            force=force,
        )

    def snapshot(self, role="identity anchor", image_format="PNG", mime_type="image/png"):
        data = image_bytes(image_format)
        path = self.root / "identity.png"
        path.write_bytes(data)
        return GroundingImage(
            path,
            role,
            data,
            mime_type,
            hashlib.sha256(data).hexdigest(),
        )

    def test_edit_uses_exact_parameters_and_immutable_snapshot(self):
        snapshot = self.snapshot()
        images = FakeImages()

        result = OpenAIImageGenerator(client=FakeClient(images)).edit(
            self.request(references=(snapshot,))
        )

        method, call = images.calls[0]
        self.assertEqual(method, "edit")
        self.assertEqual(set(call), {
            "model", "prompt", "image", "quality", "size", "output_format",
            "background", "n",
        })
        self.assertEqual(call["model"], "gpt-image-2")
        self.assertEqual(call["prompt"], (
            "Draw Sample Pet waving\nReference image 1 role: identity anchor."
        ))
        self.assertIsInstance(call["image"], io.BytesIO)
        self.assertEqual(call["image"].name, "reference-01.png")
        self.assertEqual(call["image"].getvalue(), snapshot.content)
        self.assertEqual(call["quality"], "low")
        self.assertEqual(call["size"], "1536x1024")
        self.assertEqual(call["output_format"], "png")
        self.assertEqual(call["background"], "opaque")
        self.assertEqual(call["n"], 1)
        self.assertNotIn("input_fidelity", call)
        self.assertEqual(result.path.read_bytes(), image_bytes())
        self.assertEqual(result.width, 2)
        self.assertEqual(result.height, 2)
        self.assertEqual(result.sha256, hashlib.sha256(image_bytes()).hexdigest())

    def test_generate_uses_exact_parameters_without_image(self):
        images = FakeImages()

        OpenAIImageGenerator(client=FakeClient(images)).generate(self.request())

        self.assertEqual(images.calls, [("generate", {
            "model": "gpt-image-2",
            "prompt": "Draw Sample Pet waving",
            "quality": "low",
            "size": "1536x1024",
            "output_format": "png",
            "background": "opaque",
            "n": 1,
        })])

    def test_generate_rejects_references_and_edit_requires_them_before_sdk(self):
        for operation, request in (
            ("generate", self.request(references=(self.snapshot(),))),
            ("edit", self.request(destination=self.generated / "edit.png")),
        ):
            with self.subTest(operation=operation):
                client = RetryCapableFakeClient()
                with self.assertRaises(OpenAIValidationError):
                    getattr(OpenAIImageGenerator(client=client), operation)(request)
                self.assertEqual(client.options, [])
                self.assertEqual(client.images.calls, [])

    def test_model_allowlist_rejects_arbitrary_models(self):
        for model in ("gpt-image-1", "custom-model", ""):
            with self.subTest(model=model):
                with self.assertRaises(OpenAIValidationError):
                    OpenAIImageGenerator(model=model)

    def test_allowed_quality_is_forwarded_without_other_parameter_changes(self):
        images = FakeImages()

        OpenAIImageGenerator(client=FakeClient(images), quality="high").generate(
            self.request()
        )

        self.assertEqual(images.calls[0][1]["quality"], "high")

    def test_rejects_unknown_quality_before_sdk(self):
        with self.assertRaises(OpenAIValidationError):
            OpenAIImageGenerator(quality="ultra")

    def test_build_client_disables_sdk_retries_and_sets_timeout(self):
        sentinel = object()
        with mock.patch("omnipet.openai_images.openai.OpenAI", return_value=sentinel) as factory:
            self.assertIs(build_client("secret-value"), sentinel)

        factory.assert_called_once_with(
            api_key="secret-value", max_retries=0, timeout=120.0
        )

    def test_injected_client_is_forced_to_one_sdk_attempt(self):
        images = FakeImages(error=RuntimeError("one failure"))
        client = RetryCapableFakeClient(images)

        with self.assertRaises(OpenAIRequestError):
            OpenAIImageGenerator(client=client).generate(self.request())

        self.assertEqual(client.options, [{"max_retries": 0}])
        self.assertEqual(len(images.calls), 1)

    def test_injected_client_without_with_options_is_rejected_before_sdk(self):
        images = FakeImages()
        client = BareFakeClient(images)

        with self.assertRaises(OpenAIValidationError):
            OpenAIImageGenerator(client=client).generate(self.request())

        self.assertEqual(images.calls, [])
        self.assertFalse(self.request().destination.exists())

    def test_request_uses_client_returned_with_retries_disabled(self):
        configured = FakeClient()
        client = ReturningFakeClient(configured)

        OpenAIImageGenerator(client=client).generate(self.request())

        self.assertEqual(client.options, [{"max_retries": 0}])
        self.assertEqual(len(configured.images.calls), 1)
        self.assertEqual(client.images.calls, [])

    def test_injected_client_that_ignores_retry_option_is_rejected(self):
        images = FakeImages()
        client = IgnoringOptionsFakeClient(images)

        with self.assertRaises(OpenAIValidationError):
            OpenAIImageGenerator(client=client).generate(self.request())

        self.assertEqual(client.options, [{"max_retries": 0}])
        self.assertEqual(images.calls, [])

    def test_png_jpeg_and_webp_snapshots_keep_exact_bytes_and_safe_names(self):
        snapshots = (
            self.snapshot("canonical", "PNG", "image/png"),
            self.snapshot("portrait", "JPEG", "image/jpeg"),
            self.snapshot("guide", "WEBP", "image/webp"),
        )
        images = FakeImages()

        OpenAIImageGenerator(client=FakeClient(images)).edit(
            self.request(references=snapshots)
        )

        uploads = images.calls[0][1]["image"]
        self.assertEqual(
            [upload.name for upload in uploads],
            ["reference-01.png", "reference-02.jpg", "reference-03.webp"],
        )
        self.assertEqual(
            [upload.getvalue() for upload in uploads],
            [snapshot.content for snapshot in snapshots],
        )

    def test_corrupt_mime_mismatch_or_changed_hash_is_rejected_before_sdk(self):
        corrupt = b"not an image"
        valid = self.snapshot()
        cases = (
            GroundingImage(
                self.root / "corrupt.png", "canonical", corrupt, "image/png",
                hashlib.sha256(corrupt).hexdigest(),
            ),
            self.snapshot("portrait", "JPEG", "image/png"),
            mock.Mock(
                content=valid.content,
                mime_type=valid.mime_type,
                content_sha256="0" * 64,
                role=valid.role,
            ),
        )
        for index, snapshot in enumerate(cases):
            with self.subTest(index=index):
                images = FakeImages()
                request = mock.Mock(
                    prompt="Draw", destination=self.generated / f"bad-{index}.png",
                    run_root=self.root, grounding_images=(snapshot,),
                    aspect_ratio="21:9", image_size="2K", task="idle", force=False,
                )
                with self.assertRaises(OpenAIValidationError):
                    OpenAIImageGenerator(client=FakeClient(images)).edit(request)
                self.assertEqual(images.calls, [])

    def test_edit_never_reopens_snapshot_provenance_path(self):
        snapshot = self.snapshot()
        snapshot.path.write_bytes(image_bytes("JPEG"))
        images = FakeImages()

        with mock.patch.object(Path, "open", side_effect=AssertionError("path reopened")), \
             mock.patch.object(Path, "read_bytes", side_effect=AssertionError("path reopened")):
            OpenAIImageGenerator(client=FakeClient(images)).edit(
                self.request(references=(snapshot,))
            )

        self.assertEqual(images.calls[0][1]["image"].getvalue(), snapshot.content)

    def test_requires_only_openai_api_key(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "official"}, clear=True):
            self.assertEqual(get_api_key(), "official")
        with mock.patch.dict(os.environ, {"gpt_key": "legacy"}, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                get_api_key()

    def test_missing_key_and_sdk_errors_are_sanitized(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(OpenAIValidationError) as raised:
                OpenAIImageGenerator().generate(self.request())
        self.assertEqual(str(raised.exception), "OpenAI image request validation failed")

        images = FakeImages(error=RuntimeError("secret prompt and key"))
        with self.assertRaises(OpenAIRequestError) as raised:
            OpenAIImageGenerator(client=FakeClient(images)).generate(self.request())
        self.assertEqual(str(raised.exception), "OpenAI image request failed")
        self.assertIsNone(raised.exception.__cause__)

    def test_requires_exactly_one_valid_base64_png(self):
        invalid = {
            "missing": SimpleNamespace(data=[]),
            "multiple": SimpleNamespace(data=response().data * 2),
            "base64": SimpleNamespace(data=[SimpleNamespace(b64_json="not base64")]),
            "corrupt": response(data=b"not an image"),
            "jpeg": response(data=image_bytes("JPEG")),
        }
        for name, sdk_response in invalid.items():
            with self.subTest(name=name):
                output = self.generated / f"{name}.png"
                generator = OpenAIImageGenerator(
                    client=FakeClient(FakeImages(sdk_response))
                )
                with self.assertRaises(OpenAIResponseError):
                    generator.generate(self.request(destination=output))
                self.assertFalse(output.exists())

    def test_destination_is_contained_and_symlink_free(self):
        external = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, external)
        linked = self.generated / "linked"
        linked.symlink_to(external, target_is_directory=True)
        images = FakeImages()
        generator = OpenAIImageGenerator(client=FakeClient(images))
        for output in (
            external / "out.png",
            self.root / "out.png",
            linked / "out.png",
            self.generated / ".." / "escaped" / "out.png",
            self.generated / "out.jpg",
        ):
            with self.subTest(output=output):
                with self.assertRaises(OpenAIValidationError):
                    generator.generate(self.request(destination=output))
        self.assertEqual(images.calls, [])

    def test_atomic_non_force_install_preserves_racing_file(self):
        output = self.generated / "race.png"

        def race(_source, destination, **kwargs):
            descriptor = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                dir_fd=kwargs["dst_dir_fd"],
            )
            with os.fdopen(descriptor, "wb") as racer:
                racer.write(b"racer")
            raise FileExistsError(destination)

        with mock.patch("omnipet.openai_images.os.link", side_effect=race):
            with self.assertRaises(OpenAIResponseError):
                OpenAIImageGenerator(client=FakeClient()).generate(
                    self.request(destination=output)
                )
        self.assertEqual(output.read_bytes(), b"racer")
        self.assertEqual(list(self.generated.glob(".*.tmp-*")), [])

    def test_parent_swap_cannot_redirect_output(self):
        parent = self.generated / "job"
        parent.mkdir()
        output = parent / "result.png"
        external = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, external)
        images = FakeImages()
        original_generate = images.generate

        def swap_parent(**kwargs):
            parent.rename(self.generated / "displaced-job")
            parent.symlink_to(external, target_is_directory=True)
            return original_generate(**kwargs)

        images.generate = swap_parent
        with self.assertRaises(OpenAIResponseError):
            OpenAIImageGenerator(client=FakeClient(images)).generate(
                self.request(destination=output)
            )
        self.assertFalse((external / "result.png").exists())
        self.assertEqual(
            (self.generated / "displaced-job" / "result.png").read_bytes(),
            image_bytes(),
        )

    def test_replaced_run_root_is_rejected_before_sdk_without_external_write(self):
        request = self.request()
        displaced = self.root.parent / f"{self.root.name}-displaced"
        self.root.rename(displaced)
        self.addCleanup(shutil.rmtree, displaced, True)
        self.root.mkdir()
        (self.root / "generated-sources").mkdir()
        images = FakeImages()

        with self.assertRaises(OpenAIValidationError):
            OpenAIImageGenerator(client=FakeClient(images)).generate(request)

        self.assertEqual(images.calls, [])
        self.assertFalse(request.destination.exists())
        self.assertFalse((displaced / "generated-sources" / "result.png").exists())

    def test_replaced_run_root_ancestor_is_rejected_before_sdk(self):
        repository = self.root / "repository"
        run_root = repository / ".omnipet" / "runs" / "pet"
        (run_root / "generated-sources").mkdir(parents=True)
        request = ImageRequest(
            prompt="Draw", destination=run_root / "generated-sources" / "result.png",
            run_root=run_root, task="base",
        )
        displaced = self.root / "repository-displaced"
        repository.rename(displaced)
        (run_root / "generated-sources").mkdir(parents=True)
        images = FakeImages()

        with self.assertRaises(OpenAIValidationError):
            OpenAIImageGenerator(client=FakeClient(images)).generate(request)

        self.assertEqual(images.calls, [])
        self.assertFalse(request.destination.exists())
        self.assertFalse(
            (displaced / ".omnipet" / "runs" / "pet" / "generated-sources" / "result.png").exists()
        )

    def test_run_root_swap_during_request_cannot_redirect_or_report_success(self):
        output = self.generated / "result.png"
        displaced = self.root.parent / f"{self.root.name}-during-request"
        images = FakeImages()
        original_generate = images.generate

        def swap_root(**kwargs):
            self.root.rename(displaced)
            self.addCleanup(shutil.rmtree, displaced, True)
            self.root.mkdir()
            (self.root / "generated-sources").mkdir()
            return original_generate(**kwargs)

        images.generate = swap_root
        with self.assertRaises(OpenAIResponseError):
            OpenAIImageGenerator(client=FakeClient(images)).generate(
                self.request(destination=output)
            )

        self.assertFalse(output.exists())
        self.assertEqual(
            (displaced / "generated-sources" / "result.png").read_bytes(),
            image_bytes(),
        )

    def test_force_atomically_replaces_existing_output(self):
        output = self.generated / "result.png"
        output.write_bytes(b"old")

        result = OpenAIImageGenerator(client=FakeClient()).generate(
            self.request(destination=output, force=True)
        )

        self.assertEqual(result.path.read_bytes(), image_bytes())


if __name__ == "__main__":
    unittest.main()
