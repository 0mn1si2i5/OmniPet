import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from omnipet.public_release import PublicReleaseError, verify_public_release


REQUIRED_FILES = {
    "pet.json", "spritesheet.webp", "preview.webp", "README.md",
    "LICENSE-ASSETS",
}


def _valid_atlas(path: Path) -> None:
    image = Image.new("RGBA", (1536, 2288), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    counts = (7, 8, 8, 4, 5, 8, 6, 6, 6, 8, 8)
    for row, count in enumerate(counts):
        for column in range(count):
            left, top = column * 192 + 80, row * 208 + 88
            draw.rectangle((left, top, left + 15, top + 15), fill=(30, 60, 200, 255))
    image.save(path, format="WEBP", lossless=True)


class PublicReleaseVerifyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bundle = self.root / "isolated-bundle"
        self.bundle.mkdir()
        (self.bundle / "pet.json").write_text(json.dumps({
            "id": "test-pet",
            "displayName": "Test Pet",
            "description": "A friendly pet.",
            "spriteVersionNumber": 2,
            "spritesheetPath": "spritesheet.webp",
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        _valid_atlas(self.bundle / "spritesheet.webp")
        Image.new("RGBA", (320, 240), (0, 0, 0, 0)).save(
            self.bundle / "preview.webp", format="WEBP", lossless=True
        )
        (self.bundle / "README.md").write_text(
            "# Test Pet\n\nInstall this friendly pet.\n", encoding="utf-8"
        )
        (self.bundle / "LICENSE-ASSETS").write_text(
            "SPDX-License-Identifier: CC-BY-NC-4.0\n", encoding="utf-8"
        )
        self.write_release()

    def write_release(self, **updates):
        files = {
            name: "sha256:" + hashlib.sha256(
                (self.bundle / name).read_bytes()
            ).hexdigest()
            for name in sorted(REQUIRED_FILES)
            if (self.bundle / name).is_file()
        }
        record = {
            "schemaVersion": 1,
            "petId": "test-pet",
            "version": "1.2.3",
            "omnipetVersion": "0.1.0a1",
            "spriteVersionNumber": 2,
            "files": files,
            "license": "CC-BY-NC-4.0",
        }
        record.update(updates)
        (self.bundle / "release.json").write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def test_verifies_in_clean_room_without_project_or_credentials(self):
        os.environ.pop("OPENAI_API_KEY", None)
        before = {
            path.name: path.read_bytes() for path in self.bundle.iterdir()
        }
        result = verify_public_release(self.bundle)
        self.assertEqual(result["petId"], "test-pet")
        self.assertFalse((self.root / "pet.yaml").exists())
        self.assertFalse((self.root / ".omnipet").exists())
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.bundle.iterdir()},
            before,
        )

    def test_rejects_open_schema_missing_undeclared_and_hash_mismatch(self):
        record = json.loads((self.bundle / "release.json").read_text())
        record["private"] = "not allowed"
        self.write_release(private="not allowed")
        with self.assertRaises(PublicReleaseError):
            verify_public_release(self.bundle)

        self.write_release()
        (self.bundle / "brief.md").write_text("private")
        with self.assertRaises(PublicReleaseError):
            verify_public_release(self.bundle)
        (self.bundle / "brief.md").unlink()

        (self.bundle / "README.md").write_text("changed")
        with self.assertRaises(PublicReleaseError):
            verify_public_release(self.bundle)

    def test_rejects_pet_identity_path_version_and_license_mismatch(self):
        manifest = json.loads((self.bundle / "pet.json").read_text())
        for key, value in (
            ("id", "another-pet"),
            ("spritesheetPath", "../spritesheet.webp"),
            ("spriteVersionNumber", 1),
        ):
            original = manifest[key]
            manifest[key] = value
            (self.bundle / "pet.json").write_text(json.dumps(manifest))
            self.write_release()
            with self.subTest(key=key), self.assertRaises(PublicReleaseError):
                verify_public_release(self.bundle)
            manifest[key] = original

        (self.bundle / "pet.json").write_text(json.dumps(manifest))
        self.write_release(version="latest")
        with self.assertRaises(PublicReleaseError):
            verify_public_release(self.bundle)
        self.write_release(license="MIT")
        with self.assertRaises(PublicReleaseError):
            verify_public_release(self.bundle)

    def test_rejects_fake_or_wrong_atlas_and_unreadable_preview(self):
        atlas = self.bundle / "spritesheet.webp"
        original = atlas.read_bytes()
        atlas.write_bytes(b"RIFF fake WEBP")
        self.write_release()
        with self.assertRaises(PublicReleaseError):
            verify_public_release(self.bundle)

        Image.new("RGB", (1536, 2288), "blue").save(atlas, format="WEBP")
        self.write_release()
        with self.assertRaises(PublicReleaseError):
            verify_public_release(self.bundle)

        atlas.write_bytes(original)
        (self.bundle / "preview.webp").write_bytes(b"not an image")
        self.write_release()
        with self.assertRaises(PublicReleaseError):
            verify_public_release(self.bundle)

    def test_rejects_nonportable_secret_and_production_text(self):
        for text in (
            "Built from /srv/private/pet.png\n",
            "api_key=sk-test-secret-value\n",
            "See the private pet.yaml and checkpoint records.\n",
            "See references/portrait.png for the source input.\n",
        ):
            with self.subTest(text=text):
                (self.bundle / "README.md").write_text(text)
                self.write_release()
                with self.assertRaises(PublicReleaseError):
                    verify_public_release(self.bundle)

    def test_rejects_uri_unc_env_and_markdown_credential_assignments(self):
        for text in (
            "Source: file:///Users/alice/private/pet.png\n",
            "Source: \\\\server\\private\\pet.png\n",
            "OPENAI_API_KEY=abcdEFGH12345678\n",
            "Set `api_key`: abcdEFGH12345678\n",
        ):
            with self.subTest(text=text):
                (self.bundle / "README.md").write_text(text)
                self.write_release()
                with self.assertRaises(PublicReleaseError):
                    verify_public_release(self.bundle)

    def test_allows_normal_security_documentation_and_https_links(self):
        (self.bundle / "README.md").write_text(
            "# Install\n\n"
            "See https://example.com/docs. `api_key` values must not be committed; "
            "OPENAI_API_KEY is not required for installation.\n"
        )
        self.write_release()

        self.assertEqual(verify_public_release(self.bundle)["petId"], "test-pet")

    def test_rejects_symlinks_at_every_bundle_level(self):
        real = self.bundle / "README.real"
        (self.bundle / "README.md").rename(real)
        os.symlink(real.name, self.bundle / "README.md")
        self.write_release()
        with self.assertRaises(PublicReleaseError):
            verify_public_release(self.bundle)


if __name__ == "__main__":
    unittest.main()
