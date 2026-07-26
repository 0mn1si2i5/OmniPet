from pathlib import Path
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
            ["openai>=2.46,<3", "packaging>=24.2,<27", "Pillow>=12,<13", "PyYAML>=6,<7"],
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
