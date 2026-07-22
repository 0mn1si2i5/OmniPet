from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
MAINTAINED_PATHS = (
    Path("README.md"),
    Path(".env.example"),
    Path("docs/architecture.md"),
    Path("docs/pet-project-format.md"),
    Path("docs/generation-workflow.md"),
    Path("src/omnipet/templates/pet/README.md"),
    Path("src/omnipet/templates/pet/pet.yaml"),
    Path("src/omnipet/templates/pet/brief.md"),
    Path("src/omnipet/templates/pet/prompts/refinements.md"),
)


class RepositoryDocumentationTests(unittest.TestCase):
    def maintained_text(self) -> str:
        return "\n".join((REPO_ROOT / path).read_text() for path in MAINTAINED_PATHS)

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

    def test_starter_production_contract_is_preserved(self):
        text = self.maintained_text()

        for requirement in (
            "1536x2288",
            "spriteVersionNumber: 2",
            "nine standard rows",
            "16 directions",
            "canonical base",
            "Do not generate faux sprites programmatically",
            "one final chroma despill",
            "pet.json",
            "spritesheet.webp",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement.casefold(), text.casefold())

    def test_workflow_keeps_approval_and_qa_gates(self):
        text = self.maintained_text().casefold()

        for requirement in (
            "idle",
            "running-right",
            "four cardinal anchors",
            "row-level extraction",
            "three isolated blind axis reviews",
            "independent final visual qa",
            "package only when every hard gate passes",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, text)

    def test_look_rows_follow_the_required_review_sequence(self):
        text = (REPO_ROOT / "docs/generation-workflow.md").read_text().casefold()
        sequence = (
            "approve four cardinal anchors",
            "generate `look-row-9`",
            "register `look-row-9`",
            "edge review",
            "semantic review",
            "continuity review",
            "only after `look-row-9` passes",
            "generate `look-row-10`",
            "immediately review `look-row-10`",
            "final consolidated qa",
        )

        positions = [text.index(item) for item in sequence]
        self.assertEqual(positions, sorted(positions))

    def test_template_is_valid_prompt_only_and_documents_optional_reference(self):
        manifest = yaml.safe_load((REPO_ROOT / "src/omnipet/templates/pet/pet.yaml").read_text())
        instructions = "\n".join(
            (
                (REPO_ROOT / "README.md").read_text(),
                (REPO_ROOT / "src/omnipet/templates/pet/README.md").read_text(),
            )
        )

        self.assertEqual(manifest["references"], [])
        self.assertIn("immediately valid for prompt-only generation", instructions)
        self.assertIn("best identity quality", instructions)
        self.assertIn("mkdir -p pets/<pet-id>/references", instructions)
        self.assertIn(
            "cp /path/to/your-character.png pets/<pet-id>/references/your-character.png",
            instructions,
        )
        self.assertNotIn("rename the example reference", instructions.casefold())

    def test_packaged_template_is_the_only_template_source(self):
        self.assertFalse((REPO_ROOT / "templates/pet").exists())
        self.assertTrue((REPO_ROOT / "src/omnipet/templates/pet/pet.yaml").is_file())

    def test_security_docs_describe_contract_and_bounded_validation(self):
        text = self.maintained_text().casefold()

        self.assertIn("credentials are prohibited by contract", text)
        self.assertIn("openai_api_key", text)
        self.assertIn("bounded closed schema", text)
        self.assertIn("not an exhaustive secret detector", text)

    def test_current_docs_define_built_in_openai_generation(self):
        text = self.maintained_text()

        self.assertIn("gpt-image-2", text)
        self.assertIn("OPENAI_API_KEY", text)
        self.assertNotIn("image_" + "providers", text)
        self.assertNotIn("plugin", text.casefold())

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

    def test_readme_is_the_only_featured_project_reference(self):
        term = "su" + "shi"
        matches = []
        for path in subprocess.run(
            ["git", "ls-files"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
        ).stdout.splitlines():
            candidate = REPO_ROOT / path
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8").casefold()
            except UnicodeDecodeError:
                continue
            if term in text:
                matches.append(path)

        self.assertEqual(matches, ["README.md"])
        self.assertIn("OmniPet-" + "Su" + "Shi", (REPO_ROOT / "README.md").read_text())

    def test_release_metadata_declares_apache_license_and_readme(self):
        metadata = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertTrue((REPO_ROOT / "LICENSE").is_file())
        self.assertIn('license = "Apache-2.0"', metadata)
        self.assertIn('readme = "README.md"', metadata)

    def test_development_setup_declares_and_checks_packaging_backend(self):
        metadata = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        script = (REPO_ROOT / "scripts/test-all.sh").read_text(encoding="utf-8")
        install_command = ".venv/bin/pip install -e '.[dev]'"

        self.assertIn(
            '[project.optional-dependencies]\ndev = ["setuptools>=75", "packaging>=24", "twine>=6,<7"]',
            metadata,
        )
        self.assertIn(install_command, readme)
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
