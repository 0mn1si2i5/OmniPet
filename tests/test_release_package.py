import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from omnipet.package import (
    PackageError,
    build_package_evidence,
    check_package,
    import_package_verdict,
    publish_package,
    recover_package,
)


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run_dir = self.root / ".omnipet/runs/test-pet"
        self.project_root = self.root / "pets/test-pet"
        self.run_dir.mkdir(parents=True)
        self.project_root.mkdir(parents=True)
        self.project = SimpleNamespace(
            pet_id="test-pet",
            display_name="Test Pet",
            description="A test pet.",
            root=self.project_root,
            repository_root=self.root,
            spritesheet_path=self.project_root / "dist/spritesheet.webp",
            manifest_path=self.project_root / "dist/pet.json",
        )
        (self.run_dir / "final").mkdir()
        (self.run_dir / "final/spritesheet-extended.webp").write_bytes(b"atlas")
        (self.run_dir / "final/pet.json").write_text(json.dumps({
            "id": "test-pet",
            "displayName": "Test Pet",
            "description": "A test pet.",
            "spriteVersionNumber": 2,
            "spritesheetPath": "spritesheet.webp",
        }) + "\n")
        (self.run_dir / "qa/package-generated").mkdir(parents=True)
        (self.run_dir / "qa/package-reviewed").mkdir(parents=True)
        (self.run_dir / "qa/package-generated/validation.json").write_text(json.dumps({
            "ok": True, "sprite_version_number": 2, "width": 1536, "height": 2288, "errors": [],
        }))
        (self.run_dir / "qa/package-generated/despill.json").write_text(json.dumps({"ok": True, "passes": 1}))
        (self.run_dir / "qa/package-generated/continuity.json").write_text(json.dumps({"ok": True, "reviewRequired": False, "warnings": []}))
        (self.run_dir / "qa/package-reviewed/blind-validation.json").write_text(json.dumps({
            "ok": True, "unconfirmed": [],
        }))
        (self.run_dir / "qa/package-reviewed/final-direction-semantics.json").write_text('{"ok": true}')
        (self.run_dir / "qa/package-reviewed/final-visual-review.json").write_text(json.dumps({
            "ok": True, "verdict": "pass",
        }))

    def test_check_is_read_only_and_requires_current_package_approval(self):
        before = self._snapshot(self.root)
        with patch("omnipet.package._validated_package_approval", return_value=None):
            result = check_package(self.project)
        self.assertEqual(result["spriteVersionNumber"], 2)
        self.assertEqual(self._snapshot(self.root), before)

        with patch("omnipet.package._validated_package_approval", side_effect=PackageError("stale")):
            with self.assertRaises(PackageError):
                check_package(self.project)

    def test_publish_is_exact_idempotent_and_refuses_collision(self):
        with patch("omnipet.package._validated_package_approval", return_value=None), patch(
            "omnipet.package._mark_delivered", return_value=None
        ):
            outputs = publish_package(self.project)
            self.assertEqual(outputs, (self.project.manifest_path, self.project.spritesheet_path))
            self.assertEqual(self.project.spritesheet_path.read_bytes(), b"atlas")
            self.assertEqual(json.loads(self.project.manifest_path.read_text())["spriteVersionNumber"], 2)
            first = self._snapshot(self.project_root)
            publish_package(self.project)
            self.assertEqual(self._snapshot(self.project_root), first)

            self.project.spritesheet_path.write_bytes(b"collision")
            with self.assertRaises(PackageError):
                publish_package(self.project)

    def test_publish_rolls_back_pair_when_second_replace_fails(self):
        import omnipet.package as package_module

        real_replace = package_module.os.replace
        calls = 0

        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("failure")
            return real_replace(source, destination)

        with patch("omnipet.package._validated_package_approval", return_value=None), patch(
            "omnipet.package.os.replace", side_effect=fail_second
        ):
            with self.assertRaises(PackageError):
                publish_package(self.project)
        self.assertFalse(self.project.manifest_path.exists())
        self.assertFalse(self.project.spritesheet_path.exists())

    def test_publish_refuses_partial_existing_distribution(self):
        self.project.manifest_path.parent.mkdir(parents=True)
        self.project.manifest_path.write_bytes((self.run_dir / "final/pet.json").read_bytes())
        with patch("omnipet.package._validated_package_approval", return_value=None):
            with self.assertRaises(PackageError):
                publish_package(self.project)
        self.assertFalse(self.project.spritesheet_path.exists())

    def test_crash_after_dist_backup_is_recovered_by_next_check(self):
        dist = self.project_root / "dist"
        dist.mkdir()
        (dist / ".gitkeep").write_text("")
        real_replace = os.replace
        crashed = False

        def crash_after_backup(source, destination):
            nonlocal crashed
            result = real_replace(source, destination)
            if Path(source) == dist and not crashed:
                crashed = True
                raise KeyboardInterrupt()
            return result

        with patch("omnipet.package._validated_package_approval", return_value=None), patch(
            "omnipet.package.os.replace", side_effect=crash_after_backup
        ):
            with self.assertRaises(KeyboardInterrupt):
                publish_package(self.project)
        self.assertTrue((self.run_dir / "package-publication.json").is_file())

        before = self._snapshot(self.root)
        with patch("omnipet.package._validated_package_approval", return_value=None):
            with self.assertRaises(PackageError):
                check_package(self.project)
        self.assertEqual(self._snapshot(self.root), before)

        recover_package(self.project)
        self.assertTrue((dist / ".gitkeep").is_file())
        self.assertFalse((self.run_dir / "package-publication.json").exists())

    def test_recovery_rejects_tampered_closed_journal_without_mutation(self):
        dist = self.project_root / "dist"
        stage = dist.with_name(".dist-stage")
        backup = dist.with_name(".dist-backup")
        journal = self.run_dir / "package-publication.json"
        payload = {
            "schema_version": 1, "state": "prepared", "dist": str(dist),
            "stage": str(stage), "backup": str(backup),
        }
        mutations = (
            {**payload, "stage": str(self.root / "victim")},
            {**payload, "extra": True},
            {**payload, "backup": str(dist.with_name(".other-backup"))},
        )
        for index, value in enumerate(mutations):
            with self.subTest(index=index):
                journal.write_text(json.dumps(value))
                before = self._snapshot(self.root)
                with self.assertRaises(PackageError):
                    recover_package(self.project)
                self.assertEqual(self._snapshot(self.root), before)

    def test_marker_failure_rolls_back_dist_and_workflow_files(self):
        dist = self.project_root / "dist"
        dist.mkdir()
        (dist / ".gitkeep").write_text("keep")
        workflow = self.run_dir / "workflow.json"
        marker = self.run_dir / "package-complete.json"
        workflow.write_text("before")
        before = self._snapshot(self.root)
        with patch("omnipet.package._validated_package_approval", return_value=None), patch(
            "omnipet.package._mark_delivered", side_effect=OSError("marker failed")
        ):
            with self.assertRaises(PackageError):
                publish_package(self.project)
        self.assertFalse(marker.exists())
        self.assertEqual(workflow.read_text(), "before")
        self.assertTrue((dist / ".gitkeep").is_file())
        self.assertFalse((dist / "pet.json").exists())
        self.assertFalse((dist / "spritesheet.webp").exists())

    def test_build_creates_candidate_qa_and_waits_for_human_verdict(self):
        import shutil
        shutil.rmtree(self.run_dir / "qa/package-reviewed")
        atlas = Image.new("RGBA", (1536, 2288))
        draw = ImageDraw.Draw(atlas)
        counts = (6, 8, 8, 4, 5, 8, 6, 6, 6, 8, 8)
        for row, count in enumerate(counts):
            for column in range(count):
                draw.rectangle((column * 192 + 40, row * 208 + 40, column * 192 + 140, row * 208 + 170), fill=(180, 60, 80, 255))
        draw.rectangle((6 * 192 + 40, 40, 6 * 192 + 140, 170), fill=(180, 60, 80, 255))
        atlas.save(self.run_dir / "final/spritesheet-extended.png")
        (self.run_dir / "pet_request.json").write_text(json.dumps({"chroma_key": {"hex": "#00FF00"}}))

        package_source = self.run_dir / "final/package-source.png"
        def assemble(_run_dir, _chroma):
            package_source.write_bytes((self.run_dir / "final/spritesheet-extended.png").read_bytes())
            return package_source

        with patch("omnipet.package._assemble_package_source", side_effect=assemble):
            result = build_package_evidence(self.project)

        self.assertEqual(result, "awaiting_external_verdict")
        self.assertTrue((self.run_dir / "qa/package-generated/blind-sheet.png").is_file())
        self.assertTrue((self.run_dir / "qa/package-generated/blind-answer-key.json").is_file())
        self.assertFalse((self.run_dir / "qa/package-reviewed").exists())
        self.assertEqual(json.loads((self.run_dir / "final/pet.json").read_text())["spriteVersionNumber"], 2)
        despill = json.loads((self.run_dir / "qa/package-generated/despill.json").read_text())
        first_hash = hashlib.sha256((self.run_dir / "final/spritesheet-extended.webp").read_bytes()).hexdigest()
        with patch("omnipet.package._assemble_package_source", side_effect=assemble):
            self.assertEqual(build_package_evidence(self.project), "awaiting_external_verdict")
        self.assertEqual(json.loads((self.run_dir / "qa/package-generated/despill.json").read_text()), despill)
        self.assertEqual(hashlib.sha256((self.run_dir / "final/spritesheet-extended.webp").read_bytes()).hexdigest(), first_hash)

    def test_changed_upstream_rebuild_replaces_source_and_invalidates_reviews(self):
        source = self.run_dir / "final/package-source.png"
        generated = self.run_dir / "qa/package-generated"
        reviewed = self.run_dir / "qa/package-reviewed"
        generated.mkdir(parents=True, exist_ok=True)
        reviewed.mkdir(parents=True, exist_ok=True)
        old = b"old source"
        source.write_bytes(old)
        (generated / "despill.json").write_text(json.dumps({
            "ok": True, "input_sha256": hashlib.sha256(old).hexdigest(),
            "output_sha256": hashlib.sha256(b"old atlas").hexdigest(), "passes": 1,
        }))
        (self.run_dir / "final/spritesheet-extended.webp").write_bytes(b"old atlas")
        (reviewed / "final-visual-review.json").write_text('{"ok": true}')
        atlas = Image.new("RGBA", (1536, 2288))
        atlas.putpixel((50, 50), (200, 10, 10, 255))
        atlas.save(self.run_dir / "changed.png")
        (self.run_dir / "pet_request.json").write_text('{"chroma_key":{"hex":"#00FF00"}}')

        def assemble(_run_dir, _chroma):
            source.write_bytes((self.run_dir / "changed.png").read_bytes())
            return source

        with patch("omnipet.package._assemble_package_source", side_effect=assemble), patch(
            "omnipet.package.validate_atlas",
            return_value=SimpleNamespace(ok=False),
        ):
            with self.assertRaises(PackageError):
                build_package_evidence(self.project)
        self.assertTrue(source.is_file())
        self.assertNotEqual(source.read_bytes(), old)
        self.assertFalse(reviewed.exists())

    def test_import_requires_exactly_three_independent_reviews_and_bound_visual_pass(self):
        package = self.run_dir / "qa/package-generated"
        package.mkdir(parents=True, exist_ok=True)
        sheet = package / "blind-sheet.png"
        sheet.write_bytes(b"blind")
        answer = package / "blind-answer-key.json"
        answer.write_text(json.dumps({
            "schema_version": 3,
            "atlas_sha256": hashlib.sha256((self.run_dir / "final/spritesheet-extended.webp").read_bytes()).hexdigest(),
            "instructions": "hidden",
            "pairs": [{
                "pair": "horizontal-1", "axis": "horizontal", "gate": "hard",
                "A": {"expected_direction": "screen-right", "source_direction": "090"},
                "B": {"expected_direction": "screen-left", "source_direction": "270"},
            }],
        }))
        evidence = [
            {"path": "qa/package-generated/blind-sheet.png", "sha256": hashlib.sha256(sheet.read_bytes()).hexdigest()},
            {"path": "final/spritesheet-extended.webp", "sha256": hashlib.sha256((self.run_dir / "final/spritesheet-extended.webp").read_bytes()).hexdigest()},
        ]
        direction_sheet = package / "direction-sheet.png"
        direction_sheet.write_bytes(b"directions")
        verdict = self.root / "package-verdict.json"
        verdict.write_text(json.dumps({
            "schema_version": 1,
            "stage": "package",
            "reviewers": [
                {"reviewer": reviewer, "pairs": [{"pair": "horizontal-1", "A": "screen-right", "B": "screen-left"}]}
                for reviewer in ("one", "two", "three")
            ],
            "directions": self._direction_reviews(),
            "direction_evidence": [
                {"path": "qa/package-generated/direction-sheet.png", "sha256": hashlib.sha256(direction_sheet.read_bytes()).hexdigest()},
                {"path": "final/spritesheet-extended.webp", "sha256": hashlib.sha256((self.run_dir / "final/spritesheet-extended.webp").read_bytes()).hexdigest()},
            ],
            "final_visual": {"verdict": "pass", "note": "all final visuals pass", "evidence": evidence},
        }))

        import_package_verdict(self.project, verdict)

        reviewed = self.run_dir / "qa/package-reviewed"
        self.assertTrue(json.loads((reviewed / "blind-validation.json").read_text())["ok"])
        self.assertTrue(json.loads((reviewed / "final-visual-review.json").read_text())["ok"])
        self.assertTrue(json.loads((reviewed / "final-direction-semantics.json").read_text())["ok"])

    def test_import_rejects_duplicate_reviewers_without_writing_validation(self):
        package = self.run_dir / "qa/package-generated"
        package.mkdir(parents=True, exist_ok=True)
        (package / "blind-sheet.png").write_bytes(b"blind")
        (package / "blind-answer-key.json").write_text(json.dumps({
            "schema_version": 3, "atlas_sha256": "a" * 64, "instructions": "hidden",
            "pairs": [],
        }))
        verdict = self.root / "duplicate-reviewers.json"
        verdict.write_text(json.dumps({
            "schema_version": 1, "stage": "package",
            "reviewers": [{"reviewer": "same", "pairs": []}] * 3,
            "directions": self._direction_reviews(), "direction_evidence": [],
            "final_visual": {"verdict": "pass", "note": "pass", "evidence": []},
        }))
        reviewed = self.run_dir / "qa/package-reviewed"
        if reviewed.exists():
            import shutil
            shutil.rmtree(reviewed)
        with self.assertRaises(PackageError):
            import_package_verdict(self.project, verdict)
        self.assertFalse(reviewed.exists())

    def test_import_rejects_wrong_cardinal_failed_or_missing_direction_and_stale_sheet(self):
        generated = self.run_dir / "qa/package-generated"
        generated.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.rmtree(self.run_dir / "qa/package-reviewed")
        (generated / "blind-sheet.png").write_bytes(b"blind")
        (generated / "blind-answer-key.json").write_text(json.dumps({
            "schema_version": 3, "atlas_sha256": hashlib.sha256(b"atlas").hexdigest(),
            "instructions": "hidden", "pairs": [],
        }))
        sheet = generated / "direction-sheet.png"
        sheet.write_bytes(b"sheet")
        base = {
            "schema_version": 1, "stage": "package",
            "reviewers": [{"reviewer": name, "pairs": []} for name in ("a", "b", "c")],
            "directions": self._direction_reviews(),
            "direction_evidence": self._evidence((
                "qa/package-generated/direction-sheet.png", "final/spritesheet-extended.webp",
            )),
            "final_visual": {"verdict": "pass", "note": "pass", "evidence": self._evidence((
                "qa/package-generated/blind-sheet.png", "final/spritesheet-extended.webp",
            ))},
        }
        mutations = []
        wrong = json.loads(json.dumps(base)); wrong["directions"][0]["observed"]["vertical"] = "down"; mutations.append(wrong)
        failed = json.loads(json.dumps(base)); failed["directions"][1]["verdict"] = "fail"; mutations.append(failed)
        missing = json.loads(json.dumps(base)); missing["directions"].pop(); mutations.append(missing)
        for index, payload in enumerate(mutations):
            verdict = self.root / f"bad-{index}.json"
            verdict.write_text(json.dumps(payload))
            with self.subTest(index=index), self.assertRaises(PackageError):
                import_package_verdict(self.project, verdict)
        stale = self.root / "stale.json"; stale.write_text(json.dumps(base)); sheet.write_bytes(b"changed")
        with self.assertRaises(PackageError):
            import_package_verdict(self.project, stale)
        self.assertFalse((self.run_dir / "qa/package-reviewed").exists())

    def test_import_rejects_warning_ambiguous_or_wrong_intermediate_axes(self):
        reviews = self._direction_reviews()
        mutations = []
        warning = json.loads(json.dumps(reviews)); warning[1]["verdict"] = "warning"; mutations.append(warning)
        ambiguous = json.loads(json.dumps(reviews)); ambiguous[2]["observed"]["horizontal"] = "ambiguous"; mutations.append(ambiguous)
        wrong = json.loads(json.dumps(reviews)); wrong[3]["observed"]["vertical"] = "down"; mutations.append(wrong)
        from omnipet.package import _validate_direction_semantics
        generated = self.run_dir / "qa/package-generated"
        generated.mkdir(parents=True, exist_ok=True)
        (generated / "direction-sheet.png").write_bytes(b"sheet")
        evidence = self._evidence((
            "qa/package-generated/direction-sheet.png", "final/spritesheet-extended.webp",
        ))
        for index, value in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(PackageError):
                _validate_direction_semantics(self.run_dir, value, evidence)

    def test_check_rejects_each_missing_gate_and_wrong_dimensions(self):
        with patch("omnipet.package._validated_package_approval", return_value=None):
            for relative in (
                "final/pet.json",
                "final/spritesheet-extended.webp",
                "qa/package-generated/validation.json",
                "qa/package-reviewed/blind-validation.json",
                "qa/package-reviewed/final-direction-semantics.json",
                "qa/package-reviewed/final-visual-review.json",
            ):
                path = self.run_dir / relative
                content = path.read_bytes()
                path.unlink()
                with self.subTest(relative=relative), self.assertRaises(PackageError):
                    check_package(self.project)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            validation = self.run_dir / "qa/package-generated/validation.json"
            payload = json.loads(validation.read_text())
            payload["height"] = 1872
            validation.write_text(json.dumps(payload))
            with self.assertRaises(PackageError):
                check_package(self.project)

    def test_check_rejects_failed_despill_and_unresolved_continuity(self):
        cases = (
            ("qa/package-generated/despill.json", {"ok": False, "passes": 1}),
            ("qa/package-generated/despill.json", {"ok": True, "passes": 2}),
            ("qa/package-generated/continuity.json", {"ok": False, "reviewRequired": False, "warnings": []}),
            ("qa/package-generated/continuity.json", {"ok": True, "reviewRequired": True, "warnings": ["snap"]}),
        )
        with patch("omnipet.package._validated_package_approval", return_value=None):
            for relative, payload in cases:
                path = self.run_dir / relative
                original = path.read_bytes()
                path.write_text(json.dumps(payload))
                with self.subTest(relative=relative, payload=payload), self.assertRaises(PackageError):
                    check_package(self.project)
                path.write_bytes(original)

    def test_tracked_complete_fixture_builds_valid_v2_atlas_without_network(self):
        fixture = Path(__file__).parent / "fixtures/complete-pet"
        metadata = json.loads((fixture / "atlas.json").read_text())
        checkpoint = json.loads((fixture / "checkpoint.json").read_text())
        self.assertFalse(checkpoint["network_required"])
        atlas = Image.new("RGBA", (
            metadata["columns"] * metadata["cell_width"],
            metadata["rows"] * metadata["cell_height"],
        ))
        draw = ImageDraw.Draw(atlas)
        for row, count in enumerate(metadata["frame_counts"]):
            for column in range(count):
                draw.rectangle((column * 192 + 50, row * 208 + 50, column * 192 + 130, row * 208 + 160), fill=(190, 40, 70, 255))
        row, column = metadata["extended_neutral"]
        draw.rectangle((column * 192 + 50, row * 208 + 50, column * 192 + 130, row * 208 + 160), fill=(190, 40, 70, 255))
        path = self.run_dir / "fixture.webp"
        atlas.save(path, format="WEBP", lossless=True)
        from omnipet.hatch.validation import ValidateAtlasConfig, validate_atlas
        result = validate_atlas(ValidateAtlasConfig(path, require_v2=True, chroma_key="#00FF00"))
        self.assertTrue(result.ok, result.report["errors"])

    def test_verdict_directory_swap_rolls_back_without_mixed_review_files(self):
        reviewed = self.run_dir / "qa/package-reviewed"
        import shutil
        shutil.rmtree(reviewed)
        reviewed.mkdir(parents=True)
        (reviewed / "old.json").write_text("old")
        with patch("omnipet.package._validate_package_verdict", return_value={
            "blind-consensus.json": {"pairs": []},
            "blind-validation.json": {"ok": True},
            "final-direction-semantics.json": {"ok": True},
            "final-visual-review.json": {"ok": True},
        }), patch("omnipet.package.os.replace", side_effect=OSError("swap failed")):
            with self.assertRaises(PackageError):
                import_package_verdict(self.project, self._minimal_verdict_file())
        self.assertEqual([path.name for path in reviewed.iterdir()], ["old.json"])

    def test_interrupted_review_swap_uses_deterministic_paths_and_recovers_before_import(self):
        reviewed = self.run_dir / "qa/package-reviewed"
        import shutil
        shutil.rmtree(reviewed)
        reviewed.mkdir()
        (reviewed / "old.json").write_text("old")
        reports = {
            "blind-consensus.json": {"pairs": []}, "blind-validation.json": {"ok": True},
            "final-direction-semantics.json": {"ok": True}, "final-visual-review.json": {"ok": True},
        }
        real_replace = os.replace
        crashed = False

        def crash(source, destination):
            nonlocal crashed
            result = real_replace(source, destination)
            if Path(source) == reviewed and not crashed:
                crashed = True
                raise KeyboardInterrupt()
            return result

        with patch("omnipet.package._validate_package_verdict", return_value=reports), patch(
            "omnipet.package.os.replace", side_effect=crash
        ):
            with self.assertRaises(KeyboardInterrupt):
                import_package_verdict(self.project, self._minimal_verdict_file())
        self.assertTrue((self.run_dir / "qa/package-review-publication.json").is_file())
        self.assertTrue((self.run_dir / "qa/.package-reviewed-backup").is_dir())
        self.assertTrue((self.run_dir / "qa/.package-reviewed-stage").is_dir())

        with patch("omnipet.package._validate_package_verdict", return_value=reports):
            import_package_verdict(self.project, self._minimal_verdict_file())
        self.assertFalse((self.run_dir / "qa/package-review-publication.json").exists())
        self.assertEqual(set(path.name for path in reviewed.iterdir()), set(reports))

    def test_first_review_install_crash_rolls_back_only_installed_review(self):
        reviewed = self.run_dir / "qa/package-reviewed"
        import shutil
        shutil.rmtree(reviewed)
        unrelated = self.run_dir / "qa/unrelated"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("keep")
        reports = {
            "blind-consensus.json": {"pairs": []}, "blind-validation.json": {"ok": True},
            "final-direction-semantics.json": {"ok": True}, "final-visual-review.json": {"ok": True},
        }
        real_write = __import__("omnipet.package", fromlist=["_write_json"])._write_json

        def crash_before_installed_journal(path, payload):
            if path.name == "package-review-publication.json" and payload.get("state") == "installed":
                raise KeyboardInterrupt()
            return real_write(path, payload)

        with patch("omnipet.package._validate_package_verdict", return_value=reports), patch(
            "omnipet.package._write_json", side_effect=crash_before_installed_journal
        ):
            with self.assertRaises(KeyboardInterrupt):
                import_package_verdict(self.project, self._minimal_verdict_file())
        journal = self.run_dir / "qa/package-review-publication.json"
        self.assertEqual(json.loads(journal.read_text())["state"], "backed-up")
        self.assertTrue(reviewed.is_dir())
        self.assertFalse((self.run_dir / "qa/.package-reviewed-stage").exists())
        self.assertFalse((self.run_dir / "qa/.package-reviewed-backup").exists())

        before = self._snapshot(self.root)
        with patch("omnipet.package._validated_package_approval", return_value=None):
            with self.assertRaises(PackageError):
                check_package(self.project)
        self.assertEqual(self._snapshot(self.root), before)

        recover_package(self.project)
        self.assertFalse(reviewed.exists())
        self.assertFalse(journal.exists())
        self.assertEqual((unrelated / "keep.txt").read_text(), "keep")

    def test_next_import_recovers_first_review_install_before_replacing_it(self):
        reviewed = self.run_dir / "qa/package-reviewed"
        import shutil
        shutil.rmtree(reviewed)
        reviewed.mkdir()
        (reviewed / "partial.json").write_text("partial")
        qa = self.run_dir / "qa"
        journal = qa / "package-review-publication.json"
        journal.write_text(json.dumps({
            "schema_version": 1, "state": "backed-up", "reviewed": str(reviewed),
            "stage": str(qa / ".package-reviewed-stage"),
            "backup": str(qa / ".package-reviewed-backup"),
        }))
        reports = {
            "blind-consensus.json": {"pairs": []}, "blind-validation.json": {"ok": True},
            "final-direction-semantics.json": {"ok": True}, "final-visual-review.json": {"ok": True},
        }
        with patch("omnipet.package._validate_package_verdict", return_value=reports):
            import_package_verdict(self.project, self._minimal_verdict_file())
        self.assertEqual(set(path.name for path in reviewed.iterdir()), set(reports))
        self.assertFalse(journal.exists())

    @staticmethod
    def _snapshot(root):
        return tuple(
            (str(path.relative_to(root)), path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted(root.rglob("*"))
            if path.is_file()
        )

    def _evidence(self, relatives):
        return [{"path": relative, "sha256": hashlib.sha256((self.run_dir / relative).read_bytes()).hexdigest()} for relative in relatives]

    def _minimal_verdict_file(self):
        path = self.root / "minimal.json"
        path.write_text("{}")
        return path

    @staticmethod
    def _direction_reviews():
        labels = ("000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5", "180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5")
        result = []
        for index, label in enumerate(labels):
            horizontal = "center" if index in (0, 8) else "right" if index < 8 else "left"
            vertical = "center" if index in (4, 12) else "up" if index < 8 or index > 12 else "down"
            result.append({
                "direction": label,
                "expected": {"horizontal": horizontal, "vertical": vertical},
                "observed": {"horizontal": horizontal, "vertical": vertical},
                "verdict": "pass", "note": "final despilled direction reads correctly",
            })
        return result


if __name__ == "__main__":
    unittest.main()
