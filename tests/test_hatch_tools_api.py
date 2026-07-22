from __future__ import annotations

import json
import tempfile
import unittest
from math import nan
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from omnipet.hatch.atlas import (
    AssembleExtendedAtlasConfig,
    ComposeAtlasConfig,
    DeriveRunningLeftConfig,
    assemble_extended_atlas,
    compose_atlas,
    compose_cardinal_anchor_strip,
    derive_running_left,
)
from omnipet.hatch.chroma import DespillConfig, despill
from omnipet.hatch.directions import (
    BlindQaSheetConfig,
    BlindVerdictConfig,
    CombineVerdictsConfig,
    ContinuityConfig,
    DirectionQaSheetConfig,
    combine_verdicts,
    make_direction_blind_qa_sheet,
    make_direction_qa_sheet,
    measure_direction_continuity,
    validate_blind_verdicts,
)
from omnipet.hatch.extract import (
    CardinalAnchorsConfig,
    ExtractStripFramesConfig,
    extract_cardinal_anchors,
    extract_strip_frames,
)
from omnipet.hatch.inspect import (
    ContactSheetConfig,
    InspectFramesConfig,
    PreviewConfig,
    inspect_frames,
    make_contact_sheet,
    render_animation_previews,
)
from omnipet.hatch.prepare import PrepareRunInputs, prepare_run
from omnipet.hatch.validation import ValidateAtlasConfig, validate_atlas
from omnipet.hatch import HatchExecutionError


CELL = (192, 208)
REPO_ROOT = Path(__file__).resolve().parents[1]
ROW_COUNTS = {
    "idle": 6,
    "running-right": 8,
    "running-left": 8,
    "waving": 4,
    "jumping": 5,
    "failed": 8,
    "waiting": 6,
    "running": 6,
    "review": 6,
}


def sprite(color: str = "#cc3344", *, offset: int = 0) -> Image.Image:
    image = Image.new("RGBA", CELL, (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((56 + offset, 54, 135 + offset, 177), fill=color)
    return image


def write_frames(root: Path) -> None:
    rows = []
    for state, count in ROW_COUNTS.items():
        state_dir = root / state
        state_dir.mkdir(parents=True)
        outputs = []
        for index in range(count):
            output = state_dir / f"{index:02d}.png"
            sprite(offset=index % 2).save(output)
            outputs.append(str(output))
        rows.append({"state": state, "frames": outputs, "method": "components"})
    (root / "frames-manifest.json").write_text(
        json.dumps({"chroma_key": {"rgb": [0, 255, 0]}, "rows": rows}), encoding="utf-8"
    )


def write_atlas(path: Path, *, rows: int) -> None:
    atlas = Image.new("RGBA", (1536, rows * CELL[1]), (0, 0, 0, 0))
    counts = [6, 8, 8, 4, 5, 8, 6, 6, 6] + ([8, 8] if rows == 11 else [])
    for row, count in enumerate(counts):
        for column in range(count):
            atlas.alpha_composite(sprite(offset=(row + column) % 3), (column * CELL[0], row * CELL[1]))
    if rows == 11:
        atlas.alpha_composite(sprite(), (6 * CELL[0], 0))
    atlas.save(path)


def write_strip(path: Path, count: int) -> None:
    strip = Image.new("RGBA", (count * CELL[0], CELL[1]), "#00ff00")
    for index in range(count):
        strip.alpha_composite(sprite(offset=index % 2), (index * CELL[0], 0))
    strip.save(path)


class HatchToolsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_extract_operations_smoke_and_invalid_inputs(self) -> None:
        decoded = self.root / "decoded"
        decoded.mkdir()
        write_strip(decoded / "idle.png", 6)
        extracted = extract_strip_frames(
            ExtractStripFramesConfig(decoded, self.root / "frames", states=("idle",), method="slots")
        )
        self.assertEqual(extracted.states, ("idle",))
        self.assertTrue(extracted.manifest.is_file())

        cardinal_strip = self.root / "cardinals.png"
        write_strip(cardinal_strip, 4)
        anchors = extract_cardinal_anchors(
            CardinalAnchorsConfig(cardinal_strip, self.root / "anchors", self.root / "anchors.json", "#00FF00")
        )
        self.assertTrue(anchors.ok)
        self.assertEqual(len(anchors.anchors), 4)

        with self.assertRaises(ValueError):
            extract_strip_frames(ExtractStripFramesConfig(decoded, self.root / "bad", method="magic"))  # type: ignore[arg-type]
        with self.assertRaises(FileNotFoundError):
            extract_cardinal_anchors(
                CardinalAnchorsConfig(self.root / "missing.png", self.root / "x", self.root / "x.json", "#00FF00")
            )

    def test_inspection_operations_smoke_and_invalid_inputs(self) -> None:
        frames = self.root / "frames"
        write_frames(frames)
        review = inspect_frames(InspectFramesConfig(frames, self.root / "review.json"))
        self.assertTrue(review.ok)
        previews = render_animation_previews(PreviewConfig(frames, self.root / "previews"))
        self.assertEqual(len(previews.previews), 9)

        atlas = self.root / "atlas.png"
        write_atlas(atlas, rows=9)
        sheet = make_contact_sheet(ContactSheetConfig(atlas, self.root / "sheet.png", scale=0.25))
        self.assertTrue(sheet.output.is_file())

        with self.assertRaises(FileNotFoundError):
            inspect_frames(InspectFramesConfig(self.root / "missing", self.root / "bad.json"))
        with self.assertRaises(FileNotFoundError):
            render_animation_previews(PreviewConfig(self.root / "missing", self.root / "bad"))
        with self.assertRaises(ValueError):
            make_contact_sheet(ContactSheetConfig(atlas, self.root / "bad.png", scale=0))

    def test_atlas_operations_smoke_and_invalid_inputs(self) -> None:
        frames = self.root / "frames"
        write_frames(frames)
        composed = compose_atlas(ComposeAtlasConfig(self.root / "base.png", frames_root=frames))
        self.assertEqual(composed.size, (1536, 1872))

        anchors = self.root / "anchors"
        anchors.mkdir()
        for label in ("000", "090", "180", "270"):
            sprite().save(anchors / f"{label}.png")
        strip = compose_cardinal_anchor_strip(anchors_dir=anchors, output=self.root / "approved.png")
        self.assertEqual(strip.size, (768, 208))

        cells = self.root / "look"
        cells.mkdir()
        for index, label in enumerate((
            "000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5",
            "180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5",
        )):
            sprite(offset=index % 3).save(cells / f"{label}.png")
        extended = assemble_extended_atlas(
            AssembleExtendedAtlasConfig(self.root / "base.png", self.root / "extended.png", look_cells_dir=cells)
        )
        self.assertEqual(extended.size, (1536, 2288))

        run_dir = self.root / "run"
        (run_dir / "decoded").mkdir(parents=True)
        write_strip(run_dir / "decoded" / "running-right.png", 8)
        jobs = {
            "jobs": [
                {"id": "running-right", "status": "complete"},
                {"id": "running-left", "status": "pending", "mirror_policy": {"may_derive_from": "running-right"}},
            ]
        }
        (run_dir / "imagegen-jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
        derived = derive_running_left(DeriveRunningLeftConfig(run_dir, approved=True, decision_note="Symmetric pet."))
        self.assertTrue(derived.output.is_file())

        with self.assertRaises(ValueError):
            compose_atlas(ComposeAtlasConfig(self.root / "bad.png"))
        with self.assertRaises(FileNotFoundError):
            compose_cardinal_anchor_strip(anchors_dir=self.root / "missing", output=self.root / "bad.png")
        with self.assertRaises(ValueError):
            assemble_extended_atlas(AssembleExtendedAtlasConfig(self.root / "base.png", self.root / "bad.png"))
        with self.assertRaises(ValueError):
            derive_running_left(DeriveRunningLeftConfig(run_dir, approved=False, decision_note="No."))

    def test_direction_operations_smoke_and_invalid_inputs(self) -> None:
        atlas = self.root / "extended.png"
        write_atlas(atlas, rows=11)
        qa = make_direction_qa_sheet(DirectionQaSheetConfig(atlas, self.root / "direction.png"))
        self.assertTrue(qa.output.is_file())
        blind = make_direction_blind_qa_sheet(
            BlindQaSheetConfig(atlas, self.root / "blind.png", self.root / "answers.json")
        )
        self.assertEqual(blind.answer_key["schema_version"], 3)
        continuity = measure_direction_continuity(ContinuityConfig(atlas, self.root / "continuity.json"))
        self.assertEqual(len(continuity.report["pairs"]), 16)

        pairs = blind.answer_key["pairs"]
        verdict_payload = {
            "pairs": [
                {"pair": pair["pair"], "A": pair["A"]["expected_direction"], "B": pair["B"]["expected_direction"]}
                for pair in pairs
            ]
        }
        verdict_paths = []
        for index in range(3):
            path = self.root / f"verdict-{index}.json"
            path.write_text(json.dumps(verdict_payload), encoding="utf-8")
            verdict_paths.append(path)
        combined = combine_verdicts(CombineVerdictsConfig(tuple(verdict_paths), self.root / "combined.json"))
        self.assertEqual(len(combined.report["pairs"]), len(pairs))
        validated = validate_blind_verdicts(
            BlindVerdictConfig(self.root / "answers.json", self.root / "combined.json", self.root / "validated.json")
        )
        self.assertTrue(validated.ok)

        with self.assertRaises(FileNotFoundError):
            make_direction_qa_sheet(DirectionQaSheetConfig(self.root / "missing.png", self.root / "bad.png"))
        with self.assertRaises(FileNotFoundError):
            make_direction_blind_qa_sheet(
                BlindQaSheetConfig(self.root / "missing.png", self.root / "bad.png", self.root / "bad.json")
            )
        with self.assertRaises(ValueError):
            measure_direction_continuity(ContinuityConfig(atlas, self.root / "bad.json", diff_outlier_ratio=0))
        with self.assertRaises(ValueError):
            combine_verdicts(CombineVerdictsConfig(tuple(verdict_paths[:2]), self.root / "bad.json"))
        with self.assertRaises(FileNotFoundError):
            validate_blind_verdicts(
                BlindVerdictConfig(self.root / "missing.json", self.root / "combined.json", self.root / "bad.json")
            )

    def test_chroma_and_validation_smoke_and_invalid_inputs(self) -> None:
        atlas = self.root / "atlas.png"
        write_atlas(atlas, rows=9)
        cleaned = despill(DespillConfig(atlas, self.root / "clean.png", "#00FF00", json_out=self.root / "despill.json"))
        self.assertTrue(cleaned.output.is_file())
        self.assertTrue(cleaned.report["alpha_preserved"])
        validation = validate_atlas(ValidateAtlasConfig(cleaned.output, self.root / "validation.json"))
        self.assertTrue(validation.ok)

        with self.assertRaises(ValueError):
            despill(DespillConfig(atlas, self.root / "bad.png", "green"))
        with self.assertRaises(FileNotFoundError):
            validate_atlas(ValidateAtlasConfig(self.root / "missing.png"))

    def test_vendor_failures_never_leak_system_exit(self) -> None:
        atlas = self.root / "atlas.png"
        write_atlas(atlas, rows=9)
        frames = self.root / "frames"
        write_frames(frames)
        run_dir = self.root / "run"
        run_dir.mkdir()
        (run_dir / "imagegen-jobs.json").write_text('{"jobs": []}', encoding="utf-8")

        cases = (
            ("omnipet.hatch.atlas.compose_vendor.compose_from_source_atlas", ComposeAtlasConfig(self.root / "out.png", source_atlas=atlas), compose_atlas),
            ("omnipet.hatch.extract.strip_vendor.extract_state", ExtractStripFramesConfig(self.root, self.root / "out-frames", states=("idle",)), extract_strip_frames),
            ("omnipet.hatch.inspect.preview_vendor.load_frames", PreviewConfig(frames, self.root / "previews"), render_animation_previews),
            ("omnipet.hatch.atlas.mirror_vendor.load_manifest", DeriveRunningLeftConfig(run_dir, True, "Safe."), derive_running_left),
        )
        (self.root / "idle.png").write_bytes(atlas.read_bytes())
        for target, config, operation in cases:
            with self.subTest(target=target), patch(target, side_effect=SystemExit("vendor rejected input")):
                with self.assertRaises(HatchExecutionError):
                    operation(config)

        with patch("omnipet.hatch.atlas.extended_vendor.load_registration_scale", side_effect=RuntimeError("broken registration")):
            registered = self.root / "registered.png"
            Image.new("RGBA", (1536, 208), (0, 0, 0, 0)).save(registered)
            row = self.root / "row.png"
            write_strip(row, 8)
            registration = self.root / "registration.json"
            registration.write_text('{"scale": 1}', encoding="utf-8")
            with self.assertRaises(HatchExecutionError):
                assemble_extended_atlas(AssembleExtendedAtlasConfig(
                    atlas, self.root / "extended.png", registered_row_9=registered,
                    look_row_10=row, row_9_registration=registration,
                ))

    def test_keyboard_interrupt_is_not_translated(self) -> None:
        atlas = self.root / "atlas.png"
        write_atlas(atlas, rows=9)
        with patch("omnipet.hatch.atlas.compose_vendor.compose_from_source_atlas", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                compose_atlas(ComposeAtlasConfig(self.root / "out.png", source_atlas=atlas))

    def test_validate_atlas_rejects_invalid_numeric_configuration_and_empty_atlas(self) -> None:
        atlas = self.root / "atlas.png"
        write_atlas(atlas, rows=9)
        invalid = (
            {"min_used_pixels": True},
            {"min_used_pixels": 0},
            {"near_opaque_threshold": nan},
            {"near_opaque_threshold": 1.1},
            {"chroma_fringe_edge_radius": -1},
            {"chroma_fringe_alpha_minimum": 256},
            {"max_chroma_leak_pixels": -1},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                validate_atlas(ValidateAtlasConfig(atlas, **changes))

        empty = self.root / "empty.png"
        Image.new("RGBA", (1536, 1872), (0, 0, 0, 0)).save(empty)
        result = validate_atlas(ValidateAtlasConfig(empty, min_used_pixels=1))
        self.assertFalse(result.ok)
        self.assertTrue(result.report["errors"])

    def test_unknown_direction_gate_is_rejected_as_malformed(self) -> None:
        answer = self.root / "answers.json"
        answer.write_text(json.dumps({"pairs": [{
            "pair": "horizontal-1", "axis": "horizontal", "gate": "hrad",
            "A": {"expected_direction": "screen-left", "source_direction": "270"},
            "B": {"expected_direction": "screen-right", "source_direction": "090"},
        }]}), encoding="utf-8")
        verdict = self.root / "verdict.json"
        verdict.write_text(json.dumps({"pairs": [{
            "pair": "horizontal-1", "A": "screen-right", "B": "screen-left",
        }]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            validate_blind_verdicts(BlindVerdictConfig(answer, verdict, self.root / "result.json"))

    def test_output_symlink_leaf_is_rejected(self) -> None:
        atlas = self.root / "atlas.png"
        write_atlas(atlas, rows=9)
        external = self.root / "external.png"
        external.write_bytes(b"unchanged")
        output = self.root / "output.png"
        output.symlink_to(external)
        with self.assertRaises(ValueError):
            despill(DespillConfig(atlas, output, "#00FF00"))
        self.assertEqual(external.read_bytes(), b"unchanged")

    def test_operations_create_missing_nested_output_parents(self) -> None:
        prepared = prepare_run(PrepareRunInputs(
            pet_id="nested",
            display_name="Nested",
            description="A nested output pet.",
            style_preset="pixel",
            style_notes="",
            output_dir=self.root / "runs" / "nested" / "run",
        ))
        self.assertTrue(prepared.jobs.is_file())

        atlas = self.root / "atlas.png"
        write_atlas(atlas, rows=9)
        composed = compose_atlas(ComposeAtlasConfig(
            self.root / "final" / "images" / "spritesheet.png",
            source_atlas=atlas,
        ))
        self.assertTrue(composed.output.is_file())

        extended = self.root / "extended.png"
        write_atlas(extended, rows=11)
        sheet = make_direction_qa_sheet(DirectionQaSheetConfig(
            extended,
            self.root / "qa" / "directions" / "sheet.png",
        ))
        self.assertTrue(sheet.output.is_file())

    def test_output_rejects_symlink_ancestor_and_parent_traversal(self) -> None:
        atlas = self.root / "atlas.png"
        write_atlas(atlas, rows=9)
        external = self.root / "external"
        external.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            compose_atlas(ComposeAtlasConfig(
                linked / "nested" / "spritesheet.png",
                source_atlas=atlas,
            ))
        with self.assertRaises(ValueError):
            compose_atlas(ComposeAtlasConfig(
                self.root / "final" / ".." / "spritesheet.png",
                source_atlas=atlas,
            ))

    def test_distributed_public_files_do_not_name_external_runtime_home(self) -> None:
        for root in (REPO_ROOT / "NOTICE", REPO_ROOT / "src" / "omnipet" / "_vendor" / "hatch"):
            paths = (root,) if root.is_file() else tuple(
                path for path in root.rglob("*")
                if path.is_file() and path.suffix in {".json", ".md", ".py", ".txt", ""}
            )
            for path in paths:
                with self.subTest(path=path):
                    self.assertNotIn("CODEX_" + "HOME", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
