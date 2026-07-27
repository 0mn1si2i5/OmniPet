import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omnipet.checkpoint import (
    CheckpointError,
    restore_checkpoint,
    restore_checkpoint_for_current_engine,
    restore_checkpoint_v2,
)
from omnipet.project import PetReference
from omnipet.release import initialize_design_run
from omnipet.workflow import (
    WorkflowError,
    approve_workflow_stage,
    clear_blocked,
    load_workflow,
    load_workflow_v2,
    mark_blocked,
    mark_package_complete,
    refresh_workflow,
    transition_workflow_v2,
)
from omnipet.run import EXPECTED_JOB_IDS


class WorkflowV2Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run_dir = self.root / "run"

    def test_new_phase2_run_starts_at_intake_without_generation_contracts(self):
        state = initialize_design_run(self.run_dir, pet_id="napoleon")

        self.assertEqual(state.state, "intake")
        self.assertIsNone(state.blocked)
        self.assertEqual(
            json.loads((self.run_dir / "workflow.json").read_text(encoding="utf-8")),
            {"schema_version": 2, "state": "intake", "blocked": None},
        )
        self.assertEqual(
            json.loads((self.run_dir / "omnipet-run.json").read_text(encoding="utf-8")),
            {
                "schema_version": 2,
                "pet_id": "napoleon",
                "design_revision": "design-0001",
                "references": [],
            },
        )
        for relative in ("design", "decoded/prototypes", "qa/design-pack", "references"):
            self.assertTrue((self.run_dir / relative).is_dir(), relative)
        for relative in ("imagegen-jobs.json", "prompts", "provider"):
            self.assertFalse((self.run_dir / relative).exists(), relative)

    def test_new_phase2_run_does_not_prepare_legacy_run_or_construct_provider(self):
        with (
            patch(
                "omnipet.release.prepare_run",
                side_effect=AssertionError("legacy preparation called"),
            ) as prepare,
            patch(
                "omnipet.release.OpenAIImageGenerator",
                side_effect=AssertionError("provider constructed"),
            ) as provider,
        ):
            state = initialize_design_run(self.run_dir, pet_id="napoleon")

        prepare.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(state.state, "intake")
        self.assertFalse((self.run_dir / "imagegen-jobs.json").exists())

    def test_reference_snapshots_are_copied_and_hash_bound(self):
        first = self.root / "portrait.PNG"
        second = self.root / "costume.jpg"
        third = self.root / "silhouette"
        first.write_bytes(b"portrait")
        second.write_bytes(b"costume")
        third.write_bytes(b"silhouette")

        initialize_design_run(
            self.run_dir,
            pet_id="napoleon",
            references=(
                PetReference(first, "identity"),
                PetReference(second, "costume construction"),
                PetReference(third, "shape"),
            ),
        )

        metadata = json.loads(
            (self.run_dir / "omnipet-run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["references"], [
            {
                "run_path": "references/reference-01.png",
                "role": "identity",
                "sha256": hashlib.sha256(b"portrait").hexdigest(),
            },
            {
                "run_path": "references/reference-02.jpg",
                "role": "costume construction",
                "sha256": hashlib.sha256(b"costume").hexdigest(),
            },
            {
                "run_path": "references/reference-03.png",
                "role": "shape",
                "sha256": hashlib.sha256(b"silhouette").hexdigest(),
            },
        ])
        self.assertEqual(
            (self.run_dir / "references/reference-01.png").read_bytes(), b"portrait"
        )
        self.assertEqual(
            (self.run_dir / "references/reference-02.jpg").read_bytes(), b"costume"
        )

    def test_unsafe_reference_rolls_back_the_whole_run(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside")
        reference = self.root / "reference.png"
        reference.symlink_to(outside)

        with self.assertRaises(ValueError):
            initialize_design_run(
                self.run_dir,
                pet_id="napoleon",
                references=(PetReference(reference, "identity"),),
            )

        self.assertFalse(self.run_dir.exists())

    def test_reference_swapped_to_symlink_at_copy_boundary_is_not_published(self):
        from omnipet import release

        reference = self.root / "reference.png"
        reference.write_bytes(b"validated")
        outside = self.root / "outside.png"
        outside.write_bytes(b"external")
        snapshot_reference = getattr(release, "_snapshot_reference", None)

        def swap_then_snapshot(source, destination, expected_identity):
            reference.unlink()
            reference.symlink_to(outside)
            return snapshot_reference(source, destination, expected_identity)

        with patch(
            "omnipet.release._snapshot_reference",
            create=True,
            side_effect=swap_then_snapshot,
        ):
            with self.assertRaises((OSError, ValueError)):
                initialize_design_run(
                    self.run_dir,
                    pet_id="napoleon",
                    references=(PetReference(reference, "identity"),),
                )

        self.assertFalse(self.run_dir.exists())
        self.assertEqual(outside.read_bytes(), b"external")

    def test_symlink_destination_is_rejected_without_mutating_target(self):
        target = self.root / "target"
        target.mkdir()
        marker = target / "marker"
        marker.write_bytes(b"unchanged")
        self.run_dir.symlink_to(target, target_is_directory=True)

        with self.assertRaises(ValueError):
            initialize_design_run(self.run_dir, pet_id="napoleon")

        self.assertEqual(marker.read_bytes(), b"unchanged")
        self.assertTrue(self.run_dir.is_symlink())

    def test_existing_destination_is_rejected_without_mutation(self):
        self.run_dir.mkdir()
        marker = self.run_dir / "marker"
        marker.write_bytes(b"unchanged")

        with self.assertRaises(FileExistsError):
            initialize_design_run(self.run_dir, pet_id="napoleon")

        self.assertEqual({path.name for path in self.run_dir.iterdir()}, {"marker"})
        self.assertEqual(marker.read_bytes(), b"unchanged")

    def test_initialization_publishes_only_a_complete_run(self):
        from omnipet import release

        write_json = release._write_json

        def observe_staging(path, payload):
            self.assertFalse(self.run_dir.exists())
            return write_json(path, payload)

        with patch("omnipet.release._write_json", side_effect=observe_staging):
            initialize_design_run(self.run_dir, pet_id="napoleon")

        self.assertEqual(load_workflow_v2(self.run_dir).state, "intake")

    def test_parent_fsync_failure_after_publish_rolls_back_run(self):
        from omnipet import release

        fsync = release._fsync_directory

        def fail_parent_only(path):
            if Path(path) == self.run_dir.parent:
                raise OSError("parent fsync failed")
            fsync(path)

        with patch(
            "omnipet.release._fsync_directory",
            side_effect=fail_parent_only,
        ):
            with self.assertRaises(OSError):
                initialize_design_run(self.run_dir, pet_id="napoleon")

        self.assertFalse(self.run_dir.exists())
        self.assertFalse(any(self.root.glob(".run-*")))

    def test_failed_compensating_rename_removes_published_run(self):
        from omnipet import release

        replace = release.os.replace
        fsync = release._fsync_directory

        def fail_compensating_replace(source, destination):
            if Path(source) == self.run_dir:
                raise OSError("rollback rename failed")
            return replace(source, destination)

        def fail_parent_only(path):
            if Path(path) == self.run_dir.parent:
                raise OSError("parent fsync failed")
            fsync(path)

        with (
            patch("omnipet.release.os.replace", side_effect=fail_compensating_replace),
            patch("omnipet.release._fsync_directory", side_effect=fail_parent_only),
        ):
            with self.assertRaisesRegex(ValueError, "^design run initialization failed$"):
                initialize_design_run(self.run_dir, pet_id="napoleon")

        self.assertFalse(self.run_dir.exists())

    def test_failed_compensating_rename_and_removal_requires_recovery(self):
        from omnipet import release

        replace = release.os.replace
        fsync = release._fsync_directory
        rmtree = release.shutil.rmtree

        def fail_compensating_replace(source, destination):
            if Path(source) == self.run_dir:
                raise OSError("rollback rename failed")
            return replace(source, destination)

        def fail_parent_only(path):
            if Path(path) == self.run_dir.parent:
                raise OSError("parent fsync failed")
            fsync(path)

        def fail_published_removal(path, *args, **kwargs):
            if Path(path) == self.run_dir:
                raise OSError("published removal failed")
            return rmtree(path, *args, **kwargs)

        with (
            patch("omnipet.release.os.replace", side_effect=fail_compensating_replace),
            patch("omnipet.release._fsync_directory", side_effect=fail_parent_only),
            patch("omnipet.release.shutil.rmtree", side_effect=fail_published_removal),
        ):
            with self.assertRaisesRegex(ValueError, "^design run recovery required$"):
                initialize_design_run(self.run_dir, pet_id="napoleon")

        self.assertTrue(self.run_dir.is_dir())
        self.assertEqual(load_workflow_v2(self.run_dir).state, "intake")

    def test_staging_directories_are_fsynced_bottom_up_before_publish(self):
        from omnipet import release

        events = []
        publish = getattr(release, "_publish_design_run", None)

        def record_fsync(path):
            events.append(("fsync", Path(path)))

        def record_publish(staging, destination):
            events.append(("publish", Path(staging)))
            return publish(staging, destination)

        with (
            patch("omnipet.release._fsync_directory", side_effect=record_fsync),
            patch(
                "omnipet.release._publish_design_run",
                create=True,
                side_effect=record_publish,
            ) as publish_mock,
        ):
            initialize_design_run(self.run_dir, pet_id="napoleon")

        publish_mock.assert_called_once()
        staging = publish_mock.call_args.args[0]
        publish_index = events.index(("publish", staging))
        required = (
            staging / "design",
            staging / "decoded/prototypes",
            staging / "decoded",
            staging / "qa/design-pack",
            staging / "qa",
            staging / "references",
            staging,
        )
        for directory in required:
            self.assertIn(("fsync", directory), events[:publish_index])
        self.assertLess(
            events.index(("fsync", staging / "decoded/prototypes")),
            events.index(("fsync", staging / "decoded")),
        )
        self.assertLess(
            events.index(("fsync", staging / "qa/design-pack")),
            events.index(("fsync", staging / "qa")),
        )
        self.assertEqual(events[publish_index - 1], ("fsync", staging))

    def test_v2_initialize_and_load_do_not_invoke_approval_migration(self):
        with patch(
            "omnipet.approvals.migrate_checkpoint_base_approval",
            side_effect=AssertionError("implicit migration called"),
        ) as migrate:
            initialize_design_run(self.run_dir, pet_id="napoleon")
            self.assertEqual(load_workflow_v2(self.run_dir).state, "intake")

        migrate.assert_not_called()

    def test_v2_loader_rejects_schema1_and_default_loader_dispatches_schema2(self):
        self.run_dir.mkdir()
        path = self.run_dir / "workflow.json"
        path.write_text(
            json.dumps({"schema_version": 1, "state": "preparing", "blocked": None}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "explicit migration required"):
            load_workflow_v2(self.run_dir)

        path.write_text(
            json.dumps({"schema_version": 2, "state": "intake", "blocked": None}),
            encoding="utf-8",
        )
        self.assertEqual(load_workflow(self.run_dir).state, "intake")

    def test_workflow_swap_to_symlink_at_read_boundary_is_rejected(self):
        from omnipet import workflow

        self.run_dir.mkdir()
        path = self.run_dir / "workflow.json"
        path.write_text(json.dumps({
            "schema_version": 2, "state": "intake", "blocked": None,
        }), encoding="utf-8")
        outside = self.root / "outside-workflow.json"
        outside.write_text(json.dumps({
            "schema_version": 2, "state": "designing", "blocked": None,
        }), encoding="utf-8")
        safe_read = getattr(workflow, "_read_json_no_follow", None)

        def swap_then_read(source):
            path.unlink()
            path.symlink_to(outside)
            return safe_read(source)

        with patch(
            "omnipet.workflow._read_json_no_follow",
            create=True,
            side_effect=swap_then_read,
        ):
            with self.assertRaises(WorkflowError):
                load_workflow_v2(self.run_dir)

    def test_v2_workflow_schema_version_requires_exact_integer(self):
        self.run_dir.mkdir()
        path = self.run_dir / "workflow.json"
        for version in (2.0, True):
            with self.subTest(version=version):
                path.write_text(json.dumps({
                    "schema_version": version,
                    "state": "intake",
                    "blocked": None,
                }), encoding="utf-8")
                with self.assertRaises(WorkflowError):
                    load_workflow_v2(self.run_dir)

    def test_v2_loader_rejects_nonclosed_documents(self):
        self.run_dir.mkdir()
        valid = {"schema_version": 2, "state": "intake", "blocked": None}
        invalid = (
            {**valid, "extra": True},
            {"schema_version": 2, "state": "intake"},
            {**valid, "state": "approved"},
            {**valid, "blocked": {"code": "unexpected"}},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                (self.run_dir / "workflow.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                with self.assertRaises(WorkflowError):
                    load_workflow_v2(self.run_dir)

    def test_v2_loader_accepts_minimal_closed_blocked_record(self):
        self.run_dir.mkdir()
        blocked = {
            "code": "provider-timeout",
            "prior_state": "prototyping",
            "job_id": "prototype-front",
            "evidence_path": "qa/design-pack/review.json",
            "root_failure_key": "provider-timeout:prototype-front",
            "recoveries": [],
            "diagnostic": {
                "category": "provider-timeout",
                "status": 504,
                "request_id": "req-123",
                "retryable": True,
            },
        }
        (self.run_dir / "workflow.json").write_text(json.dumps({
            "schema_version": 2,
            "state": "blocked",
            "blocked": blocked,
        }), encoding="utf-8")

        state = load_workflow_v2(self.run_dir)

        self.assertEqual(state.state, "blocked")
        self.assertEqual(state.blocked, blocked)
        self.assertEqual(load_workflow(self.run_dir), state)

    def test_v2_loader_rejects_invalid_blocked_records(self):
        self.run_dir.mkdir()
        valid = {
            "code": "provider-timeout",
            "prior_state": "prototyping",
            "job_id": "prototype-front",
            "evidence_path": "qa/design-pack/review.json",
            "root_failure_key": "provider-timeout:prototype-front",
            "recoveries": [],
            "diagnostic": None,
        }
        invalid = (
            {**valid, "extra": True},
            {key: value for key, value in valid.items() if key != "code"},
            {**valid, "code": "Bearer private-token"},
            {**valid, "prior_state": "blocked"},
            {**valid, "prior_state": "complete"},
            {**valid, "prior_state": "rejected"},
            {**valid, "prior_state": "unknown"},
            {**valid, "job_id": ""},
            {**valid, "job_id": "prototype\nfront"},
            {**valid, "evidence_path": "../outside.json"},
            {**valid, "evidence_path": "/outside.json"},
            {**valid, "evidence_path": "."},
            {**valid, "evidence_path": "qa/design-pack\n/review.json"},
            {**valid, "root_failure_key": ""},
            {**valid, "recoveries": ["retry"]},
            {**valid, "diagnostic": {"category": "provider-timeout"}},
        )
        path = self.run_dir / "workflow.json"
        for blocked in invalid:
            with self.subTest(blocked=blocked):
                path.write_text(json.dumps({
                    "schema_version": 2,
                    "state": "blocked",
                    "blocked": blocked,
                }), encoding="utf-8")
                with self.assertRaises(WorkflowError):
                    load_workflow_v2(self.run_dir)

    def test_v2_blocked_object_and_state_must_agree(self):
        self.run_dir.mkdir()
        blocked = {
            "code": "provider-timeout",
            "prior_state": "prototyping",
            "job_id": None,
            "evidence_path": None,
            "root_failure_key": None,
            "recoveries": [],
            "diagnostic": None,
        }
        path = self.run_dir / "workflow.json"
        for payload in (
            {"schema_version": 2, "state": "blocked", "blocked": None},
            {"schema_version": 2, "state": "prototyping", "blocked": blocked},
        ):
            with self.subTest(payload=payload):
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(WorkflowError):
                    load_workflow_v2(self.run_dir)

    def test_initial_phase2_transitions_are_closed(self):
        initialize_design_run(self.run_dir, pet_id="napoleon")

        transitions = (
            ("intake-validated", "designing"),
            ("contracts-validated", "prototyping"),
            ("prototypes-passed", "awaiting_design_pack_approval"),
            ("design-pack-approved", "producing_standard_rows"),
        )
        for event, expected in transitions:
            with self.subTest(event=event):
                self.assertEqual(
                    transition_workflow_v2(self.run_dir, event).state, expected
                )
                self.assertEqual(load_workflow_v2(self.run_dir).state, expected)

    def test_illegal_transition_is_atomic(self):
        initialize_design_run(self.run_dir, pet_id="napoleon")
        path = self.run_dir / "workflow.json"
        before = path.read_bytes()

        with self.assertRaises(WorkflowError):
            transition_workflow_v2(self.run_dir, "design-pack-approved")

        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(any(path.name.startswith(".workflow.json-") for path in self.run_dir.iterdir()))

    def test_shared_lock_rejects_entry_replacement_after_flock(self):
        from omnipet import workflow

        initialize_design_run(self.run_dir, pet_id="napoleon")
        before = (self.run_dir / "workflow.json").read_bytes()
        flock = workflow.fcntl.flock
        replaced = False

        def replace_after_lock(descriptor, operation):
            nonlocal replaced
            result = flock(descriptor, operation)
            if operation == workflow.fcntl.LOCK_EX and not replaced:
                lock_path = self.run_dir / ".workflow.lock"
                lock_path.unlink()
                lock_path.write_bytes(b"replacement")
                replaced = True
            return result

        with patch("omnipet.workflow.fcntl.flock", side_effect=replace_after_lock):
            with self.assertRaisesRegex(
                WorkflowError, "^workflow lock is unavailable$"
            ) as caught:
                transition_workflow_v2(self.run_dir, "intake-validated")

        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual((self.run_dir / "workflow.json").read_bytes(), before)

    def test_legacy_block_mutations_reject_v2_without_schema_downgrade(self):
        initialize_design_run(self.run_dir, pet_id="napoleon")
        path = self.run_dir / "workflow.json"
        before = path.read_bytes()

        with self.assertRaises(WorkflowError):
            mark_blocked(
                self.run_dir, code="manual-review", job=None, evidence=None
            )
        self.assertEqual(path.read_bytes(), before)

        blocked = {
            "code": "provider-timeout",
            "prior_state": "prototyping",
            "job_id": None,
            "evidence_path": None,
            "root_failure_key": None,
            "recoveries": [],
            "diagnostic": None,
        }
        path.write_text(json.dumps({
            "schema_version": 2, "state": "blocked", "blocked": blocked,
        }), encoding="utf-8")
        before = path.read_bytes()

        with self.assertRaises(WorkflowError):
            clear_blocked(self.run_dir)
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_block_guard_uses_schema_not_overlapping_state_name(self):
        self.run_dir.mkdir()
        (self.run_dir / "workflow.json").write_text(json.dumps({
            "schema_version": 1,
            "state": "building_package",
            "blocked": None,
        }), encoding="utf-8")

        state = mark_blocked(
            self.run_dir, code="manual-review", job=None, evidence=None
        )

        self.assertEqual(state.state, "blocked")
        self.assertEqual(
            json.loads((self.run_dir / "workflow.json").read_text(encoding="utf-8"))["schema_version"],
            1,
        )

    def test_refresh_v2_returns_explicit_state_without_legacy_downgrade(self):
        initialize_design_run(self.run_dir, pet_id="napoleon")
        (self.run_dir / "imagegen-jobs.json").write_text(json.dumps({
            "schema_version": 1,
            "jobs": [
                {"id": job_id, "status": "pending"}
                for job_id in EXPECTED_JOB_IDS
            ],
        }), encoding="utf-8")
        path = self.run_dir / "workflow.json"
        before = path.read_bytes()

        state = refresh_workflow(self.run_dir)

        self.assertEqual(state.state, "intake")
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_approval_and_package_mutations_reject_v2_without_mutation(self):
        initialize_design_run(self.run_dir, pet_id="napoleon")
        (self.run_dir / "imagegen-jobs.json").write_text(json.dumps({
            "schema_version": 1,
            "jobs": [
                {"id": job_id, "status": "pending"}
                for job_id in EXPECTED_JOB_IDS
            ],
        }), encoding="utf-8")
        path = self.run_dir / "workflow.json"
        before = path.read_bytes()

        for operation in (
            lambda: approve_workflow_stage(self.run_dir, "base"),
            lambda: mark_package_complete(self.run_dir),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(WorkflowError):
                    operation()
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.run_dir / "package-complete.json").exists())


class WorkflowV2CheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkpoint = self.root / "checkpoint"
        self.checkpoint.mkdir()
        self.run_dir = self.root / ".omnipet/runs/napoleon"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "existing.bin").write_bytes(b"unchanged")
        self.project = SimpleNamespace(
            root=self.root,
            repository_root=self.root,
            pet_id="napoleon",
        )

    def test_v2_restore_rejects_phase1_before_prepare_migration_or_mutation(self):
        (self.checkpoint / "checkpoint.json").write_text(json.dumps({
            "schema_version": 1,
            "pet_id": "napoleon",
        }), encoding="utf-8")
        before = self._run_snapshot()

        with (
            patch("omnipet.checkpoint.prepare_run") as prepare,
            patch("omnipet.approvals.migrate_checkpoint_base_approval") as migrate,
        ):
            with self.assertRaisesRegex(CheckpointError, "^explicit migration required$"):
                restore_checkpoint_v2(self.project, force=True)

        prepare.assert_not_called()
        migrate.assert_not_called()
        self.assertEqual(self._run_snapshot(), before)

    def test_v2_restore_future_checkpoint_is_closed_and_nonmutating(self):
        (self.checkpoint / "checkpoint.json").write_text(json.dumps({
            "schema_version": 2,
            "source_workflow_schema_version": 2,
        }), encoding="utf-8")
        before = self._run_snapshot()

        with patch("omnipet.checkpoint.prepare_run") as prepare:
            with self.assertRaisesRegex(CheckpointError, "^checkpoint restore unsupported$"):
                restore_checkpoint_v2(self.project)

        prepare.assert_not_called()
        self.assertEqual(self._run_snapshot(), before)

    def test_v2_restore_invalid_detection_document_has_sanitized_error(self):
        (self.checkpoint / "checkpoint.json").write_text(json.dumps({
            "schema_version": 2,
            "source_workflow_schema_version": "Bearer private-token",
        }), encoding="utf-8")

        with patch("omnipet.checkpoint.prepare_run") as prepare:
            with self.assertRaisesRegex(CheckpointError, "^checkpoint restore invalid$"):
                restore_checkpoint_v2(self.project)

        prepare.assert_not_called()

    def test_checkpoint_swap_to_symlink_at_read_boundary_is_rejected(self):
        from omnipet import checkpoint

        marker = self.checkpoint / "checkpoint.json"
        marker.write_text(json.dumps({
            "schema_version": 2,
            "source_workflow_schema_version": 2,
        }), encoding="utf-8")
        outside = self.root / "outside-checkpoint.json"
        outside.write_text(marker.read_text(encoding="utf-8"), encoding="utf-8")
        safe_read = getattr(checkpoint, "_read_json_no_follow", None)

        def swap_then_read(source):
            marker.unlink()
            marker.symlink_to(outside)
            return safe_read(source)

        with patch(
            "omnipet.checkpoint._read_json_no_follow",
            create=True,
            side_effect=swap_then_read,
        ):
            with self.assertRaisesRegex(CheckpointError, "^checkpoint restore invalid$"):
                restore_checkpoint_v2(self.project)

    def test_checkpoint_detection_versions_require_exact_integers(self):
        marker = self.checkpoint / "checkpoint.json"
        cases = (
            {"schema_version": 2.0, "source_workflow_schema_version": 2},
            {"schema_version": True, "source_workflow_schema_version": 2},
            {"schema_version": 2, "source_workflow_schema_version": 2.0},
            {"schema_version": 2, "source_workflow_schema_version": True},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                marker.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(CheckpointError, "^checkpoint restore invalid$"):
                    restore_checkpoint_v2(self.project)

    def test_auto_restore_rejects_phase1_without_dispatch_even_without_run(self):
        (self.checkpoint / "checkpoint.json").write_text(json.dumps({
            "schema_version": 1,
        }), encoding="utf-8")
        (self.run_dir / "workflow.json").unlink(missing_ok=True)

        with (
            patch("omnipet.checkpoint._restore_checkpoint_phase1") as legacy,
            patch("omnipet.checkpoint.restore_checkpoint_v2") as phase2,
            patch("omnipet.approvals.migrate_checkpoint_base_approval") as migrate,
        ):
            with self.assertRaisesRegex(CheckpointError, "^explicit migration required$"):
                restore_checkpoint_for_current_engine(self.project, force=True)

        legacy.assert_not_called()
        phase2.assert_not_called()
        migrate.assert_not_called()

    def test_auto_restore_never_routes_phase1_migration_into_v2_run(self):
        (self.checkpoint / "checkpoint.json").write_text(json.dumps({
            "schema_version": 1,
        }), encoding="utf-8")
        (self.run_dir / "workflow.json").write_text(json.dumps({
            "schema_version": 2, "state": "intake", "blocked": None,
        }), encoding="utf-8")

        with (
            patch("omnipet.checkpoint._restore_checkpoint_phase1") as legacy,
            patch(
                "omnipet.checkpoint.restore_checkpoint_v2",
                side_effect=CheckpointError("explicit migration required"),
            ) as phase2,
            patch("omnipet.approvals.migrate_checkpoint_base_approval") as migrate,
        ):
            with self.assertRaisesRegex(CheckpointError, "^explicit migration required$"):
                restore_checkpoint_for_current_engine(self.project, force=True)

        legacy.assert_not_called()
        phase2.assert_not_called()
        migrate.assert_not_called()

    def test_public_restore_rejects_phase1_before_touching_existing_v2_run(self):
        (self.checkpoint / "checkpoint.json").write_text(json.dumps({
            "schema_version": 1,
        }), encoding="utf-8")
        (self.run_dir / "workflow.json").write_text(json.dumps({
            "schema_version": 2, "state": "intake", "blocked": None,
        }), encoding="utf-8")
        before = self._run_snapshot()

        with (
            patch("omnipet.checkpoint._archive_existing_run") as archive,
            patch("omnipet.checkpoint.prepare_run") as prepare,
            patch("omnipet.approvals.migrate_checkpoint_base_approval") as migrate,
        ):
            with self.assertRaisesRegex(CheckpointError, "^explicit migration required$"):
                restore_checkpoint(self.project, force=True)

        archive.assert_not_called()
        prepare.assert_not_called()
        migrate.assert_not_called()
        self.assertEqual(self._run_snapshot(), before)

    def test_auto_restore_routes_source_v2_checkpoint_to_v2(self):
        (self.checkpoint / "checkpoint.json").write_text(json.dumps({
            "schema_version": 2,
            "source_workflow_schema_version": 2,
        }), encoding="utf-8")
        expected = object()

        with (
            patch("omnipet.checkpoint.restore_checkpoint") as legacy,
            patch("omnipet.checkpoint.restore_checkpoint_v2", return_value=expected) as phase2,
        ):
            result = restore_checkpoint_for_current_engine(self.project)

        self.assertIs(result, expected)
        legacy.assert_not_called()
        phase2.assert_called_once_with(self.project, force=False)

    def test_schema1_rejection_precedes_existing_workflow_inspection(self):
        (self.checkpoint / "checkpoint.json").write_text(json.dumps({
            "schema_version": 1,
        }), encoding="utf-8")
        workflow_path = self.run_dir / "workflow.json"
        invalid_documents = (
            json.dumps({"schema_version": 3, "state": "intake", "blocked": None}),
            json.dumps({"schema_version": 2.0, "state": "intake", "blocked": None}),
            json.dumps({"schema_version": True, "state": "preparing", "blocked": None}),
            "{malformed",
            json.dumps({"state": "preparing", "blocked": None}),
            json.dumps({"schema_version": 1, "state": "intake", "blocked": None}),
            json.dumps({
                "schema_version": 1,
                "state": "preparing",
                "blocked": None,
                "extra": True,
            }),
        )
        before = self._run_snapshot()

        for document in invalid_documents:
            with self.subTest(document=document):
                workflow_path.write_text(document, encoding="utf-8")
                with (
                    patch("omnipet.checkpoint._restore_checkpoint_phase1") as legacy,
                    patch("omnipet.checkpoint.restore_checkpoint_v2") as phase2,
                    patch("omnipet.approvals.migrate_checkpoint_base_approval") as migrate,
                ):
                    with self.assertRaisesRegex(
                        CheckpointError, "^explicit migration required$"
                    ):
                        restore_checkpoint_for_current_engine(self.project, force=True)
                legacy.assert_not_called()
                phase2.assert_not_called()
                migrate.assert_not_called()
                self.assertEqual(
                    (self.run_dir / "existing.bin").read_bytes(),
                    before["existing.bin"],
                )

    def test_auto_restore_rejects_schema1_and_routes_closed_schema2_workflow(self):
        (self.checkpoint / "checkpoint.json").write_text(json.dumps({
            "schema_version": 1,
        }), encoding="utf-8")
        workflow_path = self.run_dir / "workflow.json"
        workflow_path.write_text(json.dumps({
            "schema_version": 1, "state": "preparing", "blocked": None,
        }), encoding="utf-8")

        with (
            patch("omnipet.checkpoint._restore_checkpoint_phase1") as legacy,
            patch("omnipet.checkpoint.restore_checkpoint_v2") as phase2,
            patch("omnipet.approvals.migrate_checkpoint_base_approval") as migrate,
        ):
            with self.assertRaisesRegex(CheckpointError, "^explicit migration required$"):
                restore_checkpoint_for_current_engine(self.project)
        legacy.assert_not_called()
        phase2.assert_not_called()
        migrate.assert_not_called()

        (self.checkpoint / "checkpoint.json").write_text(json.dumps({
            "schema_version": 2,
            "source_workflow_schema_version": 2,
        }), encoding="utf-8")
        workflow_path.write_text(json.dumps({
            "schema_version": 2, "state": "intake", "blocked": None,
        }), encoding="utf-8")
        with (
            patch("omnipet.checkpoint._restore_checkpoint_phase1") as legacy,
            patch("omnipet.checkpoint.restore_checkpoint_v2", return_value="phase2") as phase2,
        ):
            self.assertEqual(
                restore_checkpoint_for_current_engine(self.project), "phase2"
            )
        legacy.assert_not_called()
        phase2.assert_called_once()

    def _run_snapshot(self):
        return {
            str(path.relative_to(self.run_dir)): path.read_bytes()
            for path in self.run_dir.rglob("*")
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
