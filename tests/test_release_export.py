import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omnipet.package import PackageError
from omnipet.public_release import PublicReleaseError, export_public_release


class PublicReleaseExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        pet_root = self.root / "pets/test-pet"
        dist = pet_root / "dist"
        dist.mkdir(parents=True)
        (dist / "pet.json").write_text(json.dumps({
            "id": "test-pet",
            "displayName": "Test Pet",
            "description": "A friendly pet.",
            "spriteVersionNumber": 2,
            "spritesheetPath": "spritesheet.webp",
        }) + "\n", encoding="utf-8")
        (dist / "spritesheet.webp").write_bytes(b"webp-atlas")
        (pet_root / "preview-source.webp").write_bytes(b"webp-preview")
        (pet_root / "README.md").write_text(
            "# Test Pet\n\nInstall this friendly pet.\n", encoding="utf-8"
        )
        (pet_root / "README.zh-CN.md").write_text(
            "# 测试宠物\n\n安装这只友好的宠物。\n", encoding="utf-8"
        )
        (pet_root / "LICENSE-ASSETS").write_text(
            "SPDX-License-Identifier: CC-BY-NC-4.0\n", encoding="utf-8"
        )
        self.project = SimpleNamespace(
            pet_id="test-pet",
            display_name="Test Pet",
            description="A friendly pet.",
            root=pet_root,
            repository_root=self.root,
            manifest_path=dist / "pet.json",
            spritesheet_path=dist / "spritesheet.webp",
            release_version="1.2.3",
            asset_license="CC-BY-NC-4.0",
            release_readme_path=pet_root / "README.md",
            release_readme_zh_cn_path=pet_root / "README.zh-CN.md",
            asset_license_path=pet_root / "LICENSE-ASSETS",
            preview_source_path=pet_root / "preview-source.webp",
        )
        (self.root / "release-work").mkdir()

    def export(self, name="test-pet-1.2.3"):
        destination = self.root / "release-work" / name
        with patch("omnipet.public_release.check_package", return_value={
            "id": "test-pet", "spriteVersionNumber": 2,
        }) as check, patch(
            "omnipet.public_release.verify_public_release", return_value=None
        ) as verify:
            result = export_public_release(self.project, destination)
        check.assert_called_once_with(self.project)
        verify.assert_called_once()
        verified_stage = verify.call_args.args[0]
        self.assertEqual(verified_stage.parent, destination.parent)
        self.assertTrue(verified_stage.name.startswith(".release-stage-"))
        return result

    def test_export_is_exact_allowlist_and_canonical_deterministic_record(self):
        first = self.export("first")
        second = self.export("second")

        expected = {
            "pet.json", "spritesheet.webp", "preview.webp", "README.md",
            "README.zh-CN.md", "LICENSE-ASSETS", "release.json",
        }
        self.assertEqual({path.name for path in first.iterdir()}, expected)
        self.assertEqual(
            {path.name: path.read_bytes() for path in first.iterdir()},
            {path.name: path.read_bytes() for path in second.iterdir()},
        )
        record = json.loads((first / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(set(record), {
            "schemaVersion", "petId", "version", "omnipetVersion",
            "spriteVersionNumber", "files", "license",
        })
        self.assertEqual(set(record["files"]), expected - {"release.json"})
        self.assertEqual(
            (first / "release.json").read_text(encoding="utf-8"),
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
        )
        for name, digest in record["files"].items():
            self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
            import hashlib
            self.assertEqual(
                digest,
                "sha256:" + hashlib.sha256((first / name).read_bytes()).hexdigest(),
            )

    def test_optional_chinese_readme_is_formally_omitted(self):
        self.project.release_readme_zh_cn_path = None
        bundle = self.export()
        self.assertFalse((bundle / "README.zh-CN.md").exists())
        record = json.loads((bundle / "release.json").read_text())
        self.assertNotIn("README.zh-CN.md", record["files"])

    def test_export_requires_current_passing_package_check(self):
        destination = self.root / "release-work/test-pet-1.2.3"
        with patch(
            "omnipet.public_release.check_package",
            side_effect=PackageError("stale package approval"),
        ):
            with self.assertRaises(PublicReleaseError):
                export_public_release(self.project, destination)
        self.assertFalse(destination.exists())

    def test_export_rejects_unsafe_sources_and_destination_collisions(self):
        original = self.project.release_readme_path
        original.unlink()
        os.symlink(self.project.asset_license_path, original)
        with self.assertRaises(PublicReleaseError):
            self.export()

        original.unlink()
        original.write_text("# Test Pet\n")
        outside = self.root / "public/test-pet"
        with patch("omnipet.public_release.check_package", return_value={}):
            with self.assertRaises(PublicReleaseError):
                export_public_release(self.project, outside)

    def test_failed_atomic_replacement_preserves_previous_bundle(self):
        destination = self.root / "release-work/test-pet-1.2.3"
        destination.mkdir()
        (destination / "old").write_text("previous")
        real_replace = os.replace

        def fail_install(source, target):
            if Path(target) == destination and Path(source).name.startswith(".release-stage-"):
                raise OSError("install failed")
            return real_replace(source, target)

        with patch("omnipet.public_release.check_package", return_value={}), patch(
            "omnipet.public_release.verify_public_release", return_value=None
        ), patch("omnipet.public_release.os.replace", side_effect=fail_install):
            with self.assertRaises(PublicReleaseError):
                export_public_release(self.project, destination)
        self.assertEqual((destination / "old").read_text(), "previous")


if __name__ == "__main__":
    unittest.main()
