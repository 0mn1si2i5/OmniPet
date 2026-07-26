import hashlib
import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from omnipet.cli import main
from omnipet.approvals import _required_paths
from omnipet.hatch.directions import ContinuityConfig, measure_direction_continuity
from omnipet.package import PackageError, check_package
from omnipet.review_resolution import (
    ResolutionError,
    create_warning_resolution,
    resolution_artifact_paths,
    validate_report_resolutions,
)


class WarningResolutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run_dir = self.root / ".omnipet/runs/test-pet"
        self.report = self.run_dir / "qa/package-generated/continuity.json"
        self.evidence = self.run_dir / "qa/package-generated/direction-sheet.png"
        self.report.parent.mkdir(parents=True)
        self.atlas = self.run_dir / "final/spritesheet-extended.webp"
        self.atlas.parent.mkdir()
        self.atlas.write_bytes(b"atlas")
        self.evidence.write_bytes(b"review pixels")
        self.report.write_text(json.dumps({
            "ok": True,
            "atlasSha256": hashlib.sha256(self.atlas.read_bytes()).hexdigest(),
            "reviewRequired": True,
            "warnings": [
                {
                    "id": "direction-continuity:pair-000-to-022.5:center-shift-high",
                    "text": "000->022.5 center shift is high",
                },
                {
                    "id": "direction-continuity:direction-090:transparent-interior-hole-rows",
                    "text": "090 has transparent interior hole rows",
                },
            ],
        }, sort_keys=True) + "\n", encoding="utf-8")

    def _verdict(self, **changes):
        payload = {
            "schema_version": 1,
            "warning_ids": [
                "direction-continuity:pair-000-to-022.5:center-shift-high",
                "direction-continuity:direction-090:transparent-interior-hole-rows",
            ],
            "reviewer": "release-reviewer",
            "disposition": "pass",
            "note": "The pose change is intentional and the apparent hole is enclosed negative space.",
            "visual_evidence": [{
                "path": "qa/package-generated/direction-sheet.png",
                "sha256": hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
            }],
        }
        payload.update(changes)
        path = self.root / "continuity-resolution.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_continuity_warnings_have_stable_ids_and_keep_text(self):
        atlas = self.root / "atlas.png"
        Image.new("RGBA", (1536, 2288), (1, 2, 3, 255)).save(atlas)
        metrics = {
            "diffPixels": 1,
            "centerDelta": 9.0,
            "areaRatio": 1.0,
        }
        with patch(
            "omnipet.hatch.directions.continuity_vendor.pair_metric",
            return_value=metrics,
        ), patch(
            "omnipet.hatch.directions.continuity_vendor.transparent_hole_rows",
            side_effect=lambda cell: [4] if cell.getbbox() == (0, 0, 192, 208) else [],
        ):
            first = measure_direction_continuity(
                ContinuityConfig(atlas, self.root / "first.json")
            ).report
            second = measure_direction_continuity(
                ContinuityConfig(atlas, self.root / "second.json")
            ).report
        self.assertEqual(first["warnings"], second["warnings"])
        self.assertIn({
            "id": "direction-continuity:direction-000:transparent-interior-hole-rows",
            "text": "000 has transparent interior hole rows",
        }, first["warnings"])
        self.assertIn({
            "id": "direction-continuity:pair-000-to-022.5:center-shift-high",
            "text": "000->022.5 center shift is high",
        }, first["warnings"])

    def test_resolution_is_closed_exact_and_does_not_mutate_report(self):
        before = self.report.read_bytes()
        stored = create_warning_resolution(
            self.run_dir,
            "qa/package-generated/continuity.json",
            self._verdict(),
        )
        self.assertEqual(self.report.read_bytes(), before)
        payload = json.loads(stored.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {
            "schema_version", "source_report", "warning_ids", "reviewer",
            "disposition", "note", "visual_evidence", "created_at",
        })
        self.assertEqual(payload["source_report"], {
            "path": "qa/package-generated/continuity.json",
            "sha256": hashlib.sha256(before).hexdigest(),
        })
        self.assertEqual(
            validate_report_resolutions(
                self.run_dir, "qa/package-generated/continuity.json"
            ),
            (stored,),
        )
        self.assertEqual(
            resolution_artifact_paths(
                self.run_dir, "qa/package-generated/continuity.json"
            ),
            {
                stored.relative_to(self.run_dir).as_posix(),
                "qa/package-generated/direction-sheet.png",
            },
        )
        package_paths = _required_paths(self.run_dir, "package")
        self.assertIn(stored.relative_to(self.run_dir).as_posix(), package_paths)
        self.assertIn("qa/package-generated/direction-sheet.png", package_paths)

    def test_duplicate_resolution_artifacts_are_rejected(self):
        stored = create_warning_resolution(
            self.run_dir,
            "qa/package-generated/continuity.json",
            self._verdict(),
        )
        duplicate = stored.with_name("duplicate.json")
        duplicate.write_bytes(stored.read_bytes())
        with self.assertRaises(ResolutionError):
            validate_report_resolutions(
                self.run_dir, "qa/package-generated/continuity.json"
            )

    def test_duplicate_unknown_and_incomplete_verdicts_are_rejected(self):
        invalid = (
            {"warning_ids": [
                "direction-continuity:pair-000-to-022.5:center-shift-high",
                "direction-continuity:pair-000-to-022.5:center-shift-high",
            ]},
            {"warning_ids": [
                "direction-continuity:pair-000-to-022.5:center-shift-high",
                "unknown",
            ]},
            {"warning_ids": [
                "direction-continuity:pair-000-to-022.5:center-shift-high",
            ]},
            {"reviewer": ""},
            {"note": ""},
        )
        for mutation in invalid:
            with self.subTest(mutation=mutation), self.assertRaises(ResolutionError):
                create_warning_resolution(
                    self.run_dir,
                    "qa/package-generated/continuity.json",
                    self._verdict(**mutation),
                )

    def test_failed_resolution_is_recorded_but_does_not_clear_the_block(self):
        stored = create_warning_resolution(
            self.run_dir,
            "qa/package-generated/continuity.json",
            self._verdict(disposition="fail"),
        )
        self.assertEqual(
            json.loads(stored.read_text(encoding="utf-8"))["disposition"],
            "fail",
        )
        with self.assertRaises(ResolutionError):
            validate_report_resolutions(
                self.run_dir, "qa/package-generated/continuity.json"
            )

    def test_stale_report_and_stale_evidence_restore_the_block(self):
        create_warning_resolution(
            self.run_dir,
            "qa/package-generated/continuity.json",
            self._verdict(),
        )
        self.report.write_bytes(self.report.read_bytes() + b" ")
        with self.assertRaises(ResolutionError):
            validate_report_resolutions(
                self.run_dir, "qa/package-generated/continuity.json"
            )

    def test_stale_atlas_restores_the_block(self):
        create_warning_resolution(
            self.run_dir,
            "qa/package-generated/continuity.json",
            self._verdict(),
        )
        self.atlas.write_bytes(b"changed atlas")
        with self.assertRaises(ResolutionError):
            validate_report_resolutions(
                self.run_dir, "qa/package-generated/continuity.json"
            )
        self.report.write_bytes(self.report.read_bytes()[:-1])
        self.evidence.write_bytes(b"changed")
        with self.assertRaises(ResolutionError):
            validate_report_resolutions(
                self.run_dir, "qa/package-generated/continuity.json"
            )

    def test_cli_stores_resolution_under_run_qa(self):
        project = SimpleNamespace(
            pet_id="test-pet",
            repository_root=self.root,
        )
        stdout = StringIO()
        with patch("omnipet.cli.load_pet_project", return_value=project), patch(
            "sys.stdout", stdout
        ):
            code = main([
                "qa", "resolve", "test-pet",
                "--report", "qa/package-generated/continuity.json",
                "--verdict-file", str(self._verdict()),
                "--repo-root", str(self.root),
            ])
        self.assertEqual(code, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["report"], "qa/package-generated/continuity.json")
        self.assertTrue(
            (self.run_dir / output["resolution"]).is_file()
        )

    def test_package_accepts_only_current_passing_resolution(self):
        project_root = self.root / "pets/test-pet"
        project_root.mkdir(parents=True)
        final = self.run_dir / "final"
        reviewed = self.run_dir / "qa/package-reviewed"
        reviewed.mkdir()
        (final / "spritesheet-extended.webp").write_bytes(b"atlas")
        (final / "pet.json").write_text(json.dumps({
            "id": "test-pet",
            "displayName": "Test Pet",
            "description": "A test pet.",
            "spriteVersionNumber": 2,
            "spritesheetPath": "spritesheet.webp",
        }), encoding="utf-8")
        (self.run_dir / "qa/package-generated/validation.json").write_text(
            '{"ok":true,"sprite_version_number":2,"width":1536,"height":2288,"errors":[]}'
        )
        (self.run_dir / "qa/package-generated/despill.json").write_text(
            '{"ok":true,"passes":1}'
        )
        (reviewed / "blind-validation.json").write_text(
            '{"ok":true,"unconfirmed":[]}'
        )
        (reviewed / "final-direction-semantics.json").write_text('{"ok":true}')
        (reviewed / "final-visual-review.json").write_text(
            '{"ok":true,"verdict":"pass"}'
        )
        project = SimpleNamespace(
            pet_id="test-pet",
            display_name="Test Pet",
            description="A test pet.",
            root=project_root,
            repository_root=self.root,
            spritesheet_path=project_root / "dist/spritesheet.webp",
            manifest_path=project_root / "dist/pet.json",
        )
        with patch("omnipet.package._validated_package_approval", return_value=None):
            with self.assertRaises(PackageError):
                check_package(project)
            create_warning_resolution(
                self.run_dir,
                "qa/package-generated/continuity.json",
                self._verdict(),
            )
            self.assertEqual(check_package(project)["id"], "test-pet")


if __name__ == "__main__":
    unittest.main()
