import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from omnipet.project import load_pet_project
from omnipet.release import hatch_project, init_pet_project
from omnipet.repair import (
    INVALIDATION_GRAPH,
    RepairError,
    repair_completed_job,
)
from omnipet.run import EXPECTED_JOB_IDS, adopt_canonical, prepare_run
from tests.test_release_workflow import FakeGenerator


def _interrupt_repair_process(repo_root):
    from omnipet import repair

    project = load_pet_project(Path(repo_root), "my-pet")
    manifest_path = (
        Path(repo_root) / ".omnipet/runs/my-pet/imagegen-jobs.json"
    )
    real_replace = repair.os.replace

    def interrupt_before_manifest_publication(source, destination):
        if Path(destination) == manifest_path:
            os._exit(73)
        return real_replace(source, destination)

    repair.os.replace = interrupt_before_manifest_publication
    repair_completed_job(
        project, "base", reason="Simulate process interruption during repair."
    )


def _interrupt_after_archive_publication_process(repo_root):
    from omnipet import repair

    project = load_pet_project(Path(repo_root), "my-pet")
    archives = Path(repo_root) / ".omnipet/archives/repairs"
    real_replace = repair.os.replace

    def interrupt_after_archive_publication(source, destination):
        result = real_replace(source, destination)
        destination = Path(destination)
        if destination.parent == archives and not destination.name.startswith("."):
            os._exit(74)
        return result

    repair.os.replace = interrupt_after_archive_publication
    repair_completed_job(
        project, "look-row-10",
        reason="Simulate interruption after archive publication.",
    )


class JobRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        init_pet_project(self.root, "my-pet")
        manifest = self.root / "pets/my-pet/pet.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "  agent_workflow_version: 2\n", ""
            ),
            encoding="utf-8",
        )
        self.project = load_pet_project(self.root, "my-pet")
        self.run_dir = prepare_run(self.project, self.root).run_dir
        self._complete_run()

    def test_central_graph_covers_the_supported_invalidation_matrix(self):
        self.assertEqual(set(INVALIDATION_GRAPH), set(EXPECTED_JOB_IDS))
        self.assertEqual(INVALIDATION_GRAPH["base"].jobs, EXPECTED_JOB_IDS[1:])
        self.assertEqual(
            INVALIDATION_GRAPH["idle"].jobs,
            ("look-cardinals", "look-row-9", "look-row-10"),
        )
        self.assertEqual(
            INVALIDATION_GRAPH["running-right"].jobs,
            ("running-left", "look-cardinals", "look-row-9", "look-row-10"),
        )
        self.assertEqual(
            INVALIDATION_GRAPH["look-cardinals"].jobs,
            ("look-row-9", "look-row-10"),
        )
        self.assertEqual(INVALIDATION_GRAPH["look-row-9"].jobs, ("look-row-10",))
        self.assertEqual(INVALIDATION_GRAPH["look-row-10"].jobs, ())

    def test_repair_invalidation_matrix_and_upstream_byte_identity(self):
        cases = {
            "running-right": (
                ("running-left", "look-cardinals", "look-row-9", "look-row-10"),
                ("standard-rows", "directions", "package", "delivery"),
                ("base",),
            ),
            "look-cardinals": (
                ("look-row-9", "look-row-10"),
                ("directions", "package", "delivery"),
                ("base", "standard-rows"),
            ),
            "look-row-9": (
                ("look-row-10",),
                ("directions", "package", "delivery"),
                ("base", "standard-rows"),
            ),
            "look-row-10": (
                (),
                ("directions", "package", "delivery"),
                ("base", "standard-rows"),
            ),
        }
        for job_id, (invalidated, stages, retained_approvals) in cases.items():
            with self.subTest(job_id=job_id):
                self._complete_run()
                manifest_before = self._manifest()
                selected_prompt_sha = next(
                    job for job in manifest_before["jobs"] if job["id"] == job_id
                )["metadata"]["prompt_sha256"]
                upstream = {
                    job["id"]: (
                        (self.run_dir / f"generated-sources/{job['id']}.png").read_bytes(),
                        (self.run_dir / f"decoded/{job['id']}.png").read_bytes(),
                        json.dumps(job, sort_keys=True).encode(),
                    )
                    for job in manifest_before["jobs"]
                    if job["id"] not in {job_id, *invalidated}
                }

                result = repair_completed_job(
                    self.project, job_id, reason="Visual review found a pose mismatch."
                )

                self.assertEqual(result.repaired_job, job_id)
                self.assertEqual(result.invalidated_jobs, invalidated)
                self.assertEqual(result.invalidated_stages, stages)
                self.assertFalse(Path(result.archive_path).is_absolute())
                archive = self.root / result.archive_path
                self.assertTrue(archive.is_dir())
                self.assertTrue(
                    (archive / f"artifacts/run/generated-sources/{job_id}.png").is_file()
                )
                jobs = {job["id"]: job for job in self._manifest()["jobs"]}
                self.assertEqual(jobs[job_id]["status"], "pending")
                self.assertEqual(
                    jobs[job_id]["metadata"]["prompt_sha256"],
                    selected_prompt_sha,
                )
                self.assertEqual(
                    jobs[job_id]["metadata"]["repair"]["reason"],
                    "Visual review found a pose mismatch.",
                )
                for dependent in invalidated:
                    self.assertEqual(jobs[dependent]["status"], "pending")
                for upstream_id, expected in upstream.items():
                    self.assertEqual(
                        (self.run_dir / f"generated-sources/{upstream_id}.png").read_bytes(),
                        expected[0],
                    )
                    self.assertEqual(
                        (self.run_dir / f"decoded/{upstream_id}.png").read_bytes(),
                        expected[1],
                    )
                    self.assertEqual(
                        json.dumps(jobs[upstream_id], sort_keys=True).encode(),
                        expected[2],
                    )
                approvals = json.loads(
                    (self.run_dir / "qa/approvals.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    tuple(item["stage"] for item in approvals["approvals"]),
                    retained_approvals,
                )
                self.assertFalse((self.run_dir / "package-complete.json").exists())
                self.assertFalse((self.run_dir / "qa/package-generated").exists())
                self.assertFalse((self.run_dir / "qa/package-reviewed").exists())
                self.assertFalse(self.project.manifest_path.exists())
                self.assertFalse(self.project.spritesheet_path.exists())

    def test_accepts_failed_named_visual_job(self):
        manifest = self._manifest()
        job = next(item for item in manifest["jobs"] if item["id"] == "review")
        job["status"] = "failed"
        for downstream in ("look-cardinals", "look-row-9", "look-row-10"):
            next(item for item in manifest["jobs"] if item["id"] == downstream)["status"] = "pending"
        manifest_path = self.run_dir / "imagegen-jobs.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = repair_completed_job(self.project, "review", reason="Retry the failed row.")

        self.assertEqual(result.repaired_job, "review")
        repaired = next(item for item in self._manifest()["jobs"] if item["id"] == "review")
        self.assertEqual(repaired["status"], "pending")

    def test_repairs_completed_base_and_can_generate_a_new_candidate(self):
        original_manifest = (self.project.root / "pet.yaml").read_bytes()
        original_canonical = self.project.canonical_base_path.read_bytes()

        result = repair_completed_job(
            self.project, "base", reason="The canonical identity needs replacement."
        )

        self.assertEqual(result.invalidated_jobs, EXPECTED_JOB_IDS[1:])
        self.assertEqual(
            result.invalidated_stages,
            ("base", "standard-rows", "directions", "package", "delivery"),
        )
        archive = self.root / result.archive_path
        self.assertEqual(
            (archive / "artifacts/project/approved/canonical-base.png").read_bytes(),
            original_canonical,
        )
        self.assertEqual(
            (archive / "state-before/project/pet.yaml").read_bytes(),
            original_manifest,
        )
        for relative in (
            "qa/standard/contact-sheet.png",
            "qa/standard/previews.json",
            "qa/standard/review.json",
        ):
            self.assertTrue((archive / "artifacts/run" / relative).is_file())
            self.assertFalse((self.run_dir / relative).exists())
        self.assertFalse((self.project.root / "approved/canonical-base.png").exists())
        reloaded = load_pet_project(self.root, "my-pet")
        self.assertIsNone(reloaded.canonical_base_path)
        self.assertTrue(all(
            job["status"] == "pending" for job in self._manifest()["jobs"]
        ))
        approvals = json.loads(
            (self.run_dir / "qa/approvals.json").read_text(encoding="utf-8")
        )
        self.assertEqual(approvals["approvals"], [])

        state = hatch_project(reloaded, generator_factory=lambda _project: FakeGenerator())

        self.assertEqual(state.state, "awaiting_base_approval")

    def test_repairs_failed_base_without_a_durable_canonical(self):
        manifest = self._manifest()
        for job in manifest["jobs"]:
            job["status"] = "failed" if job["id"] == "base" else "pending"
        (self.run_dir / "imagegen-jobs.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        canonical = self.project.canonical_base_path
        canonical.unlink()
        text = (self.project.root / "pet.yaml").read_text(encoding="utf-8")
        (self.project.root / "pet.yaml").write_text(
            text.split("\napproved:\n", 1)[0] + "\n", encoding="utf-8"
        )
        self.project = load_pet_project(self.root, "my-pet")

        result = repair_completed_job(
            self.project, "base", reason="Retry failed base generation."
        )

        self.assertEqual(result.repaired_job, "base")
        self.assertEqual(self._manifest()["jobs"][0]["status"], "pending")

    def test_base_repair_remains_compatible_with_explicit_canonical_adoption(self):
        repair_completed_job(
            self.project, "base", reason="Adopt a corrected durable identity."
        )
        canonical = self.project.root / "approved/canonical-base.png"
        canonical.parent.mkdir(exist_ok=True)
        Image.new("RGB", (32, 32), "navy").save(canonical)
        pet_yaml = self.project.root / "pet.yaml"
        pet_yaml.write_text(
            pet_yaml.read_text(encoding="utf-8").rstrip()
            + "\napproved:\n  canonical_base: approved/canonical-base.png\n",
            encoding="utf-8",
        )
        project = load_pet_project(self.root, "my-pet")

        state = adopt_canonical(project, self.root, reset_generated_work=True)

        self.assertEqual(state.counts["complete"], 1)
        self.assertEqual(self._manifest()["jobs"][0]["status"], "complete")

    def test_rejects_pending_unknown_and_inconsistent_graph(self):
        manifest = self._manifest()
        manifest["jobs"][0]["status"] = "pending"
        (self.run_dir / "imagegen-jobs.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        for job_id in ("base", "missing"):
            with self.subTest(job_id=job_id):
                with self.assertRaises(RepairError):
                    repair_completed_job(self.project, job_id, reason="Valid repair reason.")
        self._complete_run()
        manifest = self._manifest()
        next(item for item in manifest["jobs"] if item["id"] == "look-row-9")["status"] = "pending"
        (self.run_dir / "imagegen-jobs.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(RepairError):
            repair_completed_job(
                self.project, "look-row-10", reason="Dependency closure is inconsistent."
            )

    def test_rejects_package_and_review_recovery_journals_without_mutation(self):
        for relative in (
            "package-publication.json",
            "qa/package-review-publication.json",
        ):
            with self.subTest(relative=relative):
                self._complete_run()
                before = self._snapshot()
                path = self.run_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"state":"prepared"}', encoding="utf-8")
                expected = self._snapshot()
                with self.assertRaises(RepairError):
                    repair_completed_job(
                        self.project, "look-row-10", reason="Repair after QA review."
                    )
                self.assertEqual(self._snapshot(), expected)
                path.unlink()

    def test_next_repair_recovers_a_process_interrupted_after_moves(self):
        process = multiprocessing.get_context("fork").Process(
            target=_interrupt_repair_process, args=(str(self.root),)
        )
        process.start()
        process.join(20)
        self.assertEqual(process.exitcode, 73)
        journal = self.run_dir / "repair-publication.json"
        self.assertTrue(journal.is_file())
        self.assertFalse(
            (self.run_dir / "generated-sources/base.png").exists()
        )
        recovered_project = load_pet_project(self.root, "my-pet")

        result = repair_completed_job(
            recovered_project, "base", reason="Retry after interrupted repair."
        )

        self.assertEqual(result.repaired_job, "base")
        self.assertFalse(journal.exists())
        self.assertTrue((self.root / result.archive_path).is_dir())

    def test_persistent_rollback_failure_keeps_recovery_material(self):
        before = self._snapshot()
        real_replace = os.replace
        manifest_path = self.run_dir / "imagegen-jobs.json"
        rollback_failure_started = False

        def fail_publication_and_rollback(source, destination):
            nonlocal rollback_failure_started
            if Path(destination) == manifest_path:
                rollback_failure_started = True
            if rollback_failure_started:
                raise OSError("persistent injected rollback failure: secret detail")
            return real_replace(source, destination)

        with patch(
            "omnipet.repair.os.replace",
            side_effect=fail_publication_and_rollback,
        ):
            with self.assertRaisesRegex(
                RepairError, "^repair recovery is required$"
            ) as raised:
                repair_completed_job(
                    self.project, "base", reason="Exercise persistent rollback."
                )
        self.assertNotIn("secret detail", str(raised.exception))
        journal = self.run_dir / "repair-publication.json"
        self.assertTrue(journal.is_file())
        journal_value = json.loads(journal.read_text(encoding="utf-8"))
        staging = self.root / journal_value["staging"]
        self.assertTrue(staging.is_dir())

        result = repair_completed_job(
            self.project, "base", reason="Recover and retry the repair."
        )

        self.assertEqual(result.repaired_job, "base")
        self.assertFalse(journal.exists())
        self.assertNotEqual(self._snapshot(), before)

    def test_recovery_rejects_tampered_closed_journal_without_cleanup(self):
        process = multiprocessing.get_context("fork").Process(
            target=_interrupt_repair_process, args=(str(self.root),)
        )
        process.start()
        process.join(20)
        self.assertEqual(process.exitcode, 73)
        journal = self.run_dir / "repair-publication.json"
        value = json.loads(journal.read_text(encoding="utf-8"))
        staging = self.root / value["staging"]
        value["unexpected"] = "must be rejected"
        journal.write_text(json.dumps(value), encoding="utf-8")
        project = load_pet_project(self.root, "my-pet")

        with self.assertRaisesRegex(RepairError, "^repair recovery failed$"):
            repair_completed_job(
                project, "base", reason="Reject a tampered recovery journal."
            )

        self.assertTrue(journal.is_file())
        self.assertTrue(staging.is_dir())

    def test_recovery_rejects_deleted_move_inventory_and_changed_snapshot(self):
        mutations = {
            "deleted-moves": lambda value, staging: value.update(moves=[]),
            "changed-snapshot": self._tamper_snapshot_backup,
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self._complete_run()
                journal, value, staging = self._interrupted_repair()
                mutate(value, staging)
                journal.write_text(json.dumps(value), encoding="utf-8")
                project = load_pet_project(self.root, "my-pet")

                with self.assertRaisesRegex(
                    RepairError, "^repair recovery failed$"
                ):
                    repair_completed_job(
                        project, "base",
                        reason="Reject incomplete recovery inventory.",
                    )

                self.assertTrue(journal.is_file())
                self.assertTrue(staging.is_dir())
                self.assertFalse(
                    (self.run_dir / "generated-sources/base.png").exists()
                )

    def test_recovery_cleanup_is_idempotent_after_live_state_is_restored(self):
        journal, _value, staging = self._interrupted_repair()
        project = load_pet_project(self.root, "my-pet")
        real_rmtree = __import__("shutil").rmtree
        failed = False

        def partially_remove_staging_then_fail(path, *args, **kwargs):
            nonlocal failed
            if Path(path) == staging and not failed:
                failed = True
                real_rmtree(staging / "state-before")
                raise OSError("cleanup interrupted after rollback commit")
            return real_rmtree(path, *args, **kwargs)

        with patch(
            "omnipet.repair.shutil.rmtree",
            side_effect=partially_remove_staging_then_fail,
        ):
            with self.assertRaisesRegex(
                RepairError, "^repair recovery failed$"
            ):
                repair_completed_job(
                    project, "base", reason="Resume interrupted cleanup."
                )
        self.assertEqual(
            json.loads(journal.read_text(encoding="utf-8"))["state"],
            "rolled-back",
        )

        result = repair_completed_job(
            load_pet_project(self.root, "my-pet"),
            "base",
            reason="Finish cleanup and retry repair.",
        )

        self.assertEqual(result.repaired_job, "base")
        self.assertFalse(journal.exists())

    def test_archive_publication_is_the_commit_point_when_final_fsync_fails(self):
        journal = self.run_dir / "repair-publication.json"
        real_fsync = __import__("omnipet.repair", fromlist=["_fsync_directory"])._fsync_directory
        injected = False

        def fail_after_journal_cleanup(path):
            nonlocal injected
            archives = self.root / ".omnipet/archives/repairs"
            committed = archives.is_dir() and any(
                child.name.startswith("my-pet-look-row-10-")
                and not child.name.startswith(".")
                for child in archives.iterdir()
            )
            if Path(path) == self.run_dir and committed and not journal.exists() and not injected:
                injected = True
                raise OSError("post-commit directory fsync failed")
            return real_fsync(path)

        with patch(
            "omnipet.repair._fsync_directory",
            side_effect=fail_after_journal_cleanup,
        ):
            result = repair_completed_job(
                self.project, "look-row-10",
                reason="Commit despite post-publication cleanup failure.",
            )

        self.assertTrue(injected)
        self.assertEqual(result.repaired_job, "look-row-10")
        self.assertFalse(journal.exists())
        self.assertTrue((self.root / result.archive_path).is_dir())

    def test_next_same_repair_returns_committed_result_after_archive_exit(self):
        process = multiprocessing.get_context("fork").Process(
            target=_interrupt_after_archive_publication_process,
            args=(str(self.root),),
        )
        process.start()
        process.join(20)
        self.assertEqual(process.exitcode, 74)
        journal = self.run_dir / "repair-publication.json"
        value = json.loads(journal.read_text(encoding="utf-8"))

        result = repair_completed_job(
            load_pet_project(self.root, "my-pet"),
            "look-row-10",
            reason="Resume the same committed repair.",
        )

        self.assertEqual(result.repaired_job, "look-row-10")
        self.assertEqual(result.archive_path, value["archive"])
        self.assertEqual(result.invalidated_jobs, ())
        self.assertEqual(
            result.invalidated_stages,
            ("directions", "package", "delivery"),
        )
        self.assertFalse(journal.exists())
        self.assertTrue((self.root / result.archive_path).is_dir())

    def test_committed_recovery_does_not_replace_a_different_explicit_job(self):
        process = multiprocessing.get_context("fork").Process(
            target=_interrupt_after_archive_publication_process,
            args=(str(self.root),),
        )
        process.start()
        process.join(20)
        self.assertEqual(process.exitcode, 74)
        old_journal = json.loads(
            (self.run_dir / "repair-publication.json").read_text(encoding="utf-8")
        )

        result = repair_completed_job(
            load_pet_project(self.root, "my-pet"),
            "running-right",
            reason="Repair the explicitly selected standard row.",
        )

        self.assertEqual(result.repaired_job, "running-right")
        self.assertNotEqual(result.archive_path, old_journal["archive"])
        self.assertEqual(
            result.invalidated_jobs,
            ("running-left", "look-cardinals", "look-row-9", "look-row-10"),
        )
        self.assertTrue((self.root / old_journal["archive"]).is_dir())
        self.assertTrue((self.root / result.archive_path).is_dir())

    def test_rejects_nested_symlinks_and_special_nodes_before_mutation(self):
        mutations = {
            "nested-symlink": self._add_nested_symlink,
        }
        if hasattr(os, "mkfifo"):
            mutations["fifo"] = self._add_fifo
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self._complete_run()
                mutate()
                before = self._snapshot()

                with self.assertRaises(RepairError):
                    repair_completed_job(
                        self.project, "base",
                        reason="Unsafe aggregate QA must be rejected.",
                    )

                self.assertEqual(self._snapshot(), before)
                self.assertFalse(
                    (self.run_dir / "repair-publication.json").exists()
                )

    def test_rolls_back_every_move_and_publication_boundary(self):
        for job_id in ("look-row-10", "base"):
            self._complete_run()
            with patch(
                "omnipet.repair.os.replace", wraps=__import__("os").replace
            ) as replace:
                repair_completed_job(
                    self.project, job_id, reason="Count transaction boundaries."
                )
            boundary_count = replace.call_count
            self.assertGreaterEqual(boundary_count, 6)

            for failure_at in range(1, boundary_count + 1):
                with self.subTest(job_id=job_id, failure_at=failure_at):
                    self._complete_run()
                    before = self._snapshot()
                    real_replace = __import__("os").replace
                    calls = 0

                    def fail_once(source, destination):
                        nonlocal calls
                        calls += 1
                        if calls == failure_at:
                            raise OSError("injected publication failure")
                        return real_replace(source, destination)

                    with patch("omnipet.repair.os.replace", side_effect=fail_once):
                        with self.assertRaises(RepairError):
                            repair_completed_job(
                                self.project, job_id,
                                reason="Rollback boundary test.",
                            )
                    self.assertEqual(self._snapshot(), before)

    def test_reason_is_bounded_and_secret_safe(self):
        for reason in ("", "x" * 241, "Authorization: Bearer secret-token-value"):
            with self.subTest(reason=reason[:20]):
                with self.assertRaises(RepairError):
                    repair_completed_job(self.project, "idle", reason=reason)

    def _complete_run(self):
        for child in tuple(self.run_dir.iterdir()):
            if child.name in {
                "pet_request.json", "omnipet-run.json", "imagegen-jobs.json",
                "prompts", "references",
            }:
                continue
            if child.is_dir():
                __import__("shutil").rmtree(child)
            else:
                child.unlink()
        manifest = json.loads(
            (self.run_dir / "imagegen-jobs.json").read_text(encoding="utf-8")
        ) if (self.run_dir / "imagegen-jobs.json").exists() else None
        if manifest is None:
            prepare_run(self.project, self.root)
            manifest = self._manifest()
        for job in manifest["jobs"]:
            prompt_sha256 = job.get("metadata", {}).get("prompt_sha256")
            job.update({
                "status": "complete",
                "source_path": f"generated-sources/{job['id']}.png",
                "completed_at": "2026-07-23T00:00:00+00:00",
                "metadata": {
                    "prompt_sha256": prompt_sha256,
                    "sha256": "a" * 64,
                    "attempts": 1,
                },
            })
            for relative in (
                f"generated-sources/{job['id']}.png",
                f"decoded/{job['id']}.png",
                f"qa/visual-jobs/{job['id']}.result.json",
            ):
                self._write(relative, f"{job['id']}:{relative}".encode())
        (self.run_dir / "imagegen-jobs.json").write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8"
        )
        canonical = self.project.root / "approved/canonical-base.png"
        canonical.parent.mkdir(exist_ok=True)
        canonical.write_bytes(b"durable canonical")
        pet_yaml = self.project.root / "pet.yaml"
        text = pet_yaml.read_text(encoding="utf-8")
        if "\napproved:\n" not in text:
            pet_yaml.write_text(
                text.rstrip()
                + "\napproved:\n  canonical_base: approved/canonical-base.png\n",
                encoding="utf-8",
            )
        self.project = load_pet_project(self.root, "my-pet")
        for job_id in EXPECTED_JOB_IDS[1:10]:
            self._write(f"qa/rows/{job_id}/deterministic.json", b'{"ok":true}')
            self._write(f"qa/rows/{job_id}/review.json", b'{"ok":true}')
            self._write(f"previews/{job_id}.gif", b"gif")
        for relative in (
            "qa/standard/contact-sheet.png",
            "qa/standard/previews.json",
            "qa/standard/review.json",
            "decoded/look-cardinals-approved.png",
            "qa/directions/cardinals/deterministic.json",
            "qa/directions/cardinals/review.json",
            "qa/directions/cardinals/sheet.png",
            "qa/directions/look-row-9-registered.png",
            "qa/directions/look-row-9-registration.json",
            "qa/directions/look-row-9-continuity.json",
            "qa/directions/look-row-9-contact-sheet.png",
            "final/spritesheet-extended.png",
            "qa/directions/look-row-10-registration.json",
            "qa/directions/direction-semantics.json",
            "qa/directions/continuity.json",
            "qa/directions/contact-sheet.png",
            "final/pet.json",
            "final/package-source.png",
            "final/spritesheet-extended.webp",
            "qa/package-generated/validation.json",
            "qa/package-reviewed/final-visual-review.json",
            "package-complete.json",
        ):
            self._write(relative, b'{"ok":true}' if relative.endswith(".json") else b"artifact")
        approvals = {
            "schema_version": 1,
            "approvals": [
                {
                    "stage": stage,
                    "artifacts": [{"path": "x", "sha256": "a" * 64}],
                    "approved_at": "2026-07-23T00:00:00+00:00",
                }
                for stage in ("base", "standard-rows", "directions", "package")
            ],
        }
        self._write("qa/approvals.json", json.dumps(approvals).encode())
        self._write(
            "workflow.json",
            b'{"schema_version":1,"state":"complete","blocked":null}',
        )
        self.project.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.project.manifest_path.write_bytes(b"published manifest")
        self.project.spritesheet_path.write_bytes(b"published atlas")

    def _write(self, relative, content):
        path = self.run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _add_nested_symlink(self):
        outside = self.root / "outside-evidence.txt"
        outside.write_bytes(b"outside")
        nested = self.run_dir / "qa/standard/nested"
        nested.mkdir()
        (nested / "link").symlink_to(outside)

    def _add_fifo(self):
        nested = self.run_dir / "qa/standard/nested"
        nested.mkdir()
        os.mkfifo(nested / "evidence.fifo")

    def _interrupted_repair(self):
        process = multiprocessing.get_context("fork").Process(
            target=_interrupt_repair_process, args=(str(self.root),)
        )
        process.start()
        process.join(20)
        self.assertEqual(process.exitcode, 73)
        journal = self.run_dir / "repair-publication.json"
        value = json.loads(journal.read_text(encoding="utf-8"))
        return journal, value, self.root / value["staging"]

    def _tamper_snapshot_backup(self, value, staging):
        snapshot = next(item for item in value["snapshots"] if item["existed"])
        (staging / snapshot["backup"]).write_bytes(b"changed snapshot bytes")

    def _manifest(self):
        return json.loads(
            (self.run_dir / "imagegen-jobs.json").read_text(encoding="utf-8")
        )

    def _snapshot(self):
        roots = (self.run_dir, self.project.root)
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for root in roots
            if root.exists()
            for path in root.rglob("*")
            if path.is_file() and not path.name.endswith(".lock")
            and ".omnipet/archives/repairs" not in str(path)
        }


if __name__ == "__main__":
    unittest.main()
