import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from omnipet.cli import main
from omnipet import __version__

from tests.test_project import VALID_PET_YAML


class PetCliTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_root = Path(self.temp_dir.name).resolve()
        pet_root = self.repo_root / "pets" / "sample-pet"
        (pet_root / "references").mkdir(parents=True)
        (pet_root / "approved").mkdir()
        (pet_root / "brief.md").write_text("# Sample Pet\n", encoding="utf-8")
        (pet_root / "references" / "portrait.jpg").write_bytes(b"portrait")
        (pet_root / "approved" / "canonical-base.png").write_bytes(b"base")
        (pet_root / "pet.yaml").write_text(VALID_PET_YAML, encoding="utf-8")

    def test_pet_validate_prints_sanitized_json(self):
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            result = main(
                ["pet", "validate", "sample-pet", "--repo-root", str(self.repo_root)]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(
            payload,
            {
                "ok": True,
                "pet_id": "sample-pet",
                "project_root": str(self.repo_root / "pets" / "sample-pet"),
            },
        )

    def test_top_level_version_exits_without_a_command(self):
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), f"omnipet {__version__}\n")

    def test_module_entrypoint_prints_version(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

        result = subprocess.run(
            [sys.executable, "-m", "omnipet.cli", "--version"],
            cwd=self.repo_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"omnipet {__version__}\n")
        self.assertEqual(result.stderr, "")

    def test_pet_validate_accepts_standalone_repo_root(self):
        standalone = self.repo_root / "standalone"
        (self.repo_root / "pets" / "sample-pet").rename(standalone)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            result = main(
                ["pet", "validate", ".", "--repo-root", str(standalone)]
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "ok": True,
                "pet_id": "sample-pet",
                "project_root": str(standalone.resolve()),
            },
        )

    def test_pet_validate_accepts_standalone_path(self):
        standalone = self.repo_root / "standalone"
        (self.repo_root / "pets" / "sample-pet").rename(standalone)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            result = main(["pet", "validate", str(standalone.resolve())])

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["project_root"], str(standalone.resolve()))

    def test_pet_validate_error_does_not_expose_environment(self):
        secret = "provider-secret-that-must-not-leak"
        stderr = io.StringIO()

        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}), redirect_stderr(stderr):
            result = main(
                ["pet", "validate", "missing", "--repo-root", str(self.repo_root)]
            )

        payload = json.loads(stderr.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(payload, {"ok": False, "error": "invalid pet project"})
        self.assertNotIn(secret, stderr.getvalue())

    def test_pet_validate_sanitizes_extra_image_generation_secret(self):
        secret = "bearer-private-that-must-not-leak"
        pet_yaml = self.repo_root / "pets" / "sample-pet" / "pet.yaml"
        content = pet_yaml.read_text(encoding="utf-8").replace(
            "  quality: low\n",
            f"  quality: low\n  authorization: {secret}\n",
        )
        pet_yaml.write_text(content, encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(
                ["pet", "validate", "sample-pet", "--repo-root", str(self.repo_root)]
            )

        self.assertEqual(result, 1)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"ok": False, "error": "invalid pet project"},
        )
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn(secret, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_pet_validate_sanitizes_cyclic_yaml_alias(self):
        pet_yaml = self.repo_root / "pets" / "sample-pet" / "pet.yaml"
        content = pet_yaml.read_text(encoding="utf-8").replace(
            "image_generation:\n  model: gpt-image-2\n  quality: low\n",
            "image_generation: &image_generation\n  model: gpt-image-2\n  quality: low\n  recursive: *image_generation\n",
        )
        pet_yaml.write_text(content, encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(
                ["pet", "validate", "sample-pet", "--repo-root", str(self.repo_root)]
            )

        self.assertEqual(result, 1)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"ok": False, "error": "invalid pet project"},
        )
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_pet_validate_sanitizes_deeply_nested_yaml(self):
        marker = "deep-private-input-must-not-leak"
        pet_yaml = self.repo_root / "pets" / "sample-pet" / "pet.yaml"
        nested = "image_generation:\n  model: gpt-image-2\n  quality: low\n  options:\n"
        for depth in range(1200):
            nested += f"{'  ' * (depth + 2)}level_{depth}:\n"
        nested += f"{'  ' * 1202}value: {marker}\n"
        content = VALID_PET_YAML[: VALID_PET_YAML.index("image_generation:\n")] + nested
        pet_yaml.write_text(content, encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(
                ["pet", "validate", "sample-pet", "--repo-root", str(self.repo_root)]
            )

        self.assertEqual(result, 1)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"ok": False, "error": "invalid pet project"},
        )
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn(marker, stderr.getvalue())
        self.assertNotIn("level_1199", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_pet_validate_sanitizes_unsafe_package_destination(self):
        marker = "durable-input-name-must-not-leak"
        pet_yaml = self.repo_root / "pets" / "sample-pet" / "pet.yaml"
        content = pet_yaml.read_text(encoding="utf-8").replace(
            "dist/spritesheet.webp",
            f"references/{marker}.webp",
        )
        pet_yaml.write_text(content, encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(
                ["pet", "validate", "sample-pet", "--repo-root", str(self.repo_root)]
            )

        self.assertEqual(result, 1)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"ok": False, "error": "invalid pet project"},
        )
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn(marker, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_pet_validate_sanitizes_invalid_required_metadata(self):
        marker = "invalid-required-content-must-not-leak"
        pet_yaml = self.repo_root / "pets" / "sample-pet" / "pet.yaml"
        content = (
            pet_yaml.read_text(encoding="utf-8")
            .replace("schema_version: 1", "schema_version: true")
            .replace("display_name: Sample Pet", f'display_name: "   {marker}   "')
            .replace("minimum_sprite_version: 2", "minimum_sprite_version: false")
        )
        pet_yaml.write_text(content, encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(
                ["pet", "validate", "sample-pet", "--repo-root", str(self.repo_root)]
            )

        self.assertEqual(result, 1)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"ok": False, "error": "invalid pet project"},
        )
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn(marker, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
