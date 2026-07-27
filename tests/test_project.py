import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from omnipet.project import (
    PetReference,
    ProjectValidationError,
    load_pet_project,
    locate_pet_project,
)
from omnipet.security import is_credential_like_key


VALID_PET_YAML = """\
schema_version: 1
id: sample-pet
display_name: Sample Pet
description: A warm pixel-art sample character desktop pet.
brief: brief.md
style:
  preset: pixel
  notes: Modern high-detail pixel art.
references:
  - path: references/portrait.jpg
    role: historical character reference
image_generation:
  model: gpt-image-2
  quality: low
hatch_engine:
  minimum_sprite_version: 2
  atlas_layout: extended-v2
package:
  spritesheet: dist/spritesheet.webp
  manifest: dist/pet.json
release:
  version: 1.2.3
  asset_license: CC-BY-NC-4.0
  readme: README.md
  readme_zh_cn: README.zh-CN.md
  asset_license_file: LICENSE-ASSETS
  preview_source: dist/spritesheet.webp
approved:
  canonical_base: approved/canonical-base.png
"""


class CredentialDetectorTests(unittest.TestCase):
    def test_shared_detector_covers_auth_and_cookie_keys(self):
        for key in (
            "auth",
            "bearer",
            "cookie",
            "signing_key",
            "session_cookie",
            "authorization",
            "nested-client-secret",
        ):
            with self.subTest(key=key):
                self.assertTrue(is_credential_like_key(key))
        self.assertFalse(is_credential_like_key("token_budget"))


class PetProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_root = Path(self.temp_dir.name).resolve()
        self.pet_root = self.repo_root / "pets" / "sample-pet"
        (self.pet_root / "references").mkdir(parents=True)
        (self.pet_root / "approved").mkdir()
        (self.pet_root / "brief.md").write_text("# Sample Pet\n", encoding="utf-8")
        (self.pet_root / "README.md").write_text("# Sample Pet\n", encoding="utf-8")
        (self.pet_root / "README.zh-CN.md").write_text("# 示例宠物\n", encoding="utf-8")
        (self.pet_root / "LICENSE-ASSETS").write_text(
            "SPDX-License-Identifier: CC-BY-NC-4.0\n",
            encoding="utf-8",
        )
        (self.pet_root / "references" / "portrait.jpg").write_bytes(b"portrait")
        (self.pet_root / "approved" / "canonical-base.png").write_bytes(b"base")
        self.pet_yaml = self.pet_root / "pet.yaml"
        self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_loads_valid_pet_project(self):
        project = load_pet_project(self.repo_root, "sample-pet")

        self.assertEqual(project.pet_id, "sample-pet")
        self.assertEqual(project.root, self.pet_root)
        self.assertEqual(project.display_name, "Sample Pet")
        self.assertEqual(project.description, "A warm pixel-art sample character desktop pet.")
        self.assertEqual(project.style_preset, "pixel")
        self.assertEqual(project.style_notes, "Modern high-detail pixel art.")
        self.assertEqual(project.brief_path, self.pet_root / "brief.md")
        self.assertEqual(
            project.references,
            (
                PetReference(
                    path=self.pet_root / "references" / "portrait.jpg",
                    role="historical character reference",
                ),
            ),
        )
        self.assertEqual(
            project.reference_paths,
            (self.pet_root / "references" / "portrait.jpg",),
        )
        self.assertEqual(project.image_generation_model, "gpt-image-2")
        self.assertEqual(project.image_generation_quality, "low")
        self.assertEqual(project.minimum_sprite_version, 2)
        self.assertEqual(project.agent_workflow_version, 1)
        self.assertEqual(
            dict(project.hatch_engine_requirements),
            {"atlas_layout": "extended-v2"},
        )
        self.assertEqual(
            project.spritesheet_path,
            self.pet_root / "dist" / "spritesheet.webp",
        )
        self.assertEqual(project.manifest_path, self.pet_root / "dist" / "pet.json")
        self.assertEqual(
            project.canonical_base_path,
            self.pet_root / "approved" / "canonical-base.png",
        )
        self.assertEqual(project.release_version, "1.2.3")
        self.assertEqual(project.asset_license, "CC-BY-NC-4.0")
        self.assertEqual(project.release_readme_path, self.pet_root / "README.md")
        self.assertEqual(
            project.release_readme_zh_cn_path,
            self.pet_root / "README.zh-CN.md",
        )
        self.assertEqual(
            project.asset_license_path,
            self.pet_root / "LICENSE-ASSETS",
        )
        self.assertEqual(
            project.preview_source_path,
            self.pet_root / "dist" / "spritesheet.webp",
        )

    def test_release_metadata_is_closed(self):
        self._replace(
            "  preview_source: dist/spritesheet.webp",
            "  preview_source: dist/spritesheet.webp\n  repository_token: secret",
        )

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_invalid_release_versions(self):
        for version in ("1", "1.2", "01.2.3", "1.2.3.4", "v1.2.3", "1.2.3-"):
            with self.subTest(version=version):
                self._replace("version: 1.2.3", f"version: {version}")
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_accepts_semantic_release_prerelease_and_build_metadata(self):
        self._replace("version: 1.2.3", "version: 1.2.3-rc.1+build.7")

        project = load_pet_project(self.repo_root, "sample-pet")

        self.assertEqual(project.release_version, "1.2.3-rc.1+build.7")

    def test_rejects_invalid_spdx_asset_license(self):
        for asset_license in (
            "",
            "not a license",
            "MIT OR Apache-2.0",
            "LicenseRef-Proprietary",
            "Zlibb",
        ):
            with self.subTest(asset_license=asset_license):
                replacement = (
                    'asset_license: ""'
                    if not asset_license
                    else f"asset_license: {asset_license}"
                )
                self._replace("asset_license: CC-BY-NC-4.0", replacement)
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_release_readme_zh_cn_is_optional(self):
        self._replace("  readme_zh_cn: README.zh-CN.md\n", "")

        project = load_pet_project(self.repo_root, "sample-pet")

        self.assertIsNone(project.release_readme_zh_cn_path)

    def test_rejects_explicit_null_release_readme_zh_cn(self):
        self._replace("readme_zh_cn: README.zh-CN.md", "readme_zh_cn: null")

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_missing_release_text_files(self):
        for relative in ("README.md", "README.zh-CN.md", "LICENSE-ASSETS"):
            with self.subTest(relative=relative):
                path = self.pet_root / relative
                content = path.read_bytes()
                path.unlink()
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                path.write_bytes(content)

    def test_rejects_release_path_traversal(self):
        for field, value in (
            ("readme", "../README.md"),
            ("readme_zh_cn", "../README.zh-CN.md"),
            ("asset_license_file", "../LICENSE-ASSETS"),
            ("preview_source", "../preview.webp"),
        ):
            with self.subTest(field=field):
                original = {
                    "readme": "README.md",
                    "readme_zh_cn": "README.zh-CN.md",
                    "asset_license_file": "LICENSE-ASSETS",
                    "preview_source": "dist/spritesheet.webp",
                }[field]
                self._replace(f"{field}: {original}", f"{field}: {value}")
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_rejects_symlinked_release_text_file(self):
        outside = self.repo_root / "outside-readme.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        readme = self.pet_root / "README.md"
        readme.unlink()
        readme.symlink_to(outside)

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_locates_standalone_project_from_root_selector(self):
        standalone = self._standalone_project()

        locator = locate_pet_project(standalone, ".")
        project = load_pet_project(standalone, ".")

        self.assertEqual(locator.repository_root, standalone.resolve())
        self.assertEqual(locator.project_root, standalone.resolve())
        self.assertEqual(project.pet_id, "sample-pet")
        self.assertEqual(project.root, standalone.resolve())
        self.assertEqual(project.repository_root, standalone.resolve())

    def test_rejects_standalone_repository_root_with_symlinked_parent(self):
        external_root = self._external_root()
        standalone = self._write_standalone(external_root / "target" / "pet")
        link = external_root / "link-parent"
        link.symlink_to(standalone.parent, target_is_directory=True)

        with self.assertRaises(ProjectValidationError):
            load_pet_project(link / "pet", ".")

    def test_rejects_standalone_repository_root_with_symlinked_grandparent(self):
        external_root = self._external_root()
        standalone = self._write_standalone(external_root / "target" / "child" / "pet")
        link = external_root / "link-grandparent"
        link.symlink_to(standalone.parents[1], target_is_directory=True)

        with self.assertRaises(ProjectValidationError):
            load_pet_project(link / "child" / "pet", ".")

    def test_rejects_legacy_repository_root_with_symlinked_parent(self):
        external_root = self._external_root()
        repository = external_root / "target" / "repo"
        self._write_standalone(repository / "pets" / "sample-pet")
        link = external_root / "link-parent"
        link.symlink_to(repository.parent, target_is_directory=True)

        with self.assertRaises(ProjectValidationError):
            load_pet_project(link / "repo", "sample-pet")

    def test_rejects_legacy_repository_root_with_symlinked_grandparent(self):
        external_root = self._external_root()
        repository = external_root / "target" / "child" / "repo"
        self._write_standalone(repository / "pets" / "sample-pet")
        link = external_root / "link-grandparent"
        link.symlink_to(repository.parents[1], target_is_directory=True)

        with self.assertRaises(ProjectValidationError):
            load_pet_project(link / "child" / "repo", "sample-pet")

    def test_loads_normal_canonical_repository_roots(self):
        standalone = self._write_standalone(self._external_root() / "standalone")
        legacy = self._external_root() / "legacy"
        self._write_standalone(legacy / "pets" / "sample-pet")

        standalone_project = load_pet_project(standalone, ".")
        legacy_project = load_pet_project(legacy, "sample-pet")

        self.assertEqual(standalone_project.root, standalone)
        self.assertEqual(legacy_project.root, legacy / "pets" / "sample-pet")

    def test_locates_standalone_project_from_absolute_path(self):
        standalone = self._standalone_project()

        project = load_pet_project(self.repo_root, standalone.resolve())

        self.assertEqual(project.root, standalone.resolve())
        self.assertEqual(project.repository_root, standalone.resolve())

    def test_locates_standalone_project_from_relative_path(self):
        standalone = self._standalone_project()

        project = load_pet_project(self.repo_root, "standalone")

        self.assertEqual(project.root, standalone.resolve())
        self.assertEqual(project.repository_root, standalone.resolve())

    def test_standalone_selector_id_must_match_manifest(self):
        standalone = self._standalone_project()

        with self.assertRaises(ProjectValidationError):
            load_pet_project(standalone, "other")

    def test_standalone_manifest_id_must_be_safe(self):
        standalone = self._standalone_project()
        pet_yaml = standalone / "pet.yaml"
        pet_yaml.write_text(
            pet_yaml.read_text(encoding="utf-8").replace("id: sample-pet", "id: ../sample-pet"),
            encoding="utf-8",
        )

        with self.assertRaises(ProjectValidationError):
            load_pet_project(standalone, ".")

    def test_rejects_standalone_root_symlink(self):
        standalone = self._standalone_project()
        link = self.repo_root / "standalone-link"
        link.symlink_to(standalone, target_is_directory=True)

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, link)

    def test_rejects_standalone_path_with_symlinked_parent(self):
        standalone = self._standalone_project()
        link = self.repo_root / "linked-parent"
        link.symlink_to(self.repo_root, target_is_directory=True)

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, link / standalone.name)

    def test_rejects_external_standalone_with_symlinked_parent(self):
        external_root = self._external_root()
        standalone = self._write_standalone(external_root / "target" / "pet")
        link = external_root / "link-parent"
        link.symlink_to(standalone.parent, target_is_directory=True)

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, link / "pet")

    def test_rejects_external_standalone_with_symlinked_grandparent(self):
        external_root = self._external_root()
        standalone = self._write_standalone(external_root / "target" / "child" / "pet")
        link = external_root / "link-grandparent"
        link.symlink_to(standalone.parents[1], target_is_directory=True)

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, link / "child" / "pet")

    def test_rejects_nonexistent_standalone_components(self):
        external_root = self._external_root()

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, external_root / "missing" / "pet")

    def test_rejects_lexically_traversing_absolute_standalone_path(self):
        external_root = self._external_root()
        standalone = self._write_standalone(external_root / "pet")

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, standalone.parent / "unused" / ".." / "pet")

    def test_loads_normal_external_standalone_project(self):
        standalone = self._write_standalone(self._external_root() / "pet")

        project = load_pet_project(self.repo_root, standalone)

        self.assertEqual(project.root, standalone)
        self.assertEqual(project.repository_root, standalone)

    def test_rejects_standalone_selector_traversal(self):
        self._standalone_project()

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "../standalone")

    def test_project_metadata_is_immutable(self):
        project = load_pet_project(self.repo_root, "sample-pet")

        with self.assertRaises(FrozenInstanceError):
            project.display_name = "Changed"
        with self.assertRaises(FrozenInstanceError):
            project.image_generation_model = "changed"

    def test_legacy_openai_provider_is_read_with_deprecation_warning(self):
        self._replace(
            "image_generation:\n  model: gpt-image-2\n  quality: low",
            "provider:\n  name: openai",
        )

        with self.assertWarns(DeprecationWarning):
            project = load_pet_project(self.repo_root, "sample-pet")

        self.assertEqual(project.image_generation_model, "gpt-image-2")
        self.assertEqual(project.image_generation_quality, "low")

    def test_legacy_openai_provider_still_rejects_secret_options(self):
        self._replace(
            "image_generation:\n  model: gpt-image-2\n  quality: low",
            "provider:\n  name: openai\n  options:\n    api_key: private",
        )

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_legacy_non_openai_names(self):
        for name in ("open" + "router", "unknown"):
            with self.subTest(name=name):
                self._replace(
                    "image_generation:\n  model: gpt-image-2\n  quality: low",
                    f"provider:\n  name: {name}",
                )
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_rejects_unsupported_image_model_and_quality(self):
        for old, new in (
            ("model: gpt-image-2", "model: arbitrary-model"),
            ("quality: low", "quality: ultra"),
        ):
            with self.subTest(new=new):
                self._replace(old, new)
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_rejects_invalid_schema_version_boundaries(self):
        for value in ("true", "false", "0", "2", '"1"'):
            with self.subTest(value=value):
                self._replace("schema_version: 1", f"schema_version: {value}")
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_rejects_invalid_minimum_sprite_version_boundaries(self):
        for value in ("true", "false", "1", "0", '"2"'):
            with self.subTest(value=value):
                self._replace(
                    "minimum_sprite_version: 2",
                    f"minimum_sprite_version: {value}",
                )
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_accepts_minimum_sprite_version_above_two(self):
        self._replace("minimum_sprite_version: 2", "minimum_sprite_version: 3")

        project = load_pet_project(self.repo_root, "sample-pet")

        self.assertEqual(project.minimum_sprite_version, 3)

    def test_loads_explicit_agent_workflow_version_two(self):
        self._replace(
            "  minimum_sprite_version: 2",
            "  minimum_sprite_version: 2\n  agent_workflow_version: 2",
        )

        project = load_pet_project(self.repo_root, "sample-pet")

        self.assertEqual(project.agent_workflow_version, 2)
        self.assertEqual(
            dict(project.hatch_engine_requirements),
            {"atlas_layout": "extended-v2"},
        )

    def test_rejects_invalid_agent_workflow_versions(self):
        for value in ("true", "false", "0", "3", '"2"'):
            with self.subTest(value=value):
                self._replace(
                    "  minimum_sprite_version: 2",
                    "  minimum_sprite_version: 2\n  agent_workflow_version: " + value,
                )
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_rejects_blank_required_text_and_path_fields(self):
        replacements = (
            ("display_name: Sample Pet", 'display_name: "   "'),
            (
                "description: A warm pixel-art sample character desktop pet.",
                'description: "   "',
            ),
            ("  preset: pixel", '  preset: "   "'),
            ("  model: gpt-image-2", '  model: "   "'),
            (
                "    role: historical character reference",
                '    role: "   "',
            ),
            ("brief: brief.md", 'brief: "   "'),
            ("spritesheet: dist/spritesheet.webp", 'spritesheet: "   "'),
            ("manifest: dist/pet.json", 'manifest: "   "'),
        )
        for original, replacement in replacements:
            with self.subTest(field=original.split(":", 1)[0].strip()):
                self._replace(original, replacement)
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_rejects_non_string_required_text_and_path_fields(self):
        replacements = (
            ("display_name: Sample Pet", "display_name: 7"),
            (
                "description: A warm pixel-art sample character desktop pet.",
                "description: true",
            ),
            ("  preset: pixel", "  preset: 7"),
            ("  model: gpt-image-2", "  model: false"),
            ("    role: historical character reference", "    role: 7"),
            ("brief: brief.md", "brief: false"),
            ("spritesheet: dist/spritesheet.webp", "spritesheet: 7"),
            ("manifest: dist/pet.json", "manifest: false"),
        )
        for original, replacement in replacements:
            with self.subTest(field=original.split(":", 1)[0].strip()):
                self._replace(original, replacement)
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_allows_blank_optional_style_notes(self):
        self._replace(
            "  notes: Modern high-detail pixel art.",
            '  notes: "   "',
        )

        project = load_pet_project(self.repo_root, "sample-pet")

        self.assertEqual(project.style_notes, "   ")

    def test_allows_omitted_optional_style_notes(self):
        self._replace("  notes: Modern high-detail pixel art.\n", "")

        project = load_pet_project(self.repo_root, "sample-pet")

        self.assertEqual(project.style_notes, "")

    def test_rejects_combined_invalid_required_metadata(self):
        invalid = (
            VALID_PET_YAML.replace("schema_version: 1", "schema_version: true")
            .replace("display_name: Sample Pet", 'display_name: "   "')
            .replace("  preset: pixel", '  preset: "   "')
            .replace("  model: gpt-image-2", '  model: "   "')
            .replace("minimum_sprite_version: 2", "minimum_sprite_version: false")
            .replace("brief: brief.md", 'brief: "   "')
            .replace("spritesheet: dist/spritesheet.webp", 'spritesheet: "   "')
        )
        self.pet_yaml.write_text(invalid, encoding="utf-8")

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_extra_image_generation_configuration(self):
        nested = "leaf"
        for _ in range(1200):
            nested = {"config": nested}
        data = __import__("yaml").safe_load(VALID_PET_YAML)
        data["image_generation"]["options"] = nested

        with patch("omnipet.project.yaml.safe_load", return_value=data):
            with self.assertRaises(ProjectValidationError):
                load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_deep_hatch_metadata_without_recursion_error(self):
        nested = "leaf"
        for _ in range(1200):
            nested = {"config": nested}
        data = __import__("yaml").safe_load(VALID_PET_YAML)
        data["hatch_engine"]["requirements"] = nested

        with patch("omnipet.project.yaml.safe_load", return_value=data):
            with self.assertRaises(ProjectValidationError):
                load_pet_project(self.repo_root, "sample-pet")

    def test_converts_yaml_loader_recursion_error_to_validation_error(self):
        with patch("omnipet.project.yaml.safe_load", side_effect=RecursionError):
            with self.assertRaises(ProjectValidationError):
                load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_cyclic_yaml_alias_as_validation_error(self):
        self._replace(
            "image_generation:\n  model: gpt-image-2\n  quality: low\n",
            "image_generation: &image_generation\n  model: gpt-image-2\n  quality: low\n  recursive: *image_generation\n",
        )

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_allows_missing_optional_canonical_base(self):
        self._replace(
            "approved:\n  canonical_base: approved/canonical-base.png\n",
            "",
        )

        project = load_pet_project(self.repo_root, "sample-pet")

        self.assertIsNone(project.canonical_base_path)

    def test_rejects_absolute_reference(self):
        self._replace("references/portrait.jpg", "/tmp/portrait.jpg")

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_reference_traversal(self):
        self._replace("references/portrait.jpg", "../portrait.jpg")

        with self.assertRaises(ValueError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_reference_symlink_escape(self):
        outside = self.repo_root / "outside.jpg"
        outside.write_bytes(b"outside")
        portrait = self.pet_root / "references" / "portrait.jpg"
        portrait.unlink()
        portrait.symlink_to(outside)

        with self.assertRaises(ValueError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_mismatched_id(self):
        self._replace("id: sample-pet", "id: other")

        with self.assertRaises(ValueError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_duplicate_reference_paths(self):
        duplicate = """\
  - path: references/portrait.jpg
    role: duplicate
"""
        self._replace("image_generation:\n", duplicate + "image_generation:\n")

        with self.assertRaises(ValueError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_missing_brief(self):
        (self.pet_root / "brief.md").unlink()

        with self.assertRaises(ValueError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_absolute_canonical_base(self):
        self._replace("approved/canonical-base.png", "/tmp/canonical-base.png")

        with self.assertRaises(ValueError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_canonical_base_traversal(self):
        self._replace("approved/canonical-base.png", "../canonical-base.png")

        with self.assertRaises(ValueError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_absolute_package_path(self):
        self._replace("dist/spritesheet.webp", "/tmp/spritesheet.webp")

        with self.assertRaises(ValueError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_package_path_traversal(self):
        self._replace("dist/pet.json", "../pet.json")

        with self.assertRaises(ValueError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_accepts_dist_package_sibling_paths(self):
        project = load_pet_project(self.repo_root, "sample-pet")

        self.assertEqual(
            project.spritesheet_path,
            self.pet_root / "dist" / "spritesheet.webp",
        )
        self.assertEqual(project.manifest_path, self.pet_root / "dist" / "pet.json")

    def test_rejects_equal_package_paths(self):
        self._replace("dist/pet.json", "dist/spritesheet.webp")

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_package_path_colliding_with_brief(self):
        self._replace("dist/spritesheet.webp", "brief.md")

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_package_path_colliding_with_reference(self):
        self._replace("dist/pet.json", "references/portrait.jpg")

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_package_overlap_with_durable_reference_inside_dist(self):
        (self.pet_root / "dist").mkdir()
        portrait = self.pet_root / "references" / "portrait.jpg"
        portrait.rename(self.pet_root / "dist" / "portrait.jpg")
        self._replace("references/portrait.jpg", "dist/portrait.jpg")
        self._replace("dist/spritesheet.webp", "dist/portrait.jpg")

        with self.assertRaises(ProjectValidationError):
            load_pet_project(self.repo_root, "sample-pet")

    def test_rejects_package_destination_ancestor_overlap(self):
        for spritesheet, manifest in (
            ("dist", "dist/pet.json"),
            ("dist/package", "dist/package/pet.json"),
        ):
            with self.subTest(spritesheet=spritesheet, manifest=manifest):
                self._replace("dist/spritesheet.webp", spritesheet)
                self._replace("dist/pet.json", manifest)
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def test_rejects_package_path_in_durable_input_directories(self):
        for destination in (
            "pet.yaml",
            "prompts/generated.webp",
            "approved/generated.webp",
        ):
            with self.subTest(destination=destination):
                self._replace("dist/spritesheet.webp", destination)
                with self.assertRaises(ProjectValidationError):
                    load_pet_project(self.repo_root, "sample-pet")
                self.pet_yaml.write_text(VALID_PET_YAML, encoding="utf-8")

    def _replace(self, old, new):
        content = self.pet_yaml.read_text(encoding="utf-8")
        self.pet_yaml.write_text(content.replace(old, new), encoding="utf-8")

    def _standalone_project(self):
        standalone = self.repo_root / "standalone"
        return self._write_standalone(standalone)

    def _external_root(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    def _write_standalone(self, standalone):
        (standalone / "references").mkdir(parents=True)
        (standalone / "approved").mkdir()
        (standalone / "brief.md").write_text("# Sample Pet\n", encoding="utf-8")
        (standalone / "README.md").write_text("# Sample Pet\n", encoding="utf-8")
        (standalone / "README.zh-CN.md").write_text("# 示例宠物\n", encoding="utf-8")
        (standalone / "LICENSE-ASSETS").write_text(
            "SPDX-License-Identifier: CC-BY-NC-4.0\n",
            encoding="utf-8",
        )
        (standalone / "references" / "portrait.jpg").write_bytes(b"portrait")
        (standalone / "approved" / "canonical-base.png").write_bytes(b"base")
        (standalone / "pet.yaml").write_text(VALID_PET_YAML, encoding="utf-8")
        return standalone


if __name__ == "__main__":
    unittest.main()
