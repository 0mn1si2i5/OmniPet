import json
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omnipet.approvals import (
    ACCEPTED_BASE_DECISION,
    ApprovalError,
    STAGE_EVIDENCE_PATHS,
    approve_stage,
    load_approvals,
)
from omnipet.checkpoint import _restore_checkpoint_phase1
from omnipet.package import PackageError, check_package
from omnipet.project import load_pet_project
from omnipet.review_resolution import create_warning_resolution
from omnipet.run import EXPECTED_JOB_IDS
from omnipet.workflow import (
    WorkflowError,
    approve_workflow_stage,
    clear_blocked,
    load_workflow,
    mark_blocked,
    refresh_workflow,
)


class ApprovalWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run_dir = self.root / ".omnipet" / "runs" / "pet"
        self.run_dir.mkdir(parents=True)
        self._write_manifest(set())

    def test_all_workflow_transitions_and_approval_order(self):
        self.assertEqual(refresh_workflow(self.run_dir).state, "preparing")
        with self.assertRaises(WorkflowError):
            approve_workflow_stage(self.run_dir, "base")

        self._complete("base")
        self.assertEqual(refresh_workflow(self.run_dir).state, "preparing")
        self._stage_evidence("base")
        self.assertEqual(refresh_workflow(self.run_dir).state, "awaiting_base_approval")
        approve_workflow_stage(self.run_dir, "base", note="Looks right")
        self.assertEqual(refresh_workflow(self.run_dir).state, "generating_standard_rows")

        for job_id in EXPECTED_JOB_IDS[1:10]:
            self._complete(job_id)
        self.assertEqual(refresh_workflow(self.run_dir).state, "generating_standard_rows")
        self._stage_evidence("standard-rows")
        self.assertEqual(refresh_workflow(self.run_dir).state, "awaiting_standard_rows_approval")
        approve_workflow_stage(self.run_dir, "standard-rows")
        self.assertEqual(refresh_workflow(self.run_dir).state, "generating_directions")

        for job_id in EXPECTED_JOB_IDS[10:]:
            self._complete(job_id)
        self.assertEqual(refresh_workflow(self.run_dir).state, "generating_directions")
        self._stage_evidence("directions")
        self.assertEqual(refresh_workflow(self.run_dir).state, "awaiting_directions_approval")
        approve_workflow_stage(self.run_dir, "directions")
        self.assertEqual(refresh_workflow(self.run_dir).state, "building_package")

        self._stage_evidence("package")
        self.assertEqual(refresh_workflow(self.run_dir).state, "awaiting_package_approval")
        approve_workflow_stage(self.run_dir, "package")
        self.assertEqual(refresh_workflow(self.run_dir).state, "awaiting_package_approval")

    def test_stale_warning_resolution_truncates_package_approval(self):
        self._complete("base")
        self._stage_evidence("base")
        refresh_workflow(self.run_dir)
        approve_workflow_stage(self.run_dir, "base")
        for job_id in EXPECTED_JOB_IDS[1:10]:
            self._complete(job_id)
        self._stage_evidence("standard-rows")
        refresh_workflow(self.run_dir)
        approve_workflow_stage(self.run_dir, "standard-rows")
        for job_id in EXPECTED_JOB_IDS[10:]:
            self._complete(job_id)
        self._stage_evidence("directions")
        refresh_workflow(self.run_dir)
        approve_workflow_stage(self.run_dir, "directions")
        self._stage_evidence("package")

        report = self.run_dir / "qa/package-generated/continuity.json"
        evidence = self.run_dir / "qa/package-generated/direction-sheet.png"
        atlas = self.run_dir / "final/spritesheet-extended.webp"
        warning_id = "direction-continuity:pair-000-to-022.5:center-shift-high"
        report.write_text(json.dumps({
            "ok": True,
            "atlasSha256": self._sha("final/spritesheet-extended.webp"),
            "reviewRequired": True,
            "warnings": [{
                "id": warning_id,
                "text": "000->022.5 center shift is high",
            }],
        }), encoding="utf-8")
        verdict = self.root / "continuity-resolution.json"

        def resolve():
            verdict.write_text(json.dumps({
                "schema_version": 1,
                "warning_ids": [warning_id],
                "reviewer": "release-reviewer",
                "disposition": "pass",
                "note": "The transition is visually intentional.",
                "visual_evidence": [{
                    "path": "qa/package-generated/direction-sheet.png",
                    "sha256": self._sha(
                        "qa/package-generated/direction-sheet.png"
                    ),
                }],
            }), encoding="utf-8")
            create_warning_resolution(
                self.run_dir,
                "qa/package-generated/continuity.json",
                verdict,
            )

        resolve()
        self.assertEqual(
            refresh_workflow(self.run_dir).state,
            "awaiting_package_approval",
        )
        approve_workflow_stage(self.run_dir, "package")
        self.assertEqual(
            [record.stage for record in load_approvals(self.run_dir)],
            ["base", "standard-rows", "directions", "package"],
        )

        originals = {
            report: report.read_bytes(),
            evidence: evidence.read_bytes(),
            atlas: atlas.read_bytes(),
        }
        mutations = {
            "report": lambda: report.write_text(json.dumps({
                **json.loads(originals[report]),
                "mutated": True,
            }), encoding="utf-8"),
            "evidence": lambda: evidence.write_bytes(b"changed evidence"),
            "atlas": lambda: atlas.write_bytes(b"changed atlas"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                mutate()
                self.assertEqual(
                    refresh_workflow(self.run_dir).state,
                    "awaiting_package_approval",
                )
                self.assertEqual(
                    [record.stage for record in load_approvals(self.run_dir)],
                    ["base", "standard-rows", "directions"],
                )
                with self.assertRaises(PackageError):
                    check_package(SimpleNamespace(
                        pet_id="pet",
                        repository_root=self.root,
                    ))
                for path, content in originals.items():
                    path.write_bytes(content)
                resolve()
                approve_workflow_stage(self.run_dir, "package")

    def test_approval_schema_hashes_timestamp_note_and_tamper_are_strict(self):
        self._complete("base")
        self._stage_evidence("base")
        refresh_workflow(self.run_dir)
        with patch("omnipet.approvals._utc_now", return_value=datetime.fromisoformat("2026-07-22T01:02:03+00:00")):
            approve_workflow_stage(self.run_dir, "base", note="Approved identity")

        path = self.run_dir / "qa" / "approvals.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"schema_version", "approvals"})
        record = payload["approvals"][0]
        self.assertEqual(set(record), {"stage", "artifacts", "approved_at", "note"})
        self.assertEqual(record["stage"], "base")
        self.assertEqual(record["approved_at"], "2026-07-22T01:02:03+00:00")
        self.assertEqual(record["note"], "Approved identity")
        self.assertEqual(
            [item["path"] for item in record["artifacts"]],
            sorted(item["path"] for item in record["artifacts"]),
        )
        self.assertTrue(all(set(item) == {"path", "sha256"} for item in record["artifacts"]))

        payload["extra"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ApprovalError):
            load_approvals(self.run_dir)

    def test_approval_records_must_be_an_exact_stage_prefix(self):
        self._approve_through_directions()
        path = self.run_dir / "qa" / "approvals.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        mutations = (
            [original["approvals"][0], original["approvals"][2]],
            [original["approvals"][1]],
            [original["approvals"][0], original["approvals"][0]],
            [original["approvals"][1], original["approvals"][0]],
        )
        for approvals in mutations:
            with self.subTest(stages=[item["stage"] for item in approvals]):
                path.write_text(
                    json.dumps({"schema_version": 1, "approvals": approvals}),
                    encoding="utf-8",
                )
                with self.assertRaises(ApprovalError):
                    load_approvals(self.run_dir)

    def test_changed_upstream_artifact_invalidates_it_and_downstream_atomically(self):
        self._approve_through_directions()
        approvals_path = self.run_dir / "qa" / "approvals.json"
        before = json.loads(approvals_path.read_text(encoding="utf-8"))
        self.assertEqual([item["stage"] for item in before["approvals"]], ["base", "standard-rows", "directions"])

        (self.run_dir / "decoded" / "idle.png").write_bytes(b"changed")
        self.assertEqual(refresh_workflow(self.run_dir).state, "generating_standard_rows")
        after = json.loads(approvals_path.read_text(encoding="utf-8"))
        self.assertEqual([item["stage"] for item in after["approvals"]], ["base"])
        self.assertFalse(any(path.name.startswith(".approvals.json-") for path in approvals_path.parent.iterdir()))

        (self.run_dir / "decoded" / "base.png").unlink()
        self.assertEqual(refresh_workflow(self.run_dir).state, "preparing")
        self.assertEqual(load_approvals(self.run_dir), ())

    def test_qa_mutation_invalidates_stage_and_downstream_approvals(self):
        self._approve_through_directions()

        (self.run_dir / "qa" / "rows" / "idle" / "review.json").write_bytes(b'{"ok": false}')

        self.assertEqual(refresh_workflow(self.run_dir).state, "generating_standard_rows")
        self.assertEqual([record.stage for record in load_approvals(self.run_dir)], ["base"])

    def test_base_qa_requires_exact_closed_accepted_record(self):
        self._complete("base")
        review = self._artifact("qa/base/review.json", self._base_review_bytes())
        self.assertEqual(refresh_workflow(self.run_dir).state, "awaiting_base_approval")

        accepted = json.loads(review.read_text(encoding="utf-8"))
        mutations = (
            {**accepted, "ok": False},
            {**accepted, "adoption_decision": "rejected", "ok": True},
            {**accepted, "adoption_decision": "approved canonical"},
            {**accepted, "provider": "openai"},
            {key: value for key, value in accepted.items() if key != "canvas"},
            {**accepted, "canvas": {"aspect_ratio": "21:9", "image_size": "2K"}},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                review.write_text(json.dumps(mutation), encoding="utf-8")
                self.assertEqual(refresh_workflow(self.run_dir).state, "preparing")

    def test_required_set_change_is_stale_and_symlink_is_rejected(self):
        self._complete("base")
        self._stage_evidence("base")
        refresh_workflow(self.run_dir)
        approve_workflow_stage(self.run_dir, "base")
        manifest = self._manifest()
        source = self.run_dir / "generated-sources" / "base-v2.png"
        source.write_bytes(b"base")
        manifest["jobs"][0]["source_path"] = str(source)
        self._save_manifest(manifest)
        self.assertEqual(refresh_workflow(self.run_dir).state, "awaiting_base_approval")

        source.unlink()
        source.symlink_to(self.root / "outside")
        (self.root / "outside").write_bytes(b"base")
        with self.assertRaises(ApprovalError):
            approve_stage(self.run_dir, "base")

    def test_blocking_is_explicit_persisted_sanitized_and_only_explicitly_cleared(self):
        blocked = mark_blocked(self.run_dir, code="manual-review", job="base", evidence="qa/base.txt")
        self.assertEqual(blocked.state, "blocked")
        self.assertEqual(load_workflow(self.run_dir).blocked, {
            "code": "manual-review", "job": "base", "evidence": "qa/base.txt",
            "diagnostic": None,
        })
        self._complete("base")
        self._stage_evidence("base")
        self.assertEqual(refresh_workflow(self.run_dir).state, "blocked")
        self.assertEqual(clear_blocked(self.run_dir).state, "awaiting_base_approval")
        with self.assertRaises(WorkflowError):
            mark_blocked(self.run_dir, code="Bearer private-token", job="base", evidence=None)

    def test_failed_job_blocks_without_automatic_retry_or_clear(self):
        manifest = self._manifest()
        manifest["jobs"][0]["status"] = "failed"
        self._save_manifest(manifest)

        state = refresh_workflow(self.run_dir)

        self.assertEqual(state.state, "blocked")
        self.assertEqual(state.blocked, {
            "code": "job-failed", "job": "base", "evidence": None,
            "diagnostic": None,
        })
        manifest["jobs"][0]["status"] = "pending"
        self._save_manifest(manifest)
        self.assertEqual(refresh_workflow(self.run_dir).state, "blocked")
        self.assertEqual(clear_blocked(self.run_dir).state, "preparing")

    def test_workflow_lock_is_private_and_serializes_refresh_with_block(self):
        entered = threading.Event()
        release = threading.Event()
        original = __import__("omnipet.workflow", fromlist=["_write_workflow_unlocked"])._write_workflow_unlocked

        def delayed_write(run_dir, state):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(2))
            return original(run_dir, state)

        with patch("omnipet.workflow._write_workflow_unlocked", side_effect=delayed_write):
            refresh = threading.Thread(target=refresh_workflow, args=(self.run_dir,))
            refresh.start()
            self.assertTrue(entered.wait(2))
            block = threading.Thread(
                target=mark_blocked,
                args=(self.run_dir,),
                kwargs={"code": "manual-review", "job": "base", "evidence": None},
            )
            block.start()
            time.sleep(0.05)
            self.assertTrue(block.is_alive())
            release.set()
            refresh.join(2)
            block.join(2)

        lock = self.run_dir / ".workflow.lock"
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        self.assertEqual(load_workflow(self.run_dir).state, "blocked")

    def test_approval_rehashes_evidence_before_commit_under_lock(self):
        self._complete("base")
        self._stage_evidence("base")
        refresh_workflow(self.run_dir)
        target = self.run_dir / "qa" / "base" / "review.json"
        original = __import__("omnipet.approvals", fromlist=["required_artifacts"]).required_artifacts
        calls = 0

        def mutate_after_snapshot(run_dir, stage):
            nonlocal calls
            result = original(run_dir, stage)
            calls += 1
            if calls == 1:
                target.write_bytes(b'{"ok": false}')
            return result

        with patch("omnipet.approvals.required_artifacts", side_effect=mutate_after_snapshot):
            with self.assertRaises(WorkflowError):
                approve_workflow_stage(self.run_dir, "base")

        self.assertEqual(load_approvals(self.run_dir), ())

    def test_approve_and_refresh_share_one_transaction_lock(self):
        self._complete("base")
        self._stage_evidence("base")
        refresh_workflow(self.run_dir)
        entered = threading.Event()
        release = threading.Event()
        original = __import__("omnipet.approvals", fromlist=["_write_approvals"])._write_approvals

        def delayed_approval(run_dir, records):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(run_dir, records)

        with patch("omnipet.approvals._write_approvals", side_effect=delayed_approval):
            approve = threading.Thread(target=approve_workflow_stage, args=(self.run_dir, "base"))
            approve.start()
            self.assertTrue(entered.wait(2))
            refresh = threading.Thread(target=refresh_workflow, args=(self.run_dir,))
            refresh.start()
            time.sleep(0.05)
            self.assertTrue(refresh.is_alive())
            release.set()
            approve.join(2)
            refresh.join(2)

        self.assertEqual(refresh_workflow(self.run_dir).state, "generating_standard_rows")

    def test_workflow_lock_rejects_symlink(self):
        outside = self.root / "outside-lock"
        outside.write_text("unchanged", encoding="utf-8")
        (self.run_dir / ".workflow.lock").symlink_to(outside)

        with self.assertRaises(WorkflowError):
            refresh_workflow(self.run_dir)

        self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged")

    def test_atomic_writes_fsync_containing_directories(self):
        self._complete("base")
        self._stage_evidence("base")
        with (
            patch("omnipet.workflow._fsync_directory") as workflow_fsync,
            patch("omnipet.approvals._fsync_directory") as approvals_fsync,
        ):
            refresh_workflow(self.run_dir)
            approve_workflow_stage(self.run_dir, "base")

        workflow_fsync.assert_any_call(self.run_dir)
        approvals_fsync.assert_any_call(self.run_dir / "qa")

    def _approve_through_directions(self):
        self._complete("base")
        self._stage_evidence("base")
        refresh_workflow(self.run_dir)
        approve_workflow_stage(self.run_dir, "base")
        for job_id in EXPECTED_JOB_IDS[1:10]:
            self._complete(job_id)
        self._stage_evidence("standard-rows")
        refresh_workflow(self.run_dir)
        approve_workflow_stage(self.run_dir, "standard-rows")
        for job_id in EXPECTED_JOB_IDS[10:]:
            self._complete(job_id)
        self._stage_evidence("directions")
        refresh_workflow(self.run_dir)
        approve_workflow_stage(self.run_dir, "directions")

    def _complete(self, job_id):
        manifest = self._manifest()
        job = next(item for item in manifest["jobs"] if item["id"] == job_id)
        job["status"] = "complete"
        self._artifact(f"decoded/{job_id}.png", job_id.encode())
        source = self._artifact(f"generated-sources/{job_id}.png", job_id.encode())
        job["source_path"] = str(source)
        job["completed_at"] = "2026-07-22T00:00:00+00:00"
        if job_id == "base":
            self._artifact("references/canonical-base.png", b"base")
        self._save_manifest(manifest)

    def _artifact(self, relative, content):
        path = self.run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _stage_evidence(self, stage):
        if stage == "package":
            self._artifact("final/package-source.png", b"source atlas")
            self._artifact("final/spritesheet-extended.webp", b"package atlas")
        for relative in STAGE_EVIDENCE_PATHS[stage]:
            if relative == "qa/base/review.json":
                content = self._base_review_bytes()
            elif relative.startswith("qa/rows/") and relative.endswith("/review.json"):
                job_id = Path(relative).parts[2]
                self._artifact(f"previews/{job_id}.gif", f"previews/{job_id}.gif".encode())
                content = json.dumps({
                    "ok": True,
                    "id": job_id,
                    "verdict": "pass",
                    "note": "fixture semantic review",
                    "evidence": self._evidence((
                        f"generated-sources/{job_id}.png",
                        f"decoded/{job_id}.png",
                        f"qa/rows/{job_id}/deterministic.json",
                        f"previews/{job_id}.gif",
                    )),
                }).encode()
            elif relative == "qa/directions/cardinals/review.json":
                self._artifact("qa/directions/cardinals/sheet.png", b"cardinal sheet")
                content = json.dumps({
                    "ok": True,
                    "directions": [
                        {"direction": label, "expected": expected, "verdict": "pass", "note": "fixture semantic review"}
                        for label, expected in (("000", "up"), ("090", "right"), ("180", "down"), ("270", "left"))
                    ],
                    "evidence": self._evidence((
                        "generated-sources/look-cardinals.png",
                        "decoded/look-cardinals.png",
                        "qa/directions/cardinals/deterministic.json",
                        "qa/directions/cardinals/sheet.png",
                        "decoded/look-cardinals-approved.png",
                    )),
                }).encode()
            elif relative == "qa/directions/direction-semantics.json":
                self._artifact("qa/directions/look-row-9-contact-sheet.png", b"row9 sheet")
                self._artifact("qa/directions/contact-sheet.png", b"direction sheet")
                self._artifact("qa/directions/continuity.json", b'{"ok": true}')
                content = json.dumps({
                    "schema_version": 1,
                    "ok": True,
                    "reviews": {
                        "row9": {
                            "directions": [
                                {"direction": label, "verdict": "pass", "note": "fixture semantic review"}
                                for label in ("000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5")
                            ],
                            "evidence": self._evidence((
                                "generated-sources/look-row-9.png", "decoded/look-row-9.png",
                                "qa/directions/look-row-9-registered.png",
                                "qa/directions/look-row-9-registration.json",
                                "qa/directions/look-row-9-continuity.json",
                                "qa/directions/look-row-9-contact-sheet.png",
                            )),
                        },
                        "row10": {
                            "directions": [
                                {"direction": label, "verdict": "pass", "note": "fixture semantic review"}
                                for label in ("180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5")
                            ],
                            "evidence": self._evidence((
                                "generated-sources/look-row-10.png", "decoded/look-row-10.png",
                                "qa/directions/look-row-10-registration.json",
                                "qa/directions/continuity.json", "qa/directions/contact-sheet.png",
                            )),
                        },
                    },
                }).encode()
            elif relative == "qa/directions/look-row-9-registration.json":
                content = json.dumps({
                    "ok": True,
                    "scale": 1.0,
                    "source_sha256": self._sha("decoded/look-row-9.png"),
                    "registered_sha256": self._sha("qa/directions/look-row-9-registered.png"),
                }).encode()
            elif relative == "qa/directions/look-row-10-registration.json":
                content = json.dumps({
                    "ok": True,
                    "scale": 1.0,
                    "source_sha256": self._sha("decoded/look-row-10.png"),
                    "registered_row_9_sha256": self._sha("qa/directions/look-row-9-registered.png"),
                    "atlas_sha256": self._sha("final/spritesheet-extended.png"),
                }).encode()
            elif relative == "final/pet.json":
                content = json.dumps({
                    "id": "pet", "displayName": "Pet", "description": "Pet.",
                    "spriteVersionNumber": 2, "spritesheetPath": "spritesheet.webp",
                }).encode()
            elif relative == "qa/package-generated/despill.json":
                content = json.dumps({
                    "ok": True, "passes": 1,
                    "input_sha256": self._sha("final/package-source.png"),
                    "output_sha256": self._sha("final/spritesheet-extended.webp"),
                }).encode()
            elif relative == "qa/package-generated/validation.json":
                content = json.dumps({
                    "ok": True, "sprite_version_number": 2, "width": 1536,
                    "height": 2288, "errors": [],
                }).encode()
            elif relative == "qa/package-generated/blind-answer-key.json":
                content = json.dumps({
                    "schema_version": 3,
                    "atlas_sha256": self._sha("final/spritesheet-extended.webp"),
                    "pairs": [{"pair": "horizontal-1"}],
                }).encode()
            elif relative == "qa/package-reviewed/blind-consensus.json":
                content = b'{"pairs": [{"pair": "horizontal-1"}]}'
            elif relative == "qa/package-reviewed/blind-validation.json":
                content = b'{"ok": true, "errors": [], "unconfirmed": []}'
            elif relative == "qa/package-reviewed/final-direction-semantics.json":
                content = json.dumps({
                    "schema_version": 1, "ok": True,
                    "directions": [{"verdict": "pass"}] * 16,
                    "evidence": self._evidence((
                        "qa/package-generated/direction-sheet.png", "final/spritesheet-extended.webp",
                    )),
                }).encode()
            elif relative == "qa/package-reviewed/final-visual-review.json":
                content = json.dumps({
                    "ok": True, "verdict": "pass", "note": "fixture final visual pass",
                    "evidence": self._evidence((
                        "qa/package-generated/blind-sheet.png", "final/spritesheet-extended.webp",
                    )),
                }).encode()
            else:
                content = b'{"ok": true}' if relative.endswith(".json") else relative.encode()
            if not (self.run_dir / relative).exists():
                self._artifact(relative, content)

    def _sha(self, relative):
        import hashlib
        return hashlib.sha256((self.run_dir / relative).read_bytes()).hexdigest()

    def _evidence(self, relatives):
        return [{"path": relative, "sha256": self._sha(relative)} for relative in relatives]

    def _base_review_bytes(self):
        return json.dumps({
            "adoption_decision": ACCEPTED_BASE_DECISION,
            "canvas": {"aspect_ratio": "1:1", "image_size": "1K"},
            "completed_at": "2026-07-22T00:00:00+00:00",
            "job_id": "base",
            "ok": True,
            "sha256": "a" * 64,
        }).encode()

    def _write_manifest(self, complete):
        jobs = [{"id": job_id, "status": "complete" if job_id in complete else "pending"} for job_id in EXPECTED_JOB_IDS]
        self._save_manifest({"schema_version": 1, "jobs": jobs})

    def _manifest(self):
        return json.loads((self.run_dir / "imagegen-jobs.json").read_text(encoding="utf-8"))

    def _save_manifest(self, payload):
        (self.run_dir / "imagegen-jobs.json").write_text(json.dumps(payload), encoding="utf-8")


class CheckpointWorkflowMigrationTests(unittest.TestCase):
    def test_external_clean_clone_migrates_only_base_approval_and_is_ready_for_running_right(self):
        clone = self._external_clone()
        project = load_pet_project(clone, ".")

        state = _restore_checkpoint_phase1(project)
        workflow = refresh_workflow(state.run_dir)

        self.assertEqual(workflow.state, "generating_standard_rows")
        self.assertEqual([record.stage for record in load_approvals(state.run_dir)], ["base"])
        self.assertEqual(state.counts["ready"], 1)
        manifest = json.loads((state.run_dir / "imagegen-jobs.json").read_text(encoding="utf-8"))
        running_right = next(job for job in manifest["jobs"] if job["id"] == "running-right")
        self.assertEqual(running_right["status"], "pending")

    def test_checkpoint_without_accepted_base_qa_does_not_infer_approval(self):
        clone = self._external_clone()
        checkpoint_path = clone / "checkpoint" / "checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["accepted_qa"] = [item for item in checkpoint["accepted_qa"] if item["job_id"] != "base"]
        base_qa = next((clone / "checkpoint" / "qa").glob("canonical-*.json"))
        base_qa.unlink()
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

        state = _restore_checkpoint_phase1(load_pet_project(clone, "."))

        self.assertEqual(refresh_workflow(state.run_dir).state, "preparing")
        self.assertEqual(load_approvals(state.run_dir), ())

    def test_invalid_checkpoint_completed_at_uses_utc_migration_time(self):
        clone = self._external_clone()
        checkpoint_path = clone / "checkpoint" / "checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        next(item for item in checkpoint["provenance"] if item["job_id"] == "base")["completed_at"] = "legacy"
        checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
        now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

        with patch("omnipet.approvals._utc_now", return_value=now):
            state = _restore_checkpoint_phase1(load_pet_project(clone, "."))

        approval, = load_approvals(state.run_dir)
        self.assertEqual(approval.approved_at, "2026-07-22T12:00:00+00:00")

    def _external_clone(self):
        source = Path(__file__).resolve().parents[4] / ("OmniPet-" + "Su" + "Shi")
        required = (source / "pet.yaml", source / "checkpoint" / "checkpoint.json")
        if not source.is_dir() or not all(path.is_file() for path in required):
            self.skipTest("external production pet fixture is unavailable")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        clone = Path(temporary.name).resolve() / "external-pet"
        shutil.copytree(source, clone, ignore=shutil.ignore_patterns(".git", ".omnipet", "__pycache__"))
        return clone


if __name__ == "__main__":
    unittest.main()
