from email.parser import BytesParser
import os
from pathlib import Path, PurePosixPath
import shutil
import site
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTIONS = ((REPO_ROOT, "omnipet"),)


class PackagingTests(unittest.TestCase):
    def test_distributions_ship_release_metadata_and_only_their_package(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)

            for index, (project_root, package_name) in enumerate(DISTRIBUTIONS):
                with self.subTest(project=project_root.relative_to(REPO_ROOT) or Path(".")):
                    output_directory = temporary_root / str(index)
                    output_directory.mkdir()
                    build_root = temporary_root / f"source-{index}"
                    shutil.copytree(
                        project_root,
                        build_root,
                        ignore=shutil.ignore_patterns(
                            ".git", ".venv", ".worktrees", ".omnipet", "build",
                            "dist", "*.egg-info", "__pycache__",
                        ),
                    )
                    command = (
                        "import pathlib, setuptools.build_meta as backend; "
                        f"out={str(output_directory)!r}; "
                        "backend.build_wheel(out); backend.build_sdist(out)"
                    )
                    subprocess.run(
                        [sys.executable, "-c", command],
                        cwd=build_root,
                        check=True,
                        capture_output=True,
                        text=True,
                    )

                    wheel, = output_directory.glob("*.whl")
                    sdist, = output_directory.glob("*.tar.gz")
                    readme = (project_root / "README.md").read_text(encoding="utf-8")

                    with zipfile.ZipFile(wheel) as archive:
                        members = archive.namelist()
                        metadata_member, = (
                            member for member in members if member.endswith(".dist-info/METADATA")
                        )
                        metadata = BytesParser().parsebytes(archive.read(metadata_member))

                        self.assertEqual(metadata["License-Expression"], "Apache-2.0")
                        self.assertEqual(metadata.get_all("License-File"), ["LICENSE"])
                        self.assertEqual(
                            metadata.get_payload(decode=True).decode("utf-8"),
                            readme,
                        )
                        self.assertTrue(
                            any(member.endswith(".dist-info/licenses/LICENSE") for member in members)
                        )
                        packaged_modules = {
                            PurePosixPath(member).parts[0]
                            for member in members
                            if "/" in member and ".dist-info/" not in member
                        }
                        self.assertEqual(packaged_modules, {package_name})
                        self.assertIn("omnipet/_vendor/__init__.py", members)
                        self.assertIn("omnipet/_vendor/hatch/__init__.py", members)
                        self.assertIn("omnipet/templates/pet/README.md", members)
                        self.assertIn("omnipet/templates/pet/README.zh-CN.md", members)
                        self.assertIn("omnipet/templates/pet/LICENSE-ASSETS", members)
                        self.assertIn("omnipet/templates/pet/brief.md", members)
                        self.assertIn("omnipet/templates/pet/pet.yaml", members)
                        self.assertIn("omnipet/templates/pet/prompts/refinements.md", members)
                        self.assertNotIn("omnipet/files.py", members)

                    with tarfile.open(sdist, "r:gz") as archive:
                        members = [member.name for member in archive.getmembers()]
                        self.assertTrue(any(member.endswith("/LICENSE") for member in members))
                        self.assertTrue(any(member.endswith("/README.md") for member in members))
                        for relative in (
                            "src/omnipet/templates/pet/README.md",
                            "src/omnipet/templates/pet/README.zh-CN.md",
                            "src/omnipet/templates/pet/LICENSE-ASSETS",
                            "src/omnipet/templates/pet/brief.md",
                            "src/omnipet/templates/pet/pet.yaml",
                            "src/omnipet/templates/pet/prompts/refinements.md",
                        ):
                            self.assertTrue(
                                any(member.endswith(f"/{relative}") for member in members),
                                relative,
                            )

                    self._assert_installed_distribution_smoke(wheel, temporary_root / "wheel-smoke")

                    extracted = temporary_root / "sdist-source"
                    with tarfile.open(sdist, "r:gz") as archive:
                        archive.extractall(extracted, filter="data")
                    source_root, = extracted.iterdir()
                    sdist_wheel_dir = temporary_root / "sdist-wheel"
                    sdist_wheel_dir.mkdir()
                    subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "import setuptools.build_meta as backend; backend.build_wheel(" + repr(str(sdist_wheel_dir)) + ")",
                        ],
                        cwd=source_root,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    sdist_wheel, = sdist_wheel_dir.glob("*.whl")
                    self._assert_installed_distribution_smoke(
                        sdist_wheel,
                        temporary_root / "sdist-smoke",
                    )

    def test_public_tree_does_not_import_obsolete_image_copy_utility(self):
        matches = subprocess.run(
            [
                "git", "grep", "-n", "-E",
                "from omnipet(\\.files| import files)|import omnipet\\.files",
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(matches.returncode, 1, matches.stdout)
        self.assertEqual(matches.stdout, "")

    def _assert_installed_distribution_smoke(self, wheel: Path, environment_root: Path):
        environment = os.environ.copy()
        dependency_paths = {
            Path(entry).resolve()
            for entry in (*site.getsitepackages(), site.getusersitepackages())
            if Path(entry).is_dir()
            and not Path(entry).resolve().is_relative_to((REPO_ROOT / "src").resolve())
        }
        environment["PYTHONPATH"] = os.pathsep.join(map(str, sorted(dependency_paths)))
        subprocess.run(
            [sys.executable, "-m", "venv", str(environment_root)],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        python = environment_root / "bin" / "python"
        omnipet = environment_root / "bin" / "omnipet"
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        imported_paths = subprocess.run(
            [
                str(python),
                "-c",
                "from importlib.resources import files; "
                "import omnipet; "
                "print(omnipet.__file__); "
                "print(files('omnipet').joinpath('templates', 'pet', 'pet.yaml'))",
            ],
            cwd=environment_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        package_path, template_path = map(Path, imported_paths)
        self.assertTrue(package_path.resolve().is_relative_to(environment_root.resolve()))
        self.assertTrue(template_path.resolve().is_relative_to(environment_root.resolve()))
        self.assertTrue(template_path.is_file())
        version = subprocess.run(
            [str(omnipet), "--version"],
            cwd=environment_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertEqual(version.stdout, "omnipet 0.1.0a1\n")

        initialized = subprocess.run(
            [str(omnipet), "pet", "init", "my-pet"],
            cwd=environment_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        validated = subprocess.run(
            [str(omnipet), "pet", "validate", "my-pet"],
            cwd=environment_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(validated.returncode, 0, validated.stderr)
        initialized_root = environment_root / "pets" / "my-pet"
        manifest_text = (initialized_root / "pet.yaml").read_text(encoding="utf-8")
        self.assertIn("release:", manifest_text)
        self.assertIn("readme_zh_cn: README.zh-CN.md", manifest_text)
        self.assertTrue((initialized_root / "README.zh-CN.md").is_file())
        self.assertTrue((initialized_root / "LICENSE-ASSETS").is_file())


if __name__ == "__main__":
    unittest.main()
