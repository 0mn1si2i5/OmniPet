from pathlib import Path
import re
import tomllib
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_workflow(path: Path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


class AlphaReleaseTests(unittest.TestCase):
    def test_package_metadata_is_public_alpha(self):
        metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

        self.assertEqual(metadata["version"], "0.1.0a1")
        self.assertEqual(metadata["requires-python"], ">=3.12")
        self.assertEqual(metadata["readme"], "README.md")
        self.assertEqual(metadata["license"], "Apache-2.0")
        self.assertNotIn("authors", metadata)
        self.assertEqual(
            metadata["urls"],
            {
                "Homepage": "https://github.com/0mn1si2i5/OmniPet",
                "Repository": "https://github.com/0mn1si2i5/OmniPet.git",
                "Issues": "https://github.com/0mn1si2i5/OmniPet/issues",
                "Changelog": "https://github.com/0mn1si2i5/OmniPet/blob/main/CHANGELOG.md",
            },
        )
        self.assertEqual(
            metadata["dependencies"],
            ["openai>=2.46,<3", "Pillow>=12,<13", "PyYAML>=6,<7"],
        )
        self.assertEqual(
            metadata["optional-dependencies"]["dev"],
            ["setuptools>=75", "packaging>=24", "twine>=6,<7"],
        )
        for value in (
            "Programming Language :: Python :: 3.13",
            "Programming Language :: Python :: 3.14",
            "Operating System :: POSIX",
            "Operating System :: POSIX :: Linux",
            "Operating System :: MacOS",
        ):
            self.assertIn(value, metadata["classifiers"])
        self.assertNotIn("Operating System :: OS " + "Independent", metadata["classifiers"])
        self.assertTrue({"animated-pets", "sprite", "openai"}.issubset(metadata["keywords"]))
        self.assertIn('__version__ = "0.1.0a1"', (REPO_ROOT / "src/omnipet/__init__.py").read_text())

    def test_readme_covers_alpha_operation_and_risk_contract(self):
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        folded = text.casefold()

        for phrase in (
            "alpha",
            "pip install omnipet",
            "OPENAI_API_KEY",
            "built-in hatch",
            "what works",
            "approval",
            "does not retry",
            "cost",
            "privacy",
            "project structure",
            "recovery",
            "troubleshooting",
            "current limitations",
            "roadmap",
            "OmniPet-" + "Su" + "Shi",
            "release is blocked pending rights confirmation",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.casefold(), folded)
        for command in (
            "omnipet pet init",
            "omnipet pet validate",
            "omnipet hatch",
            "omnipet approve",
            "omnipet qa",
            "omnipet package",
            "omnipet checkpoint export",
        ):
            self.assertIn(command, text)
        self.assertNotIn("co" + "dex", folded)
        self.assertNotIn("plug" + "in", folded)
        for block in re.findall(r"```(?:sh|bash)\n(.*?)```", text, re.DOTALL):
            self.assertNotIn("<", block)
            self.assertNotIn(">", block)
            for line in block.splitlines():
                if line.startswith("omnipet "):
                    self.assertIn("my-pet", line)

        self.assertIn(
            "omnipet qa my-pet --stage package --verdict-file package-verdict.json",
            text,
        )
        self.assertIn("docs/package-review.md", text)
        package_review = (REPO_ROOT / "docs/package-review.md").read_text(encoding="utf-8")
        for phrase in (
            "direction_evidence",
            "final_visual",
            "sha256",
            "blind-sheet.png",
            "final direction semantics",
            "must be supplied by reviewers",
        ):
            self.assertIn(phrase, package_review)

    def test_public_docs_describe_only_built_in_current_workflow(self):
        paths = (
            REPO_ROOT / "docs/architecture.md",
            REPO_ROOT / "docs/pet-project-format.md",
            REPO_ROOT / "docs/generation-workflow.md",
        )
        text = "\n".join(path.read_text(encoding="utf-8") for path in paths).casefold()

        for phrase in (
            "built-in hatch",
            "approval pause",
            "no automatic retry",
            "checkpoint",
            "package only when every hard gate passes",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("plug" + "in", text)
        self.assertNotIn("external " + "hatch", text)
        workflow = (REPO_ROOT / "docs/generation-workflow.md").read_text(encoding="utf-8")
        self.assertNotIn("omnipet run status", workflow)
        self.assertIn("omnipet status my-pet", workflow)

    def test_release_and_community_documents_are_complete(self):
        required = (
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CODE_OF_CONDUCT.md",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/pull_request_template.md",
        )
        for relative in required:
            with self.subTest(path=relative):
                self.assertTrue((REPO_ROOT / relative).is_file())

        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("Keep a Changelog", changelog)
        self.assertIn("## [Unreleased]", changelog)
        self.assertIn("## [0.1.0a1] - 2026-07-22", changelog)

        contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").casefold()
        for phrase in (".[dev]", "scripts/test-all.sh", "test-driven development", "notice", "paid tests"):
            self.assertIn(phrase, contributing)

        security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8").casefold()
        for phrase in ("private vulnerability reporting", "0.1.0a1", "api key", "path", "provider"):
            self.assertIn(phrase, security)
        self.assertIsNone(re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", security))

        conduct = (REPO_ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
        self.assertIn("Contributor Covenant Code of Conduct", conduct)
        self.assertIn("version 2.1", conduct.casefold())
        self.assertIn("Enforcement Guidelines", conduct)
        self.assertIn("project maintainers", conduct.casefold())
        self.assertNotIn("[INSERT", conduct)

        for name in ("bug_report.yml", "feature_request.yml"):
            form = load_workflow(REPO_ROOT / ".github/ISSUE_TEMPLATE" / name)
            self.assertIn("name", form)
            self.assertIn("description", form)
            self.assertIn("body", form)

    def test_ci_matrix_build_and_smoke_are_offline(self):
        path = REPO_ROOT / ".github/workflows/ci.yml"
        workflow = load_workflow(path)
        text = path.read_text(encoding="utf-8")
        matrix = workflow["jobs"]["test"]["strategy"]["matrix"]

        self.assertEqual(workflow["on"]["push"]["branches"], ["main"])
        self.assertEqual(workflow["on"]["pull_request"], {})
        self.assertEqual(matrix["os"], ["ubuntu-latest", "macos-latest"])
        self.assertEqual(matrix["python-version"], ["3.12", "3.13", "3.14"])
        self.assertIn("pip install -e '.[dev]'", text)
        self.assertIn("scripts/test-all.sh", text)
        self.assertIn("build_wheel", text)
        self.assertIn("build_sdist", text)
        self.assertIn("python -m twine check dist/*", text)
        self.assertIn("pip wheel --no-deps dist/*.tar.gz", text)
        self.assertIn("dist/*.whl", text)
        self.assertGreaterEqual(text.count("bin/omnipet --version"), 2)
        self.assertGreaterEqual(text.count("bin/omnipet pet init my-pet"), 2)
        self.assertGreaterEqual(text.count("bin/omnipet pet validate my-pet"), 2)
        self.assertNotIn("OPENAI_API_KEY", text)
        self.assertNotIn("pytest", text)

    def test_release_workflow_uses_trusted_publishing_and_prerelease(self):
        path = REPO_ROOT / ".github/workflows/release.yml"
        workflow = load_workflow(path)
        text = path.read_text(encoding="utf-8")

        self.assertEqual(workflow["on"]["push"]["tags"], ["v[0-9]*"])
        build_steps = workflow["jobs"]["build"]["steps"]
        names = [step.get("name") for step in build_steps]
        verify_index = names.index("Verify release tag matches package version")
        self.assertLess(verify_index, names.index("Run package suite"))
        self.assertLess(verify_index, names.index("Build distributions"))
        verify_step = build_steps[verify_index]["run"]
        self.assertIn("GITHUB_REF_NAME", verify_step)
        self.assertIn("tomllib", verify_step)
        self.assertIn("importlib.metadata", verify_step)
        self.assertIn("id-token: write", text)
        self.assertIn("pypa/gh-action-pypi-publish", text)
        self.assertIn("softprops/action-gh-release", text)
        self.assertIn("prerelease: true", text)
        self.assertIn("scripts/test-all.sh", text)
        self.assertIn("build_wheel", text)
        self.assertIn("build_sdist", text)
        self.assertIn("python -m twine check dist/*", text)
        self.assertIn("pip wheel --no-deps dist/*.tar.gz", text)
        self.assertGreaterEqual(text.count("bin/omnipet --version"), 2)
        self.assertGreaterEqual(text.count("bin/omnipet pet init my-pet"), 2)
        self.assertGreaterEqual(text.count("bin/omnipet pet validate my-pet"), 2)
        publish = workflow["jobs"]["publish-pypi"]
        self.assertEqual(publish["environment"], "pypi")
        self.assertEqual(publish["permissions"], {"id-token": "write"})
        self.assertNotIn("password:", text)
        self.assertNotIn("api-token", text)
        self.assertNotIn("OPENAI_API_KEY", text)

    def test_vendor_parity_uses_only_repository_manifest(self):
        text = (REPO_ROOT / "tests/test_vendored_hatch.py").read_text(encoding="utf-8")

        self.assertNotIn("expanduser", text)
        self.assertNotIn("~/" + ".co" + "dex", text)
        self.assertIn("MANIFEST.json", text)


if __name__ == "__main__":
    unittest.main()
