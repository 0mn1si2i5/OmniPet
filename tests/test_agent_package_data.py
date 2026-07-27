import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "omnipet" / "agent"
REQUIRED_RESOURCES = {
    "contracts/minimum-evidence-v1.json",
    "schemas/action-contract-v1.json",
    "schemas/design-contract-v1.json",
    "schemas/design-pack-v1.json",
    "schemas/intake-v1.json",
    "schemas/look-mechanics-v1.json",
    "schemas/prototype-evidence-v1.json",
    "schemas/prototype-plan-v1.json",
    "schemas/state-storyboard-v1.json",
}


class AgentPackageDataTests(unittest.TestCase):
    def test_agent_resources_survive_all_distribution_paths(self):
        source_inventory = {
            path.relative_to(SOURCE_ROOT).as_posix()
            for path in SOURCE_ROOT.rglob("*.json")
        }
        self.assertTrue(REQUIRED_RESOURCES.issubset(source_inventory))

        source_bytes = {
            name: (SOURCE_ROOT / name).read_bytes() for name in REQUIRED_RESOURCES
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            build_root = temporary_root / "source"
            shutil.copytree(
                REPO_ROOT,
                build_root,
                ignore=shutil.ignore_patterns(
                    ".git", ".venv", ".worktrees", ".omnipet", "build",
                    "dist", "*.egg-info", "__pycache__",
                ),
            )
            distributions = temporary_root / "dist"
            distributions.mkdir()
            self._build_distributions(build_root, distributions)
            wheel, = distributions.glob("*.whl")
            sdist, = distributions.glob("*.tar.gz")

            self._assert_wheel_resources(wheel)
            with tarfile.open(sdist, "r:gz") as archive:
                sdist_paths = {
                    PurePosixPath(*PurePosixPath(member.name).parts[1:]).as_posix()
                    for member in archive.getmembers()
                    if member.isfile()
                }
                self.assertTrue(
                    {
                        f"src/omnipet/agent/{name}" for name in REQUIRED_RESOURCES
                    }.issubset(sdist_paths)
                )
                extracted = temporary_root / "sdist-source"
                archive.extractall(extracted, filter="data")

            extracted_root, = extracted.iterdir()
            sdist_wheel_directory = temporary_root / "sdist-wheel"
            sdist_wheel_directory.mkdir()
            self._build_wheel(extracted_root, sdist_wheel_directory)
            sdist_wheel, = sdist_wheel_directory.glob("*.whl")
            self._assert_wheel_resources(sdist_wheel)
            self._assert_clean_install_loads_source_resources(
                wheel, temporary_root / "installed", source_bytes
            )

    def _build_distributions(self, source: Path, output: Path):
        command = (
            "import setuptools.build_meta as backend; "
            f"out={str(output)!r}; backend.build_wheel(out); backend.build_sdist(out)"
        )
        subprocess.run(
            [sys.executable, "-c", command],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        )

    def _build_wheel(self, source: Path, output: Path):
        command = (
            "import setuptools.build_meta as backend; "
            f"backend.build_wheel({str(output)!r})"
        )
        subprocess.run(
            [sys.executable, "-c", command],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        )

    def _assert_wheel_resources(self, wheel: Path):
        with zipfile.ZipFile(wheel) as archive:
            self.assertTrue(
                {
                    f"omnipet/agent/{name}" for name in REQUIRED_RESOURCES
                }.issubset(archive.namelist())
            )

    def _assert_clean_install_loads_source_resources(
        self, wheel: Path, environment_root: Path, source_bytes: dict[str, bytes]
    ):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        subprocess.run(
            [sys.executable, "-m", "venv", str(environment_root)],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        python = environment_root / "bin" / "python"
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        expected = {
            name: {
                "sha256": hashlib.sha256(content).hexdigest(),
                "json": json.loads(content),
            }
            for name, content in source_bytes.items()
        }
        command = (
            "import hashlib, json; "
            "from importlib.resources import files; "
            "from omnipet.agent.resources import load_json_resource; "
            f"names={sorted(REQUIRED_RESOURCES)!r}; "
            "root=files('omnipet.agent'); "
            "print(json.dumps({name: {'sha256': hashlib.sha256("
            "root.joinpath(*name.split('/')).read_bytes()).hexdigest(), "
            "'json': load_json_resource(name)} for name in names}, sort_keys=True))"
        )
        actual = subprocess.run(
            [str(python), "-c", command],
            cwd=environment_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(actual.stdout), expected)


if __name__ == "__main__":
    unittest.main()
