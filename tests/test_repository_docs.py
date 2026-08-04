from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]


class RepositoryDocumentationTests(unittest.TestCase):
    def run_test_script(self, setuptools_version: str, *, block_packaging: bool = False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        temporary_root = Path(temporary.name)
        metadata_directory = temporary_root / "setuptools-0.dist-info"
        metadata_directory.mkdir()
        (metadata_directory / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: setuptools\nVersion: {setuptools_version}\n"
        )
        if block_packaging:
            (temporary_root / "packaging.py").write_text(
                'raise ModuleNotFoundError("No module named packaging")\n'
            )
        python_wrapper = temporary_root / "python"
        python_wrapper.write_text(
            "#!/bin/sh\n"
            f"if [ \"$1\" = -c ]; then exec {sys.executable!r} \"$@\"; fi\n"
            "exit 42\n"
        )
        python_wrapper.chmod(0o755)
        environment = os.environ.copy()
        environment["PYTHON"] = str(python_wrapper)
        environment["PYTHONPATH"] = str(temporary_root)
        return subprocess.run(
            ["scripts/test-all.sh"],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )

    def test_packaged_template_is_the_only_template_source(self):
        self.assertFalse((REPO_ROOT / "templates/pet").exists())
        self.assertTrue((REPO_ROOT / "src/omnipet/templates/pet/pet.yaml").is_file())

    def test_environment_example_is_core_only(self):
        self.assertEqual(
            (REPO_ROOT / ".env.example").read_text(),
            "OPENAI_API_KEY=\n",
        )

    def test_public_tree_excludes_project_assets_and_internal_provider(self):
        tracked = {
            path
            for path in subprocess.run(
                ["git", "ls-files"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            if (REPO_ROOT / path).exists()
        }

        self.assertFalse(any(path.startswith("pets/") for path in tracked))
        removed_plugin = "plugins/" + "model" + "hub/"
        self.assertFalse(any(path.startswith(removed_plugin) for path in tracked))
        self.assertNotIn("scripts/" + "model" + "hub_image_gen.py", tracked)

    def test_public_tree_contains_no_internal_provider_references(self):
        forbidden = ("model" + "hub", "byte" + "dance", "internal " + "endpoint")
        matches = []
        for path in subprocess.run(
            ["git", "ls-files"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
        ).stdout.splitlines():
            if path.startswith("docs/superpowers/"):
                continue
            candidate = REPO_ROOT / path
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8").casefold()
            except UnicodeDecodeError:
                continue
            if any(term in text for term in forbidden):
                matches.append(path)

        self.assertEqual(matches, [])

    def test_public_tree_excludes_temporary_generation_and_accounting_concepts(self):
        from dataclasses import fields

        from omnipet.generation import ImageRequest

        tracked = [
            path
            for path in subprocess.run(
                ["git", "ls-files"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            if (REPO_ROOT / path).exists()
        ]
        forbidden = (
            "open" + "router",
            "model" + "hub",
            "image_" + "providers",
            "provider " + "entrypoint",
            "provider " + "plugin",
            "co" + "st " + "led" + "ger",
            "co" + "st-" + "led" + "ger",
            "co" + "st_" + "led" + "ger",
            "pric" + "ing",
            "max_" + "co" + "st_" + "usd",
            "co" + "st_" + "usd",
            "pro" + "be " + "command",
            "approval_" + "token",
            "compro" + "mised",
            "codex_" + "home",
            "external " + "hatch",
        )
        allowed_legacy = {"src/omnipet/project.py", "tests/test_project.py"}
        matches = []
        for path in tracked:
            if path in allowed_legacy or path.startswith("src/omnipet/_vendor/hatch/"):
                continue
            candidate = REPO_ROOT / path
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8").casefold()
            except UnicodeDecodeError:
                continue
            if any(term in text for term in forbidden):
                matches.append(path)

        request_fields = {item.name for item in fields(ImageRequest)}
        accounting_fields = {"co" + "st", "pric" + "ing", "max_" + "co" + "st_" + "usd"}
        self.assertTrue(request_fields.isdisjoint(accounting_fields))
        self.assertNotIn("scripts/run_visual_job.sh", tracked)
        self.assertNotIn("scripts/safe_copy_image.py", tracked)
        self.assertEqual(matches, [])

    def test_release_metadata_declares_apache_license_and_readme(self):
        metadata = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertTrue((REPO_ROOT / "LICENSE").is_file())
        self.assertIn('license = "Apache-2.0"', metadata)
        self.assertIn('readme = "README.md"', metadata)

    def test_readmes_define_frozen_engine_and_recommended_creator_path(self):
        readmes = {
            "zh": (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
            "en": (REPO_ROOT / "README.en.md").read_text(encoding="utf-8"),
        }
        required_statements = {
            "zh": (
                "OmniPet 是实验性项目，现已进入维护状态，Engine 功能开发已冻结。",
                "[OmniPet-Skill](https://github.com/0mn1si2i5/OmniPet-Skill) "
                "及其 `creating-omnipets` 工作流是目前推荐的新桌宠创作路径。",
                "已发布的 OmniPet `0.1.0a1` 和现有工作流仍可使用，但不再积极开发新功能，"
                "也不承诺生产级可靠性。",
                "[OmniPets](https://github.com/0mn1si2i5/OmniPets) "
                "是最终可安装桌宠资产目录，不是用于创作的 Engine 或 Skill。",
            ),
            "en": (
                "OmniPet is experimental and now in maintenance mode; "
                "Engine feature development is frozen.",
                "[OmniPet-Skill](https://github.com/0mn1si2i5/OmniPet-Skill) "
                "and its `creating-omnipets` workflow are the recommended path "
                "for creating new desktop pets.",
                "The published OmniPet `0.1.0a1` and existing workflows remain available, "
                "but there is no active feature development or promise of "
                "production-grade reliability.",
                "[OmniPets](https://github.com/0mn1si2i5/OmniPets) "
                "is the catalog of final installable pet assets, not a creator Engine or Skill.",
            ),
        }

        for language, statements in required_statements.items():
            with self.subTest(language=language):
                for statement in statements:
                    self.assertIn(statement, readmes[language])

    def test_development_setup_declares_and_checks_packaging_backend(self):
        metadata = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        script = (REPO_ROOT / "scripts/test-all.sh").read_text(encoding="utf-8")
        install_command = ".venv/bin/pip install -e '.[dev]'"

        self.assertIn(
            '[project.optional-dependencies]\ndev = ["setuptools>=75", "packaging>=24", "twine>=6,<7"]',
            metadata,
        )
        self.assertIn('import setuptools.build_meta', script)
        self.assertIn(install_command, script)

    def test_test_script_rejects_old_and_invalid_setuptools_versions(self):
        install_command = ".venv/bin/pip install -e '.[dev]'"

        for setuptools_version in (
            "74.9.9",
            "75.0.0rc1",
            "75.0.0.dev0",
            "75.invalid",
        ):
            with self.subTest(setuptools_version=setuptools_version):
                process = self.run_test_script(setuptools_version)

                self.assertEqual(process.returncode, 1)
                self.assertIn(install_command, process.stderr)

    def test_test_script_accepts_final_setuptools_versions(self):
        for setuptools_version in ("75", "83"):
            with self.subTest(setuptools_version=setuptools_version):
                process = self.run_test_script(setuptools_version)

                self.assertEqual(process.returncode, 42)
                self.assertEqual(process.stderr, "")

    def test_test_script_rejects_missing_packaging(self):
        process = self.run_test_script("83", block_packaging=True)

        self.assertEqual(process.returncode, 1)
        self.assertIn(
            ".venv/bin/pip install -e '.[dev]'",
            process.stderr,
        )

    def test_ci_test_script_is_unittest_only_and_runs_one_package_suite(self):
        script = (REPO_ROOT / "scripts/test-all.sh").read_text(encoding="utf-8")

        self.assertNotIn("pytest", script)
        self.assertEqual(script.count("-m unittest discover"), 1)
        self.assertIn("-s tests", script)

    def test_external_skill_installer_is_removed(self):
        installer = REPO_ROOT / "scripts/install-skills.sh"

        self.assertFalse(installer.exists())


if __name__ == "__main__":
    unittest.main()
