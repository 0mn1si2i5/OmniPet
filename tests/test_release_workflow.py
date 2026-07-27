import io
import hashlib
import json
import shlex
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from omnipet.cli import main
from omnipet.generation import GeneratedImage
from omnipet.project import load_pet_project
from omnipet.release import (
    approve_project_stage,
    clear_project_block,
    hatch_project,
    init_pet_project,
    project_status,
    qa_project_stage,
    reset_failed_job,
    _failed_job_artifacts,
)
from omnipet.run import STANDARD_JOB_IDS

_ROW9_TEST = ("000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5")
_ROW10_TEST = ("180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5")


class FakeGenerator:
    def __init__(self, *, fail_job=None, malformed_job=None):
        self.calls = []
        self.fail_job = fail_job
        self.malformed_job = malformed_job

    def generate(self, request):
        return self._write(request)

    def edit(self, request):
        return self._write(request)

    def _write(self, request):
        self.calls.append(request.task)
        self.requests = getattr(self, "requests", [])
        self.requests.append(request)
        manifest = json.loads((request.run_root / "imagegen-jobs.json").read_text(encoding="utf-8"))
        job = next(item for item in manifest["jobs"] if item["id"] == request.task)
        if job["status"] != "running" or job.get("metadata", {}).get("attempts") != 1:
            raise AssertionError("job attempt was not persisted before provider call")
        if request.task == self.fail_job:
            raise RuntimeError("provider secret must stay hidden")
        size = (1024, 1024) if request.task == "base" else (1536, 1024)
        pet_request = json.loads((request.run_root / "pet_request.json").read_text(encoding="utf-8"))
        image = Image.new("RGB", size, pet_request["chroma_key"]["hex"])
        draw = ImageDraw.Draw(image)
        slots = 1 if request.task == self.malformed_job else (4 if request.task == "look-cardinals" else (1 if request.task == "base" else 8))
        slot_width = size[0] / slots
        for index in range(slots):
            center = round((index + 0.5) * slot_width)
            draw.rectangle((center - 45, 370, center + 45, 650), fill=(40 + index, 80, 160))
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(request.destination)
        data = request.destination.read_bytes()
        import hashlib
        return GeneratedImage(
            request.destination,
            "image/png",
            hashlib.sha256(data).hexdigest(),
            *size,
            {"fake": True},
        )


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project_root = init_pet_project(self.root, "my-pet")
        manifest = self.project_root / "pet.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "  agent_workflow_version: 2\n", ""
            ),
            encoding="utf-8",
        )
        self.project = load_pet_project(self.root, "my-pet")

    def test_init_is_safe_and_customizes_template(self):
        self.assertEqual(self.project_root, self.root / "pets" / "my-pet")
        self.assertEqual(load_pet_project(self.root, "my-pet").references, ())
        self.assertIn("id: my-pet", (self.project_root / "pet.yaml").read_text(encoding="utf-8"))
        initialized = init_pet_project(self.root, "template-pet")
        self.assertEqual(
            load_pet_project(self.root, "template-pet").agent_workflow_version,
            2,
        )
        self.assertIn(
            "agent_workflow_version: 2",
            (initialized / "pet.yaml").read_text(encoding="utf-8"),
        )
        with self.assertRaises(FileExistsError):
            init_pet_project(self.root, "my-pet")
        with self.assertRaises(ValueError):
            init_pet_project(self.root, "../escape")
        with self.assertRaises(ValueError):
            init_pet_project(self.root, "bad\nid")

    def test_first_hatch_for_legacy_project_uses_phase1(self):
        manifest = self.project_root / "pet.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "  agent_workflow_version: 2\n", ""
            ),
            encoding="utf-8",
        )
        project = load_pet_project(self.root, "my-pet")
        generator = FakeGenerator()

        result = hatch_project(project, generator_factory=lambda _project: generator)

        self.assertEqual(result.state, "awaiting_base_approval")
        self.assertEqual(generator.calls, ["base"])

    def test_init_rolls_back_destination_when_validation_fails(self):
        with patch("omnipet.release.load_pet_project", side_effect=ValueError("invalid")):
            with self.assertRaises(ValueError):
                init_pet_project(self.root, "broken-pet")
        self.assertFalse((self.root / "pets/broken-pet").exists())

    def test_standalone_init_rejects_symlink_destination(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "linked"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            init_pet_project(self.root, "other-pet", standalone=link)

    def test_hatch_creates_one_base_candidate_then_approval_promotes_it(self):
        generator = FakeGenerator()

        result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertEqual(result.state, "awaiting_base_approval")
        self.assertEqual(generator.calls, ["base"])
        run_dir = self.root / ".omnipet" / "runs" / "my-pet"
        jobs = json.loads((run_dir / "imagegen-jobs.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual(jobs[0]["status"], "pending")
        self.assertTrue((run_dir / "qa/candidates/base.json").is_file())

        approved = approve_project_stage(self.project, "base", note="identity accepted")

        self.assertEqual(approved.state, "generating_standard_rows")
        self.assertTrue((self.project_root / "approved/canonical-base.png").is_file())
        self.assertTrue((run_dir / "references/canonical-base.png").is_file())
        self.assertEqual(
            load_pet_project(self.root, "my-pet").canonical_base_path,
            self.project_root / "approved" / "canonical-base.png",
        )
        jobs = json.loads((run_dir / "imagegen-jobs.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual(jobs[0]["status"], "complete")

    def test_base_approval_rejects_candidate_path_or_content_tampering(self):
        hatch_project(self.project, generator_factory=lambda _project: FakeGenerator())
        run_dir = self.root / ".omnipet" / "runs" / "my-pet"
        candidate_path = run_dir / "qa/candidates/base.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate["source_path"] = "../outside.png"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        with self.assertRaises(ValueError):
            approve_project_stage(self.project, "base")

        candidate["source_path"] = "generated-sources/base.png"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        Image.new("RGB", (1024, 1024), "red").save(run_dir / candidate["source_path"])
        with self.assertRaises(ValueError):
            approve_project_stage(self.project, "base")

    def test_standard_requires_closed_semantic_verdict_and_generates_running_left(self):
        generator = FakeGenerator()
        hatch_project(self.project, generator_factory=lambda _project: generator)
        approve_project_stage(self.project, "base")

        standard = hatch_project(self.project, generator_factory=lambda _project: generator)
        self.assertEqual(standard.state, "generating_standard_rows")
        self.assertIn("running-left", generator.calls)
        run_dir = self.root / ".omnipet" / "runs" / "my-pet"
        deterministic = json.loads((run_dir / "qa/standard/review.json").read_text(encoding="utf-8"))
        self.assertTrue(deterministic["ok"])
        verdict = self._verdict("standard.json", {
            "schema_version": 1,
            "stage": "standard-rows",
            "rows": [
                {
                    "id": job_id,
                    "verdict": "pass",
                    "note": "identity and motion pass",
                    "evidence": self._evidence(self._standard_evidence(job_id)),
                }
                for job_id in STANDARD_JOB_IDS
            ],
        })
        self.assertEqual(qa_project_stage(self.project, "standard-rows", verdict_file=verdict).state, "awaiting_standard_rows_approval")
        approve_project_stage(self.project, "standard-rows")

    def test_directions_advance_one_visual_action_after_each_real_review(self):
        generator = FakeGenerator()
        self._approve_standard(generator)
        run_dir = self.root / ".omnipet" / "runs" / "my-pet"

        hatch_project(self.project, generator_factory=lambda _project: generator)
        self.assertEqual(generator.calls[-1:], ["look-cardinals"])
        self.assertEqual(project_status(self.project)["direction_phase"], "cardinals_awaiting_review")
        self.assertFalse((run_dir / "decoded/look-row-9.png").exists())
        qa_project_stage(self.project, "directions", verdict_file=self._direction_verdict(
            "cardinals", (("000", "up"), ("090", "right"), ("180", "down"), ("270", "left"))
        ))

        hatch_project(self.project, generator_factory=lambda _project: generator)
        self.assertEqual(generator.calls[-1:], ["look-row-9"])
        self.assertEqual(
            [image.role for image in generator.requests[-1].grounding_images[-3:]],
            ["layout only", "approved standard contact sheet", "approved cardinal anchors"],
        )
        self.assertEqual(project_status(self.project)["direction_phase"], "row9_awaiting_review")
        self.assertFalse((run_dir / "decoded/look-row-10.png").exists())
        self.assertTrue((run_dir / "qa/directions/look-row-9-continuity.json").is_file())
        qa_project_stage(self.project, "directions", verdict_file=self._direction_verdict(
            "row9", tuple((label, None) for label in ("000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5"))
        ))

        hatch_project(self.project, generator_factory=lambda _project: generator)
        self.assertEqual(generator.calls[-1:], ["look-row-10"])
        self.assertEqual(
            [image.role for image in generator.requests[-1].grounding_images[-4:]],
            [
                "layout only",
                "approved standard contact sheet",
                "approved cardinal anchors",
                "completed first direction row",
            ],
        )
        self.assertEqual(project_status(self.project)["direction_phase"], "row10_awaiting_review")
        qa_project_stage(self.project, "directions", verdict_file=self._direction_verdict(
            "row10", tuple((label, None) for label in ("180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5"))
        ))
        self.assertEqual(project_status(self.project)["direction_phase"], "directions_awaiting_stage_approval")
        approved = approve_project_stage(self.project, "directions")
        self.assertEqual(approved.state, "building_package")
        self.assertFalse((run_dir / "qa/directions/blind-validation.json").exists())

    def test_approval_rejects_tampered_closed_standard_verdict(self):
        generator = FakeGenerator()
        self._approve_standard(generator, approve=False)
        run_dir = self.root / ".omnipet/runs/my-pet"
        review = run_dir / "qa/rows/idle/review.json"
        payload = json.loads(review.read_text())
        payload["extra"] = True
        review.write_text(json.dumps(payload))

        with self.assertRaises(Exception):
            approve_project_stage(self.project, "standard-rows")

    def test_standard_verdict_rejects_stale_bound_artifact(self):
        generator = FakeGenerator()
        hatch_project(self.project, generator_factory=lambda _project: generator)
        approve_project_stage(self.project, "base")
        hatch_project(self.project, generator_factory=lambda _project: generator)
        verdict = self._standard_verdict()
        run_dir = self.root / ".omnipet/runs/my-pet"
        (run_dir / "previews/idle.gif").write_bytes(b"changed")

        with self.assertRaises(ValueError):
            qa_project_stage(self.project, "standard-rows", verdict_file=verdict)

    def test_approval_rejects_tampered_closed_direction_verdict(self):
        generator = FakeGenerator()
        self._complete_direction_reviews(generator)
        run_dir = self.root / ".omnipet/runs/my-pet"
        semantics = run_dir / "qa/directions/direction-semantics.json"
        payload = json.loads(semantics.read_text())
        payload["reviews"]["row9"]["directions"][0]["verdict"] = "fail"
        semantics.write_text(json.dumps(payload))

        with self.assertRaises(Exception):
            approve_project_stage(self.project, "directions")

    def test_generation_failure_blocks_without_retry(self):
        generator = FakeGenerator(fail_job="base")

        result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertEqual(result.state, "blocked")
        self.assertEqual(generator.calls, ["base"])
        self.assertEqual(result.blocked["job"], "base")
        self.assertNotIn("secret", json.dumps(result.blocked))
        run_dir = self.root / ".omnipet" / "runs" / "my-pet"
        job = json.loads((run_dir / "imagegen-jobs.json").read_text(encoding="utf-8"))["jobs"][0]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["metadata"]["attempts"], 1)
        reset_failed_job(self.project, "base")
        self.assertEqual(project_status(self.project)["workflow_state"], "preparing")

    def test_reset_archives_failed_attempt_artifacts_and_retry_succeeds(self):
        class WriteThenFailGenerator(FakeGenerator):
            def _write(self, request):
                self.calls.append(request.task)
                request.destination.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (1024, 1024), "red").save(request.destination)
                raise RuntimeError("failed after write")

        failed = WriteThenFailGenerator()
        self.assertEqual(
            hatch_project(self.project, generator_factory=lambda _project: failed).state,
            "blocked",
        )
        run_dir = self.root / ".omnipet/runs/my-pet"
        source = run_dir / "generated-sources/base.png"
        self.assertTrue(source.is_file())

        reset = reset_failed_job(self.project, "base")

        self.assertEqual(reset.state, "preparing")
        self.assertFalse(source.exists())
        archives = list((self.root / ".omnipet/archives/failed-attempts").glob("base-*"))
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / "generated-sources/base.png").is_file())
        job = json.loads((run_dir / "imagegen-jobs.json").read_text())["jobs"][0]
        self.assertNotIn("source_path", job)
        self.assertNotIn("attempts", job["metadata"])
        self.assertNotIn("started_at", job["metadata"])

        retry = FakeGenerator()
        state = hatch_project(self.project, generator_factory=lambda _project: retry)

        self.assertEqual(state.state, "awaiting_base_approval")
        self.assertEqual(retry.calls, ["base"])

    def test_reset_rejects_symlinked_failed_artifact_without_changing_manifest(self):
        generator = FakeGenerator(fail_job="base")
        hatch_project(self.project, generator_factory=lambda _project: generator)
        run_dir = self.root / ".omnipet/runs/my-pet"
        manifest = (run_dir / "imagegen-jobs.json").read_bytes()
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside")
        source = run_dir / "generated-sources/base.png"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.symlink_to(outside)

        with self.assertRaises(ValueError):
            reset_failed_job(self.project, "base")

        self.assertEqual((run_dir / "imagegen-jobs.json").read_bytes(), manifest)
        self.assertTrue(source.is_symlink())
        self.assertEqual(project_status(self.project)["workflow_state"], "blocked")

    def test_reset_archive_publication_failure_restores_block_and_artifacts(self):
        class WriteThenFailGenerator(FakeGenerator):
            def _write(self, request):
                self.calls.append(request.task)
                request.destination.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (1024, 1024), "red").save(request.destination)
                raise RuntimeError("failed after write")

        hatch_project(self.project, generator_factory=lambda _project: WriteThenFailGenerator())
        run_dir = self.root / ".omnipet/runs/my-pet"
        source = run_dir / "generated-sources/base.png"
        manifest = (run_dir / "imagegen-jobs.json").read_bytes()
        workflow = (run_dir / "workflow.json").read_bytes()
        real_replace = __import__("os").replace

        def fail_publication(source_path, destination_path):
            if Path(destination_path).parent.name == "failed-attempts" and Path(source_path).name.startswith(".base-"):
                raise OSError("publish failed")
            return real_replace(source_path, destination_path)

        with patch("omnipet.release.os.replace", side_effect=fail_publication):
            with self.assertRaises(OSError):
                reset_failed_job(self.project, "base")

        self.assertEqual((run_dir / "imagegen-jobs.json").read_bytes(), manifest)
        self.assertEqual((run_dir / "workflow.json").read_bytes(), workflow)
        self.assertTrue(source.is_file())
        self.assertEqual(project_status(self.project)["workflow_state"], "blocked")

    def test_row10_failed_artifacts_include_final_outputs_not_completed_rows(self):
        run_dir = self.root / ".omnipet/runs/my-pet"
        paths = (
            run_dir / "generated-sources/look-row-10.png",
            run_dir / "qa/directions/look-row-10-registration.json",
            run_dir / "qa/directions/continuity.json",
            run_dir / "qa/directions/contact-sheet.png",
            run_dir / "final/spritesheet-extended.png",
            run_dir / "decoded/idle.png",
        )
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"artifact")

        selected = set(_failed_job_artifacts(run_dir, "look-row-10"))

        self.assertEqual(selected, set(paths[:-1]))
        self.assertNotIn(paths[-1], selected)

    def test_generator_construction_failure_marks_intended_job(self):
        result = hatch_project(
            self.project,
            generator_factory=lambda _project: (_ for _ in ()).throw(RuntimeError("config failed")),
        )
        self.assertEqual(result.blocked["job"], "base")
        run_dir = self.root / ".omnipet/runs/my-pet"
        self.assertEqual(json.loads((run_dir / "imagegen-jobs.json").read_text())["jobs"][0]["status"], "failed")

    def test_later_standard_failure_marks_only_the_same_job_failed(self):
        generator = FakeGenerator(fail_job="running-right")
        hatch_project(self.project, generator_factory=lambda _project: generator)
        approve_project_stage(self.project, "base")

        result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertEqual(result.blocked["job"], "running-right")
        run_dir = self.root / ".omnipet/runs/my-pet"
        statuses = {item["id"]: item["status"] for item in json.loads((run_dir / "imagegen-jobs.json").read_text())["jobs"]}
        self.assertEqual(statuses["idle"], "complete")
        self.assertEqual(statuses["running-right"], "failed")

    def test_aggregate_standard_qa_failure_does_not_relabel_completed_job(self):
        generator = FakeGenerator()
        hatch_project(self.project, generator_factory=lambda _project: generator)
        approve_project_stage(self.project, "base")

        with patch("omnipet.release.make_contact_sheet", side_effect=ValueError("contact failed")):
            result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertIsNone(result.blocked["job"])
        run_dir = self.root / ".omnipet/runs/my-pet"
        statuses = {item["id"]: item["status"] for item in json.loads((run_dir / "imagegen-jobs.json").read_text())["jobs"]}
        self.assertTrue(all(statuses[job_id] == "complete" for job_id in STANDARD_JOB_IDS))
        self.assertEqual(project_status(self.project)["next_action"], "omnipet hatch my-pet --clear-block")
        before = (run_dir / "imagegen-jobs.json").read_bytes()
        self.assertEqual(clear_project_block(self.project).state, "generating_standard_rows")
        self.assertEqual((run_dir / "imagegen-jobs.json").read_bytes(), before)
        no_provider = FakeGenerator(fail_job="idle")
        factories = []
        state = hatch_project(self.project, generator_factory=lambda _project: factories.append(True) or no_provider)
        self.assertEqual(state.state, "generating_standard_rows")
        self.assertEqual(factories, [])
        self.assertEqual(no_provider.calls, [])
        with self.assertRaises(ValueError):
            reset_failed_job(self.project, "idle")

    def test_aggregate_standard_qa_failure_is_deterministic_qa(self):
        generator = FakeGenerator()
        hatch_project(self.project, generator_factory=lambda _project: generator)
        approve_project_stage(self.project, "base")

        with patch("omnipet.release._qa_standard", side_effect=ValueError("private")):
            result = hatch_project(
                self.project, generator_factory=lambda _project: generator
            )

        self.assertIsNone(result.blocked["job"])
        self.assertEqual(
            result.blocked["diagnostic"]["category"], "deterministic-qa"
        )

    def test_clear_block_rejects_job_failure_and_reset_rejects_aggregate_block(self):
        generator = FakeGenerator(fail_job="base")
        hatch_project(self.project, generator_factory=lambda _project: generator)
        with self.assertRaises(ValueError):
            clear_project_block(self.project)

        run_dir = self.root / ".omnipet/runs/my-pet"
        from omnipet.workflow import mark_blocked
        mark_blocked(run_dir, code="aggregate-qa-failed", job=None, evidence=None)
        with self.assertRaises(ValueError):
            reset_failed_job(self.project, "base")
        self.assertEqual(json.loads((run_dir / "imagegen-jobs.json").read_text())["jobs"][0]["status"], "failed")

    def test_later_invalid_prompt_marks_that_job_not_completed_predecessor(self):
        generator = FakeGenerator()
        hatch_project(self.project, generator_factory=lambda _project: generator)
        approve_project_stage(self.project, "base")
        run_dir = self.root / ".omnipet/runs/my-pet"
        manifest_path = run_dir / "imagegen-jobs.json"
        manifest = json.loads(manifest_path.read_text())
        next(item for item in manifest["jobs"] if item["id"] == "running-right")["prompt_file"] = "../bad.md"
        manifest_path.write_text(json.dumps(manifest))

        result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertEqual(result.blocked["job"], "running-right")
        statuses = {item["id"]: item["status"] for item in json.loads(manifest_path.read_text())["jobs"]}
        self.assertEqual(statuses["idle"], "complete")
        self.assertEqual(statuses["running-right"], "failed")

    def test_concurrent_hatch_calls_make_one_provider_call(self):
        entered, release = threading.Event(), threading.Event()
        generator = FakeGenerator()
        original = generator._write

        def blocking(request):
            entered.set()
            release.wait(5)
            return original(request)

        generator._write = blocking
        results = []
        threads = [threading.Thread(target=lambda: results.append(hatch_project(
            self.project, generator_factory=lambda _project: generator
        ))) for _ in range(2)]
        threads[0].start()
        self.assertTrue(entered.wait(5))
        threads[1].start()
        release.set()
        for thread in threads:
            thread.join(10)

        self.assertEqual(generator.calls, ["base"])
        self.assertEqual([item.state for item in results], ["awaiting_base_approval"] * 2)
        lock = self.root / ".omnipet/locks/my-pet.hatch.lock"
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_first_hatch_calls_serialize_before_run_preparation(self):
        import omnipet.release as release_module

        entered, release = threading.Event(), threading.Event()
        actual_prepare = release_module.prepare_run
        active = 0
        maximum = 0
        guard = threading.Lock()

        def blocking_prepare(*args, **kwargs):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            entered.set()
            release.wait(5)
            try:
                return actual_prepare(*args, **kwargs)
            finally:
                with guard:
                    active -= 1

        generator = FakeGenerator()
        results = []
        with patch("omnipet.release.prepare_run", side_effect=blocking_prepare):
            threads = [threading.Thread(target=lambda: results.append(hatch_project(
                self.project, generator_factory=lambda _project: generator
            ))) for _ in range(2)]
            threads[0].start()
            self.assertTrue(entered.wait(5))
            threads[1].start()
            release.set()
            for thread in threads:
                thread.join(10)

        self.assertEqual(maximum, 1)
        self.assertEqual(generator.calls, ["base"])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item.state == "awaiting_base_approval" for item in results))

    def test_hatch_rejects_symlink_lock_without_provider_call(self):
        outside = self.root / "outside.lock"
        outside.write_text("outside")
        locks = self.root / ".omnipet/locks"
        locks.mkdir(parents=True)
        (locks / "my-pet.hatch.lock").symlink_to(outside)
        generator = FakeGenerator()
        with self.assertRaises(ValueError):
            hatch_project(self.project, generator_factory=lambda _project: generator)
        self.assertEqual(generator.calls, [])

    def test_base_approval_failure_rolls_back_every_promoted_artifact(self):
        hatch_project(self.project, generator_factory=lambda _project: FakeGenerator())
        before = self._base_transition_snapshot()

        with patch("omnipet.approvals._write_approvals", side_effect=OSError("write failed")):
            with self.assertRaises(Exception):
                approve_project_stage(self.project, "base", note="accepted")

        self.assertEqual(self._base_transition_snapshot(), before)
        run_dir = self.root / ".omnipet/runs/my-pet"
        self.assertEqual(json.loads((run_dir / "imagegen-jobs.json").read_text())["jobs"][0]["status"], "pending")

    def test_invalid_base_approval_note_changes_nothing(self):
        hatch_project(self.project, generator_factory=lambda _project: FakeGenerator())
        before = self._base_transition_snapshot()
        with self.assertRaises(Exception):
            approve_project_stage(self.project, "base", note="   ")
        self.assertEqual(self._base_transition_snapshot(), before)

    def test_stale_base_candidate_changes_no_transition_artifact(self):
        hatch_project(self.project, generator_factory=lambda _project: FakeGenerator())
        run_dir = self.root / ".omnipet/runs/my-pet"
        Image.new("RGB", (1024, 1024), "red").save(run_dir / "generated-sources/base.png")
        before = self._base_transition_snapshot()
        with self.assertRaises(Exception):
            approve_project_stage(self.project, "base", note="accepted")
        self.assertEqual(self._base_transition_snapshot(), before)

    def test_stale_workflow_refresh_is_rolled_back_with_tampered_candidate(self):
        hatch_project(self.project, generator_factory=lambda _project: FakeGenerator())
        run_dir = self.root / ".omnipet/runs/my-pet"
        (run_dir / "workflow.json").write_text(json.dumps({
            "schema_version": 1,
            "state": "preparing",
            "blocked": None,
        }))
        candidate = run_dir / "qa/candidates/base.json"
        payload = json.loads(candidate.read_text())
        payload["sha256"] = "0" * 64
        candidate.write_text(json.dumps(payload))
        before = self._base_transition_snapshot()

        with self.assertRaises(Exception):
            approve_project_stage(self.project, "base", note="accepted")

        self.assertEqual(self._base_transition_snapshot(), before)

    def test_tampered_cardinal_checkpoint_blocks_row9_before_provider(self):
        generator = FakeGenerator()
        self._approve_standard(generator)
        hatch_project(self.project, generator_factory=lambda _project: generator)
        qa_project_stage(self.project, "directions", verdict_file=self._direction_verdict(
            "cardinals", (("000", "up"), ("090", "right"), ("180", "down"), ("270", "left"))
        ))
        run_dir = self.root / ".omnipet/runs/my-pet"
        (run_dir / "qa/directions/cardinals/sheet.png").write_bytes(b"tampered")
        calls = len(generator.calls)

        result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertEqual(result.state, "blocked")
        self.assertIsNone(result.blocked["job"])
        self.assertEqual(len(generator.calls), calls)

    def test_tampered_row9_checkpoint_blocks_row10_before_provider(self):
        generator = FakeGenerator()
        self._approve_standard(generator)
        hatch_project(self.project, generator_factory=lambda _project: generator)
        qa_project_stage(self.project, "directions", verdict_file=self._direction_verdict(
            "cardinals", (("000", "up"), ("090", "right"), ("180", "down"), ("270", "left"))
        ))
        hatch_project(self.project, generator_factory=lambda _project: generator)
        qa_project_stage(self.project, "directions", verdict_file=self._direction_verdict(
            "row9", tuple((label, None) for label in _ROW9_TEST)
        ))
        run_dir = self.root / ".omnipet/runs/my-pet"
        registration = run_dir / "qa/directions/look-row-9-registration.json"
        payload = json.loads(registration.read_text())
        payload["scale"] = 0.5
        registration.write_text(json.dumps(payload))
        calls = len(generator.calls)

        result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertEqual(result.state, "blocked")
        self.assertIsNone(result.blocked["job"])
        self.assertEqual(len(generator.calls), calls)

    def test_direction_deterministic_failure_does_not_complete_job(self):
        generator = FakeGenerator(malformed_job="look-cardinals")
        self._approve_standard(generator)

        result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertEqual(result.blocked["job"], "look-cardinals")
        run_dir = self.root / ".omnipet/runs/my-pet"
        job = next(item for item in json.loads((run_dir / "imagegen-jobs.json").read_text())["jobs"] if item["id"] == "look-cardinals")
        self.assertEqual(job["status"], "failed")
        self.assertFalse((run_dir / "decoded/look-cardinals.png").exists())

    def test_generation_rejects_tampered_prompt_path_before_provider(self):
        from omnipet.run import prepare_run
        run_dir = prepare_run(self.project, self.root).run_dir
        manifest_path = run_dir / "imagegen-jobs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["jobs"][0]["prompt_file"] = "../private.md"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        generator = FakeGenerator()

        result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertEqual(result.state, "blocked")
        self.assertEqual(generator.calls, [])

    def test_generation_rejects_tampered_prompt_content_before_provider(self):
        from omnipet.run import prepare_run
        run_dir = prepare_run(self.project, self.root).run_dir
        (run_dir / "prompts/base-pet.md").write_text("tampered prompt", encoding="utf-8")
        generator = FakeGenerator()

        result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertEqual(result.state, "blocked")
        self.assertEqual(generator.calls, [])

    def test_registration_evidence_is_closed_and_bound_to_outputs(self):
        generator = FakeGenerator()
        self._complete_direction_reviews(generator)
        run_dir = self.root / ".omnipet/runs/my-pet"

        row9 = json.loads((run_dir / "qa/directions/look-row-9-registration.json").read_text())
        row10 = json.loads((run_dir / "qa/directions/look-row-10-registration.json").read_text())

        self.assertEqual(set(row9), {"ok", "scale", "source_sha256", "registered_sha256"})
        self.assertEqual(
            set(row10),
            {"ok", "scale", "source_sha256", "registered_row_9_sha256", "atlas_sha256"},
        )
        self.assertTrue(all(len(value) == 64 for key, value in row9.items() if key.endswith("sha256")))
        self.assertTrue(all(len(value) == 64 for key, value in row10.items() if key.endswith("sha256")))

    def test_generation_rejects_tampered_manifest_input_before_provider(self):
        from omnipet.run import prepare_run
        run_dir = prepare_run(self.project, self.root).run_dir
        manifest_path = run_dir / "imagegen-jobs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["jobs"][0]["input_images"] = [{"path": "../private.png", "role": "reference"}]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        generator = FakeGenerator()

        result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertEqual(result.state, "blocked")
        self.assertEqual(generator.calls, [])

    def test_generation_rejects_omitted_required_manifest_input(self):
        from omnipet.run import prepare_run
        run_dir = prepare_run(self.project, self.root).run_dir
        manifest_path = run_dir / "imagegen-jobs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        running = next(item for item in manifest["jobs"] if item["id"] == "running-right")
        running["input_images"] = []
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        generator = FakeGenerator()
        hatch_project(self.project, generator_factory=lambda _project: generator)
        approve_project_stage(self.project, "base")

        result = hatch_project(self.project, generator_factory=lambda _project: generator)

        self.assertEqual(result.state, "blocked")
        self.assertNotIn("running-right", generator.calls)

    def test_status_cli_alias_reports_next_action(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = main(["status", "my-pet", "--repo-root", str(self.root)])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["workflow_state"], "preparing")
        self.assertEqual(payload["next_action"], "omnipet hatch my-pet")
        command = shlex.split(payload["next_action"])[1:]
        with patch("omnipet.cli.hatch_project", return_value=type("State", (), {"state": "preparing"})()), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main([*command, "--repo-root", str(self.root)]), 0)
        self.assertEqual(project_status(self.project)["workflow_state"], "preparing")

    def test_standalone_status_uses_dot_selector_in_executable_command(self):
        standalone = self.root / "standalone"
        init_pet_project(self.root, "solo-pet", standalone=standalone)
        project = load_pet_project(standalone, ".")

        command = project_status(project)["next_action"]

        self.assertEqual(command, "omnipet hatch .")
        with patch("omnipet.cli.hatch_project", return_value=type("State", (), {"state": "preparing"})()), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main([*shlex.split(command)[1:], "--repo-root", str(standalone)]), 0)

    def test_approved_package_status_points_to_publication_and_cannot_be_reapproved(self):
        run_dir = self.root / ".omnipet/runs/my-pet"
        run_dir.mkdir(parents=True)
        with patch("omnipet.release.refresh_workflow", return_value=type("State", (), {
            "state": "awaiting_package_approval", "blocked": None,
        })()), patch("omnipet.release._package_is_approved", return_value=True):
            status = project_status(self.project)
        self.assertEqual(status["next_action"], "omnipet package my-pet")

        with patch("omnipet.release.approve_workflow_stage", side_effect=ValueError("not awaiting")):
            with self.assertRaises(ValueError):
                approve_project_stage(self.project, "package")

    def _approve_standard(self, generator, *, approve=True):
        hatch_project(self.project, generator_factory=lambda _project: generator)
        approve_project_stage(self.project, "base")
        hatch_project(self.project, generator_factory=lambda _project: generator)
        verdict = self._verdict("standard.json", {
            "schema_version": 1,
            "stage": "standard-rows",
            "rows": [{
                "id": job_id,
                "verdict": "pass",
                "note": "pass",
                "evidence": self._evidence(self._standard_evidence(job_id)),
            } for job_id in STANDARD_JOB_IDS],
        })
        qa_project_stage(self.project, "standard-rows", verdict_file=verdict)
        if approve:
            approve_project_stage(self.project, "standard-rows")

    def _direction_verdict(self, phase, directions):
        return self._verdict(f"{phase}.json", {
            "schema_version": 1,
            "stage": "directions",
            "phase": phase,
            "evidence": self._evidence(self._direction_evidence(phase)),
            "directions": [
                {"direction": direction, **({"expected": expected} if expected else {}), "verdict": "pass", "note": "semantic direction passes"}
                for direction, expected in directions
            ],
        })

    def _standard_verdict(self):
        return self._verdict("standard-stale.json", {
            "schema_version": 1,
            "stage": "standard-rows",
            "rows": [{
                "id": job_id,
                "verdict": "pass",
                "note": "pass",
                "evidence": self._evidence(self._standard_evidence(job_id)),
            } for job_id in STANDARD_JOB_IDS],
        })

    def _standard_evidence(self, job_id):
        return (
            f"generated-sources/{job_id}.png",
            f"decoded/{job_id}.png",
            f"qa/rows/{job_id}/deterministic.json",
            f"previews/{job_id}.gif",
        )

    def _direction_evidence(self, phase):
        if phase == "cardinals":
            return (
                "generated-sources/look-cardinals.png",
                "decoded/look-cardinals.png",
                "qa/directions/cardinals/deterministic.json",
                "qa/directions/cardinals/sheet.png",
                "decoded/look-cardinals-approved.png",
            )
        job_id = "look-row-9" if phase == "row9" else "look-row-10"
        common = [f"generated-sources/{job_id}.png", f"decoded/{job_id}.png"]
        if phase == "row9":
            common.extend((
                "qa/directions/look-row-9-registered.png",
                "qa/directions/look-row-9-registration.json",
                "qa/directions/look-row-9-continuity.json",
                "qa/directions/look-row-9-contact-sheet.png",
            ))
        else:
            common.extend((
                "qa/directions/look-row-10-registration.json",
                "qa/directions/continuity.json",
                "qa/directions/contact-sheet.png",
            ))
        return tuple(common)

    def _evidence(self, relatives):
        run_dir = self.root / ".omnipet/runs/my-pet"
        return [
            {"path": relative, "sha256": hashlib.sha256((run_dir / (
                "qa/directions/cardinals/sheet.png"
                if relative == "decoded/look-cardinals-approved.png" and not (run_dir / relative).exists()
                else relative
            )).read_bytes()).hexdigest()}
            for relative in relatives
        ]

    def _base_transition_snapshot(self):
        paths = (
            self.project_root / "pet.yaml",
            self.project_root / "approved/canonical-base.png",
            self.root / ".omnipet/runs/my-pet/decoded/base.png",
            self.root / ".omnipet/runs/my-pet/references/canonical-base.png",
            self.root / ".omnipet/runs/my-pet/imagegen-jobs.json",
            self.root / ".omnipet/runs/my-pet/qa/base/review.json",
            self.root / ".omnipet/runs/my-pet/qa/candidates/base.json",
            self.root / ".omnipet/runs/my-pet/qa/approvals.json",
            self.root / ".omnipet/runs/my-pet/workflow.json",
        )
        trees = {}
        for root in (self.project_root, self.root / ".omnipet/runs/my-pet"):
            trees[str(root)] = tuple(sorted(
                (str(path.relative_to(root)), "dir" if path.is_dir() else "file")
                for path in root.rglob("*")
            ))
        return {
            "files": {str(path): path.read_bytes() if path.is_file() else None for path in paths},
            "trees": trees,
        }

    def _verdict(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _complete_direction_reviews(self, generator):
        self._approve_standard(generator)
        hatch_project(self.project, generator_factory=lambda _project: generator)
        qa_project_stage(self.project, "directions", verdict_file=self._direction_verdict(
            "cardinals", (("000", "up"), ("090", "right"), ("180", "down"), ("270", "left"))
        ))
        hatch_project(self.project, generator_factory=lambda _project: generator)
        qa_project_stage(self.project, "directions", verdict_file=self._direction_verdict(
            "row9", tuple((label, None) for label in _ROW9_TEST)
        ))
        hatch_project(self.project, generator_factory=lambda _project: generator)
        qa_project_stage(self.project, "directions", verdict_file=self._direction_verdict(
            "row10", tuple((label, None) for label in _ROW10_TEST)
        ))


if __name__ == "__main__":
    unittest.main()
