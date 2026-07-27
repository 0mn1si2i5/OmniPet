"""Tests that build_current_prompts (adoption path) generates rich prompts
equivalent in detail to prepare_pet_run.py (prepare_run path).

System-level issue: the adoption path generated drastically inferior prompts
— missing frame counts, chroma key, state requirements, animation continuity,
clean extraction, and style contract. The identity contract was too restrictive,
preventing legitimate pose/expression/direction variation.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from omnipet.run import build_current_prompts, _identity_contract
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

DIRECTIONAL_STATES = ("running-right", "running-left")


def _make_project(temporary_directory: str):
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
    return project


class BuildCurrentPromptsRichTest(unittest.TestCase):
    """Verify build_current_prompts generates rich, detailed prompts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = _make_project(self._tmp.name)
        self.request = {
            "pet_id": "sample-pet",
            "pet_notes": "A compact mascot with a red cloak and bicorne hat.",
            "style_contract": "Pet-safe painterly sprite with crisp edges.",
            "chroma_key": {"hex": "#00FFFF", "name": "cyan"},
            "rows": [
                {"state": "idle", "frames": 6},
                {"state": "running-right", "frames": 8},
                {"state": "running-left", "frames": 8},
                {"state": "waving", "frames": 4},
                {"state": "jumping", "frames": 5},
                {"state": "failed", "frames": 8},
                {"state": "waiting", "frames": 6},
                {"state": "running", "frames": 6},
                {"state": "review", "frames": 6},
            ],
        }
        self.prompts = build_current_prompts(self.project, self.request)

    def tearDown(self):
        self._tmp.cleanup()

    def test_row_prompts_include_frame_count(self):
        """Each row prompt must specify the exact frame count."""
        expected_frames = {
            "idle": "6", "running-right": "8", "running-left": "8",
            "waving": "4", "jumping": "5", "failed": "8",
            "waiting": "6", "running": "6", "review": "6",
        }
        for state, frames_str in expected_frames.items():
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn(
                f"exactly {frames_str} full-body frames",
                prompt,
                f"row prompt for {state} must specify frame count",
            )

    def test_row_prompts_include_chroma_key(self):
        """Each row prompt must specify the chroma key color."""
        for state in STANDARD_STATES:
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn("#00FFFF", prompt, f"row prompt for {state} must include chroma key hex")
            self.assertIn("cyan", prompt, f"row prompt for {state} must include chroma key name")

    def test_row_prompts_include_state_requirements(self):
        """Each row prompt must include per-state requirements list."""
        for state in STANDARD_STATES:
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn(
                "State requirements:",
                prompt,
                f"row prompt for {state} must include state requirements section",
            )
            self.assertIn(
                "- ",
                prompt,
                f"row prompt for {state} must include at least one requirement bullet",
            )

    def test_row_prompts_include_animation_continuity(self):
        """Each row prompt must include animation continuity guidance."""
        for state in STANDARD_STATES:
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn(
                "Animation continuity:",
                prompt,
                f"row prompt for {state} must include animation continuity guidance",
            )

    def test_row_prompts_include_clean_extraction(self):
        """Each row prompt must include clean extraction requirements."""
        for state in STANDARD_STATES:
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn(
                "Clean extraction:",
                prompt,
                f"row prompt for {state} must include clean extraction requirements",
            )

    def test_row_prompts_include_style_contract(self):
        """Each row prompt must include the style contract."""
        for state in STANDARD_STATES:
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn(
                "Style:",
                prompt,
                f"row prompt for {state} must include style contract",
            )

    def test_row_prompts_include_pet_notes(self):
        """Each row prompt must include the pet notes (identity description)."""
        for state in STANDARD_STATES:
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn(
                "A compact mascot with a red cloak and bicorne hat.",
                prompt,
                f"row prompt for {state} must include pet notes from request",
            )

    def test_row_prompts_include_layout_guide_reference(self):
        """Each row prompt must reference the layout guide."""
        for state in STANDARD_STATES:
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn(
                "layout guide",
                prompt,
                f"row prompt for {state} must reference the layout guide",
            )

    def test_directional_states_include_facing_guidance(self):
        """running-right and running-left must explicitly call for body rotation
        and gaze direction change, not just 'travel right/left'."""
        for state in DIRECTIONAL_STATES:
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn(
                "rotate the body",
                prompt,
                f"row prompt for {state} must call for body rotation",
            )
            self.assertIn(
                "gaze direction",
                prompt,
                f"row prompt for {state} must call for gaze direction change",
            )

    def test_identity_contract_encourages_variation(self):
        """Identity contract must explicitly encourage pose/expression/direction
        variation, not just preserve identity statically."""
        contract = _identity_contract(self.project)
        self.assertIn("Vary pose, expression", contract)
        self.assertIn("facing direction", contract)
        self.assertIn("living", contract)
        self.assertNotIn("do not add, remove, relocate, or mirror", contract)

    def test_row_prompts_encourage_pose_variation(self):
        """State requirements must explicitly call for pose/expression variation
        between frames."""
        for state in STANDARD_STATES:
            if state == "idle":
                continue
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn(
                "Vary",
                prompt,
                f"row prompt for {state} must explicitly call for variation between frames",
            )

    def test_retry_prompts_are_rich(self):
        """Retry prompts must be as rich as the initial row prompts."""
        for state in STANDARD_STATES:
            retry = self.prompts[f"row-retries/{state}.md"]
            self.assertIn("State requirements:", retry)
            self.assertIn("Clean extraction:", retry)
            self.assertIn("Animation continuity:", retry)

    def test_row_prompts_include_composition_guidance(self):
        """Each row prompt must include composition/sizing guidance so the
        character fills the cell and is readable at small sizes."""
        for state in STANDARD_STATES:
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn(
                "Composition:",
                prompt,
                f"row prompt for {state} must include composition guidance",
            )
            self.assertIn(
                "75%",
                prompt,
                f"row prompt for {state} must specify minimum fill percentage",
            )

    def test_running_right_no_mirroring_override(self):
        """running-right must NOT include DIRECTIONAL OVERRIDE — the agent's
        pet-specific analysis should govern mirroring decisions instead."""
        prompt = self.prompts["rows/running-right.md"]
        self.assertNotIn("DIRECTIONAL OVERRIDE", prompt)
        self.assertNotIn("mirror the entire character horizontally", prompt)

    def test_running_left_no_mirroring_override(self):
        """running-left must NOT include DIRECTIONAL OVERRIDE — the agent's
        pet-specific analysis should govern mirroring decisions instead."""
        prompt = self.prompts["rows/running-left.md"]
        self.assertNotIn("DIRECTIONAL OVERRIDE", prompt)
        self.assertNotIn("mirror the entire character horizontally", prompt)

    def test_retry_prompts_include_composition_guidance(self):
        """Retry prompts must also include composition guidance."""
        for state in STANDARD_STATES:
            retry = self.prompts[f"row-retries/{state}.md"]
            self.assertIn(
                "Composition:",
                retry,
                f"retry prompt for {state} must include composition guidance",
            )

    def test_look_cardinals_prompt_is_rich(self):
        """look-cardinals prompt must include cardinal order, landmark rules,
        screen-coordinate rules, and reference to look-mechanics.md."""
        prompt = self.prompts["look-cardinals.md"]
        self.assertIn("four-cardinal", prompt)
        self.assertIn("000 up", prompt)
        self.assertIn("090 screen-right", prompt)
        self.assertIn("180 down", prompt)
        self.assertIn("270 screen-left", prompt)
        self.assertIn("viewer's image edges", prompt)
        self.assertIn("look-mechanics.md", prompt)
        self.assertIn("nose tip", prompt)
        self.assertIn("Do not rotate", prompt)

    def test_look_cardinals_retry_exists(self):
        """look-cardinals retry prompt must exist and be rich."""
        retry = self.prompts["look-cardinals-retry.md"]
        self.assertIn("four-cardinal", retry)
        self.assertIn("look-mechanics.md", retry)

    def test_look_row_prompts_include_axis_contract(self):
        """look-row-9 and look-row-10 must include DIRECTION TARGETS with
        per-slot axis requirements."""
        for state in ("look-row-9", "look-row-10"):
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn("DIRECTION TARGETS", prompt)
            self.assertIn("not as pixel-level landmark gates", prompt)

    def test_look_row_9_axis_targets(self):
        """look-row-9 must list the correct direction targets."""
        prompt = self.prompts["rows/look-row-9.md"]
        self.assertIn("`000`: vertical UP", prompt)
        self.assertIn("`090`: horizontal SCREEN-RIGHT", prompt)
        self.assertIn("`157.5`: horizontal SCREEN-RIGHT and vertical DOWN", prompt)

    def test_look_row_10_axis_targets(self):
        """look-row-10 must list the correct direction targets."""
        prompt = self.prompts["rows/look-row-10.md"]
        self.assertIn("`180`: vertical DOWN", prompt)
        self.assertIn("`270`: horizontal SCREEN-LEFT", prompt)
        self.assertIn("`337.5`: horizontal SCREEN-LEFT and vertical UP", prompt)

    def test_look_row_prompts_include_screen_coordinate_contract(self):
        """look-row prompts must include SCREEN-COORDINATE LOCK."""
        prompt9 = self.prompts["rows/look-row-9.md"]
        self.assertIn("SCREEN-COORDINATE LOCK", prompt9)
        self.assertIn("screen-right means the viewer's right", prompt9)
        prompt10 = self.prompts["rows/look-row-10.md"]
        self.assertIn("SCREEN-COORDINATE LOCK", prompt10)
        self.assertIn("screen-left means the viewer's left", prompt10)

    def test_look_row_prompts_include_layout_contract(self):
        """look-row prompts must include HARD LAYOUT AND CONTINUITY CONTRACT."""
        for state in ("look-row-9", "look-row-10"):
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn("HARD LAYOUT AND CONTINUITY CONTRACT", prompt)
            self.assertIn("DETERMINISTIC REGISTRATION", prompt)
            self.assertIn("shared scale and baseline", prompt)
            self.assertIn("same body height, head size, baseline", prompt)

    def test_look_row_prompts_include_boundary_contract(self):
        """look-row prompts must include ROW-BOUNDARY LOCK."""
        prompt9 = self.prompts["rows/look-row-9.md"]
        self.assertIn("ROW-BOUNDARY LOCK", prompt9)
        self.assertIn("157.5 must be one even 22.5-degree step before 180", prompt9)
        prompt10 = self.prompts["rows/look-row-10.md"]
        self.assertIn("ROW-BOUNDARY LOCK", prompt10)
        self.assertIn("180 must continue directly from row 9's 157.5", prompt10)
        self.assertIn("337.5 must be one even 22.5-degree step before 000", prompt10)

    def test_look_row_prompts_include_pre_return_check(self):
        """look-row prompts must include PRE-RETURN CHECK."""
        for state in ("look-row-9", "look-row-10"):
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn("PRE-RETURN CHECK", prompt)
            self.assertIn("reject this result", prompt)

    def test_look_row_prompts_reference_look_mechanics(self):
        """look-row prompts must reference qa/look-mechanics.md."""
        for state in ("look-row-9", "look-row-10"):
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn("look-mechanics.md", prompt)

    def test_look_row_prompts_include_coherent_synthesis_lock(self):
        """look-row prompts must include COHERENT SYNTHESIS LOCK."""
        for state in ("look-row-9", "look-row-10"):
            prompt = self.prompts[f"rows/{state}.md"]
            self.assertIn("COHERENT SYNTHESIS LOCK", prompt)

    def test_look_row_prompts_include_direction_order(self):
        """look-row prompts must list the exact direction order."""
        prompt9 = self.prompts["rows/look-row-9.md"]
        self.assertIn("000, 022.5, 045, 067.5, 090, 112.5, 135, 157.5", prompt9)
        prompt10 = self.prompts["rows/look-row-10.md"]
        self.assertIn("180, 202.5, 225, 247.5, 270, 292.5, 315, 337.5", prompt10)

    def test_look_row_retry_prompts_are_rich(self):
        """look-row retry prompts must include contracts."""
        for state in ("look-row-9", "look-row-10"):
            retry = self.prompts[f"row-retries/{state}.md"]
            self.assertIn("DIRECTION TARGETS", retry)
            self.assertIn("HARD LAYOUT AND CONTINUITY CONTRACT", retry)
            self.assertIn("PRE-RETURN CHECK", retry)


class PreparePetRunDirectionalTest(unittest.TestCase):
    """Verify prepare_pet_run.py also includes directional and variation guidance."""

    def prepare_run(self, temporary_directory: str) -> Path:
        run_dir = Path(temporary_directory) / "run"
        subprocess.run(
            [
                sys.executable,
                str(PREPARE),
                "--pet-name",
                "Directional Test",
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

    def test_directional_states_include_facing_guidance(self):
        """running-right and running-left prompts must include body rotation
        and gaze direction guidance."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = self.prepare_run(temporary_directory)
            for state in DIRECTIONAL_STATES:
                prompt = (run_dir / "prompts" / "rows" / f"{state}.md").read_text()
                self.assertIn(
                    "rotate the body",
                    prompt,
                    f"prepare_pet_run row prompt for {state} must call for body rotation",
                )
                self.assertIn(
                    "gaze direction",
                    prompt,
                    f"prepare_pet_run row prompt for {state} must call for gaze direction change",
                )

    def test_identity_encourages_variation(self):
        """prepare_pet_run row prompts must encourage pose/expression/direction
        variation."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = self.prepare_run(temporary_directory)
            for state in STANDARD_STATES:
                prompt = (run_dir / "prompts" / "rows" / f"{state}.md").read_text()
                self.assertIn(
                    "Vary pose, expression",
                    prompt,
                    f"prepare_pet_run row prompt for {state} must encourage variation",
                )

    def test_state_requirements_include_variation_for_non_idle(self):
        """Non-idle state requirements must call for pose/expression variation."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = self.prepare_run(temporary_directory)
            for state in STANDARD_STATES:
                if state == "idle":
                    continue
                prompt = (run_dir / "prompts" / "rows" / f"{state}.md").read_text()
                self.assertIn(
                    "Vary",
                    prompt,
                    f"prepare_pet_run state requirements for {state} must call for variation",
                )

    def test_row_prompts_include_composition_guidance(self):
        """prepare_pet_run row prompts must include composition/sizing guidance."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = self.prepare_run(temporary_directory)
            for state in STANDARD_STATES:
                prompt = (run_dir / "prompts" / "rows" / f"{state}.md").read_text()
                self.assertIn(
                    "Composition:",
                    prompt,
                    f"prepare_pet_run row prompt for {state} must include composition guidance",
                )
                self.assertIn(
                    "75%",
                    prompt,
                    f"prepare_pet_run row prompt for {state} must specify minimum fill",
                )

    def test_directional_states_no_mirroring_override(self):
        """prepare_pet_run running-right and running-left must NOT include
        DIRECTIONAL OVERRIDE mirroring instructions."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = self.prepare_run(temporary_directory)
            right = (run_dir / "prompts" / "rows" / "running-right.md").read_text()
            self.assertNotIn("DIRECTIONAL OVERRIDE", right)
            self.assertNotIn("mirror the entire character horizontally", right)

            left = (run_dir / "prompts" / "rows" / "running-left.md").read_text()
            self.assertNotIn("DIRECTIONAL OVERRIDE", left)
            self.assertNotIn("mirror the entire character horizontally", left)


class BuildCurrentPromptsAnalysisTest(unittest.TestCase):
    """Verify build_current_prompts injects pet-specific analysis text."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = _make_project(self._tmp.name)
        self.request = {
            "pet_id": "sample-pet",
            "pet_notes": "A compact mascot with a red cloak and bicorne hat.",
            "style_contract": "Pet-safe painterly sprite with crisp edges.",
            "chroma_key": {"hex": "#00FFFF", "name": "cyan"},
            "rows": [
                {"state": "idle", "frames": 6},
                {"state": "running-right", "frames": 8},
                {"state": "running-left", "frames": 8},
                {"state": "waving", "frames": 4},
                {"state": "jumping", "frames": 5},
                {"state": "failed", "frames": 8},
                {"state": "waiting", "frames": 6},
                {"state": "running", "frames": 6},
                {"state": "review", "frames": 6},
            ],
        }
        self.analysis = (
            "Napoleon faces left in the base reference. The pointing arm and "
            "red cloak are identity-authoritative and must NOT switch sides "
            "when generating right-facing states. Instead, rotate the body "
            "and head without mirroring props."
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_analysis_not_injected_by_default(self):
        """Default build_current_prompts call must NOT include analysis section."""
        prompts = build_current_prompts(self.project, self.request)
        for path, content in prompts.items():
            self.assertNotIn(
                "PET-SPECIFIC DESIGN NOTES",
                content,
                f"prompt {path} must not include analysis section by default",
            )

    def test_analysis_not_injected_when_empty(self):
        """Empty analysis_text must NOT produce analysis section."""
        prompts = build_current_prompts(
            self.project, self.request, analysis_text=""
        )
        for path, content in prompts.items():
            self.assertNotIn(
                "PET-SPECIFIC DESIGN NOTES",
                content,
                f"prompt {path} must not include analysis section for empty text",
            )

    def test_analysis_injected_into_row_prompts(self):
        """Row prompts must include PET-SPECIFIC DESIGN NOTES when analysis is provided."""
        prompts = build_current_prompts(
            self.project, self.request, analysis_text=self.analysis
        )
        for state in STANDARD_STATES:
            content = prompts[f"rows/{state}.md"]
            self.assertIn(
                "PET-SPECIFIC DESIGN NOTES",
                content,
                f"rows/{state}.md must include analysis section",
            )
            self.assertIn(
                "identity-authoritative",
                content,
                f"rows/{state}.md must include analysis text",
            )
            self.assertIn(
                "overrides generic guidance",
                content,
                f"rows/{state}.md must declare analysis authority",
            )

    def test_analysis_injected_into_row_retries(self):
        """Row retry prompts must also include analysis."""
        prompts = build_current_prompts(
            self.project, self.request, analysis_text=self.analysis
        )
        for state in STANDARD_STATES:
            content = prompts[f"row-retries/{state}.md"]
            self.assertIn("PET-SPECIFIC DESIGN NOTES", content)

    def test_analysis_injected_into_look_cardinals(self):
        """Look cardinals prompt must include analysis."""
        prompts = build_current_prompts(
            self.project, self.request, analysis_text=self.analysis
        )
        content = prompts["look-cardinals.md"]
        self.assertIn("PET-SPECIFIC DESIGN NOTES", content)
        self.assertIn("identity-authoritative", content)

    def test_analysis_injected_into_look_cardinals_retry(self):
        """Look cardinals retry prompt must include analysis."""
        prompts = build_current_prompts(
            self.project, self.request, analysis_text=self.analysis
        )
        content = prompts["look-cardinals-retry.md"]
        self.assertIn("PET-SPECIFIC DESIGN NOTES", content)

    def test_analysis_not_injected_into_base_or_repairs(self):
        """Base-pet and look-anchor-repair prompts should NOT include analysis
        (they are identity-only and don't need per-state guidance)."""
        prompts = build_current_prompts(
            self.project, self.request, analysis_text=self.analysis
        )
        self.assertNotIn("PET-SPECIFIC DESIGN NOTES", prompts["base-pet.md"])
        for direction in ("000", "090", "180", "270"):
            content = prompts[f"look-anchor-repairs/{direction}.md"]
            self.assertNotIn(
                "PET-SPECIFIC DESIGN NOTES",
                content,
                f"look-anchor-repairs/{direction}.md should not include analysis",
            )

    def test_analysis_changes_prompt_hashes(self):
        """Prompts with analysis must have different hashes than without."""
        without = build_current_prompts(self.project, self.request)
        with_analysis = build_current_prompts(
            self.project, self.request, analysis_text=self.analysis
        )
        for state in STANDARD_STATES:
            self.assertNotEqual(
                without[f"rows/{state}.md"],
                with_analysis[f"rows/{state}.md"],
                f"rows/{state}.md must differ when analysis is provided",
            )


if __name__ == "__main__":
    unittest.main()
