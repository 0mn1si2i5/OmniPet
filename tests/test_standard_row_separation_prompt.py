import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from omnipet.run import build_current_prompts
from omnipet.project import load_pet_project

SKILL_DIR = Path(__file__).resolve().parents[1] / "src" / "omnipet" / "_vendor" / "hatch"
PREPARE = SKILL_DIR / "scripts" / "prepare_pet_run.py"

STANDARD_STATES = (
    "idle",
    "running-right",
    "running-left",
    "waving",
    "jumping",
    "failed",
    "waiting",
    "running",
    "review",
)

SEPARATION_CLAUSE = "chroma-only gap between neighboring poses"
MERGE_FORBIDDEN = "never let two poses touch or merge into one connected silhouette"


class StandardRowSeparationPromptTest(unittest.TestCase):
    def prepare_run(self, temporary_directory: str) -> Path:
        run_dir = Path(temporary_directory) / "run"
        subprocess.run(
            [
                sys.executable,
                str(PREPARE),
                "--pet-name",
                "Separation Test",
                "--pet-notes",
                "a simple mascot",
                "--output-dir",
                str(run_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return run_dir

    def test_standard_row_prompts_require_chroma_gap_between_poses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = self.prepare_run(temporary_directory)

            for state in STANDARD_STATES:
                prompt = (run_dir / "prompts" / "rows" / f"{state}.md").read_text()
                self.assertIn(
                    SEPARATION_CLAUSE,
                    prompt,
                    f"row prompt for {state} must require a chroma-only gap",
                )
                self.assertIn(
                    "without cutting through foreground",
                    prompt,
                    f"row prompt for {state} must tie the gap to deterministic extraction",
                )
                self.assertIn(
                    MERGE_FORBIDDEN,
                    prompt,
                    f"row prompt for {state} must forbid merged silhouettes",
                )

    def test_standard_row_retry_prompts_require_chroma_gap_between_poses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = self.prepare_run(temporary_directory)

            for state in STANDARD_STATES:
                prompt = (run_dir / "prompts" / "row-retries" / f"{state}.md").read_text()
                self.assertIn(
                    SEPARATION_CLAUSE,
                    prompt,
                    f"retry prompt for {state} must require a chroma-only gap",
                )
                self.assertIn(
                    MERGE_FORBIDDEN,
                    prompt,
                    f"retry prompt for {state} must forbid merged silhouettes",
                )

    def test_build_current_prompts_require_chroma_gap_between_poses(self) -> None:
        """build_current_prompts (used by adopt_canonical) must also include
        the separation clause so adoption-created runs are not regressed."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = Path(temporary_directory).resolve() / "repo"
            pet_root = repo_root / "pets" / "sample-pet"
            (pet_root / "references").mkdir(parents=True)
            (pet_root / "approved").mkdir()
            (pet_root / "brief.md").write_text(
                "# Sample Pet\n\n## Identity Lock\n\nDurable brief text.\n",
                encoding="utf-8",
            )
            (pet_root / "README.md").write_text("# Sample Pet\n", encoding="utf-8")
            (pet_root / "README.zh-CN.md").write_text("# \u793a\u4f8b\u5ba0\u7269\n", encoding="utf-8")
            (pet_root / "LICENSE-ASSETS").write_text(
                "SPDX-License-Identifier: CC-BY-NC-4.0\n",
                encoding="utf-8",
            )
            (pet_root / "references" / "portrait.jpg").write_bytes(b"portrait")
            Image.new("RGBA", (2, 2), (1, 2, 3, 255)).save(
                pet_root / "approved" / "canonical-base.png", format="PNG"
            )
            from tests.test_project import VALID_PET_YAML
            (pet_root / "pet.yaml").write_text(VALID_PET_YAML, encoding="utf-8")
            project = load_pet_project(repo_root, "sample-pet")
            prompts = build_current_prompts(project, {})

            for state in STANDARD_STATES:
                prompt = prompts[f"rows/{state}.md"]
                self.assertIn(
                    SEPARATION_CLAUSE,
                    prompt,
                    f"build_current_prompts row prompt for {state} must require a chroma-only gap",
                )
                self.assertIn(
                    MERGE_FORBIDDEN,
                    prompt,
                    f"build_current_prompts row prompt for {state} must forbid merged silhouettes",
                )
                retry = prompts[f"row-retries/{state}.md"]
                self.assertIn(
                    SEPARATION_CLAUSE,
                    retry,
                    f"build_current_prompts retry prompt for {state} must require a chroma-only gap",
                )


if __name__ == "__main__":
    unittest.main()
