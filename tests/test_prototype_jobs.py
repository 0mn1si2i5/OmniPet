import concurrent.futures
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from omnipet.design_pack import submit_design, submit_intake
from omnipet.generation import GeneratedImage
from omnipet.project import PetReference
from omnipet.prototype_jobs import (
    PrototypeJobError,
    generate_next_prototype,
    prototype_job_status,
)
from omnipet.release import initialize_design_run
from omnipet.release import hatch_prototype_run, prototype_run_status
from omnipet.workflow import load_workflow_v2, refresh_workflow
from omnipet.workflow import WorkflowError
from tests.design_pack_fixtures import (
    napoleon_design_documents, read_json, valid_design_documents, valid_intake,
)


class RecordingGenerator:
    def __init__(self, *, fail=False, barrier=None):
        self.requests = []
        self.fail = fail
        self.barrier = barrier

    def generate(self, request):
        raise AssertionError("grounded prototypes must use edit")

    def edit(self, request):
        self.requests.append(request)
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        if self.fail:
            raise RuntimeError("private provider failure")
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (1024, 1024), (20, 40, 80, 255)).save(request.destination)
        content = request.destination.read_bytes()
        return GeneratedImage(
            request.destination, "image/png", hashlib.sha256(content).hexdigest(),
            1024, 1024, {"principal": "test-generator"},
        )


class PrototypeJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.reference = self.root / "portrait.png"
        Image.new("RGB", (12, 12), "navy").save(self.reference)
        self.run_dir = self.root / "run"
        initialize_design_run(
            self.run_dir,
            pet_id="sample-pet",
            references=(PetReference(self.reference, "identity"),),
        )
        submit_intake(self.run_dir, valid_intake(self.run_dir))
        self.documents = valid_design_documents(self.run_dir)

    def submit(self, documents=None):
        contract, rationale, storyboard, plan, look = documents or self.documents
        return submit_design(
            self.run_dir, contract=contract, rationale=rationale,
            storyboard=storyboard, prototype_plan=plan, look_mechanics=look,
        )

    def test_submit_atomically_publishes_exact_declared_prototype_manifest(self):
        self.submit()
        manifest = read_json(self.run_dir / "imagegen-jobs.json")
        plan_bytes = (self.run_dir / "design/prototype-plan.json").read_bytes()

        self.assertEqual(set(manifest), {"schema_version", "design_artifacts", "jobs"})
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual([job["id"] for job in manifest["jobs"]], ["canonical", "cycle"])
        self.assertNotIn("base", {job["id"] for job in manifest["jobs"]})
        self.assertTrue(set(job["id"] for job in manifest["jobs"]).isdisjoint({
            "idle", "running-right", "look-cardinals", "look-row-9",
        }))
        expected_fields = {
            "id", "pose_id", "kind", "status", "depends_on", "design_revision",
            "prototype_plan_sha256", "prompt_file", "input_images", "output_path",
            "generation_method", "metadata",
        }
        for job in manifest["jobs"]:
            self.assertEqual(set(job), expected_fields)
            self.assertEqual(job["id"], job["pose_id"])
            self.assertEqual(job["kind"], "prototype")
            self.assertEqual(job["status"], "pending")
            self.assertEqual(job["design_revision"], "design-0001")
            self.assertEqual(
                job["prototype_plan_sha256"], hashlib.sha256(plan_bytes).hexdigest()
            )
            prompt = self.run_dir / job["prompt_file"]
            self.assertEqual(job["prompt_file"], f"prompts/prototypes/{job['id']}.md")
            self.assertTrue(prompt.is_file())
            self.assertEqual(job["metadata"], {
                "prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
                "canvas": {"aspect_ratio": "1:1", "image_size": "1K"},
            })
            self.assertEqual(job["input_images"], [{
                "path": "references/reference-01.png",
                "role": "identity",
                "sha256": hashlib.sha256(self.reference.read_bytes()).hexdigest(),
            }])
            self.assertIn("Reference roles: identity", prompt.read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["design_artifacts"]), {
            "omnipet-run.json", "design/intake.json", "design/design-contract.json",
            "design/design-rationale.md", "design/state-storyboard.json",
            "design/prototype-plan.json", "design/look-mechanics.json",
        })
        for relative, digest in manifest["design_artifacts"].items():
            self.assertEqual(digest, hashlib.sha256((self.run_dir / relative).read_bytes()).hexdigest())
        self.assertEqual(manifest["jobs"][0]["depends_on"], [])
        self.assertEqual(manifest["jobs"][0]["output_path"], "decoded/canonical.png")
        self.assertEqual(manifest["jobs"][1]["depends_on"], ["canonical"])
        self.assertEqual(manifest["jobs"][1]["output_path"], "decoded/prototypes/cycle.png")

    def test_engine_before_design_submission_never_calls_provider(self):
        generator = RecordingGenerator()
        with self.assertRaises(PrototypeJobError):
            generate_next_prototype(self.run_dir, generator)
        self.assertEqual(generator.requests, [])

    def test_public_release_wrapper_executes_exactly_one_prototype_action(self):
        self.submit()
        generator = RecordingGenerator()
        self.assertEqual(prototype_run_status(self.run_dir)["ready_id"], "canonical")
        result = hatch_prototype_run(self.run_dir, generator)
        self.assertEqual(result, {"job_id": "canonical", "status": "complete"})
        self.assertEqual([request.task for request in generator.requests], ["canonical"])
        self.assertEqual(prototype_run_status(self.run_dir)["ready_id"], "cycle")

    def test_public_release_wrapper_requires_prototyping(self):
        generator = RecordingGenerator()
        with self.assertRaises(ValueError):
            hatch_prototype_run(self.run_dir, generator)
        self.assertEqual(generator.requests, [])

    def test_initializer_does_not_create_prototype_publication_directories(self):
        with tempfile.TemporaryDirectory() as name:
            run_dir = Path(name).resolve() / "run"
            initialize_design_run(run_dir, "sample-pet")
            self.assertFalse((run_dir / "prompts").exists())
            self.assertFalse((run_dir / "generated-sources").exists())

    def test_reference_roles_select_exact_matching_references_in_source_order(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            first = root / "first.png"; Image.new("RGB", (8, 8), "red").save(first)
            second = root / "second.png"; Image.new("RGB", (8, 8), "blue").save(second)
            third = root / "third.png"; Image.new("RGB", (8, 8), "green").save(third)
            run_dir = root / "run"
            initialize_design_run(run_dir, "sample-pet", (
                PetReference(first, "style"), PetReference(second, "identity"),
                PetReference(third, "style"),
            ))
            submit_intake(run_dir, valid_intake(run_dir))
            documents = valid_design_documents(run_dir)
            for prototype in documents[3]["prototypes"]:
                prototype["reference_roles"] = ["style"]
            contract, rationale, storyboard, plan, look = documents
            submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)
            jobs = read_json(run_dir / "imagegen-jobs.json")["jobs"]
            self.assertEqual(
                [item["path"] for item in jobs[0]["input_images"]],
                ["references/reference-01.png", "references/reference-03.png"],
            )
            generator = RecordingGenerator()
            generate_next_prototype(run_dir, generator)
            self.assertEqual([item.role for item in generator.requests[0].grounding_images], ["style", "style"])

    def test_unknown_or_missing_reference_role_rejects_design(self):
        for roles in ([], ["missing"]):
            documents = valid_design_documents(self.run_dir)
            documents[3]["prototypes"][0]["reference_roles"] = roles
            with self.subTest(roles=roles), self.assertRaises(Exception):
                self.submit(documents)

    def test_status_and_each_action_expose_or_execute_only_one_ready_job(self):
        self.submit()
        self.assertEqual(prototype_job_status(self.run_dir)["ready_ids"], ["canonical"])
        generator = RecordingGenerator()

        result = generate_next_prototype(self.run_dir, generator)

        self.assertEqual(result["job_id"], "canonical")
        self.assertEqual(len(generator.requests), 1)
        request = generator.requests[0]
        self.assertEqual(request.task, "canonical")
        self.assertEqual((request.aspect_ratio, request.image_size), ("1:1", "1K"))
        self.assertEqual([item.role for item in request.grounding_images], ["identity"])
        source = self.run_dir / "generated-sources/prototypes/canonical.png"
        decoded = self.run_dir / "decoded/canonical.png"
        promoted = self.run_dir / "references/canonical-base.png"
        self.assertEqual(source.read_bytes(), decoded.read_bytes())
        self.assertEqual(source.read_bytes(), promoted.read_bytes())
        self.assertEqual(prototype_job_status(self.run_dir)["ready_ids"], ["cycle"])

        generate_next_prototype(self.run_dir, generator)
        self.assertEqual(len(generator.requests), 2)
        self.assertEqual(
            [item.path.relative_to(self.run_dir).as_posix() for item in generator.requests[1].grounding_images],
            ["references/reference-01.png", "references/canonical-base.png"],
        )
        self.assertIsNone(generate_next_prototype(self.run_dir, generator))
        self.assertEqual(len(generator.requests), 2)

    def test_status_exposes_only_first_ready_job_when_frontier_has_multiple_jobs(self):
        documents = napoleon_design_documents(self.run_dir)
        self.submit(documents)
        generate_next_prototype(self.run_dir, RecordingGenerator())
        self.assertEqual(prototype_job_status(self.run_dir)["ready_ids"], ["cycle"])

    def test_resume_skips_complete_and_failure_marks_only_active_job_and_blocks(self):
        self.submit()
        generate_next_prototype(self.run_dir, RecordingGenerator())
        generator = RecordingGenerator(fail=True)

        with self.assertRaisesRegex(PrototypeJobError, "prototype generation failed"):
            generate_next_prototype(self.run_dir, generator)

        statuses = {job["id"]: job["status"] for job in read_json(self.run_dir / "imagegen-jobs.json")["jobs"]}
        self.assertEqual(statuses, {"canonical": "complete", "cycle": "failed"})
        workflow = load_workflow_v2(self.run_dir)
        self.assertEqual(workflow.state, "blocked")
        self.assertEqual(workflow.blocked["job_id"], "cycle")
        self.assertEqual(workflow.blocked["code"], "prototype-generation-failed")

    def test_keyboard_interrupt_after_attempt_is_safely_failed_then_reraised(self):
        self.submit()

        class InterruptedGenerator:
            def edit(self, request):
                raise KeyboardInterrupt("stop")

        with self.assertRaises(KeyboardInterrupt):
            generate_next_prototype(self.run_dir, InterruptedGenerator())
        self.assertEqual(read_json(self.run_dir / "imagegen-jobs.json")["jobs"][0]["status"], "failed")
        self.assertEqual(load_workflow_v2(self.run_dir).blocked["job_id"], "canonical")

    def test_request_construction_failure_after_attempt_is_failed_and_blocked(self):
        self.submit()
        generator = RecordingGenerator()
        with patch("omnipet.prototype_jobs._request", side_effect=KeyboardInterrupt("request stop")):
            with self.assertRaises(KeyboardInterrupt):
                generate_next_prototype(self.run_dir, generator)
        self.assertEqual(generator.requests, [])
        self.assertEqual(read_json(self.run_dir / "imagegen-jobs.json")["jobs"][0]["status"], "failed")
        self.assertEqual(load_workflow_v2(self.run_dir).state, "blocked")

    def test_stale_running_is_recovered_by_status_generate_and_workflow_load(self):
        for operation in ("status", "generate", "load", "refresh"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve(); reference = root / "portrait.png"
                Image.new("RGB", (8, 8), "red").save(reference); run_dir = root / "run"
                initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
                submit_intake(run_dir, valid_intake(run_dir))
                contract, rationale, storyboard, plan, look = valid_design_documents(run_dir)
                submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)
                manifest = read_json(run_dir / "imagegen-jobs.json")
                manifest["jobs"][0]["status"] = "running"
                manifest["jobs"][0]["metadata"].update({"attempts": 1, "started_at": "2026-01-01T00:00:00+00:00"})
                (run_dir / "imagegen-jobs.json").write_text(json.dumps(manifest), encoding="utf-8")
                if operation == "status":
                    with self.assertRaises(PrototypeJobError): prototype_job_status(run_dir)
                elif operation == "generate":
                    with self.assertRaises(PrototypeJobError): generate_next_prototype(run_dir, RecordingGenerator())
                elif operation == "load":
                    load_workflow_v2(run_dir)
                else:
                    refresh_workflow(run_dir)
                self.assertEqual(read_json(run_dir / "imagegen-jobs.json")["jobs"][0]["status"], "failed")
                self.assertEqual(read_json(run_dir / "workflow.json")["blocked"]["code"], "prototype-attempt-interrupted")

    def test_failure_publication_recovers_crash_at_each_boundary(self):
        from omnipet import prototype_jobs
        boundaries = ("prepared", "manifest-installed", "workflow-installed")
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve(); reference = root / "portrait.png"
                Image.new("RGB", (8, 8), "red").save(reference); run_dir = root / "run"
                initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
                submit_intake(run_dir, valid_intake(run_dir))
                contract, rationale, storyboard, plan, look = valid_design_documents(run_dir)
                submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)
                original = prototype_jobs._failure_boundary

                def crash(current):
                    if current == boundary:
                        raise KeyboardInterrupt("crash")
                    return original(current)

                with patch("omnipet.prototype_jobs._failure_boundary", side_effect=crash):
                    with self.assertRaises(KeyboardInterrupt):
                        generate_next_prototype(run_dir, RecordingGenerator(fail=True))
                load_workflow_v2(run_dir)
                self.assertEqual(read_json(run_dir / "imagegen-jobs.json")["jobs"][0]["status"], "failed")
                self.assertEqual(read_json(run_dir / "workflow.json")["state"], "blocked")
                self.assertFalse((run_dir / "prototype-failure-publication.json").exists())

    def test_tampered_failure_journal_is_retained_without_any_write(self):
        def coordinated(field, value):
            def mutate(journal, current):
                journal["manifest"]["jobs"][0][field] = copy.deepcopy(value)
                current["jobs"][0][field] = copy.deepcopy(value)
            return mutate

        mutations = {
            "journal-job-id": lambda journal, current: journal.update(job_id="cycle"),
            "embedded-id": lambda journal, current: journal["manifest"]["jobs"][0].update(id="cycle"),
            "current-dependency": lambda journal, current: current["jobs"][0].update(depends_on=["cycle"]),
            "coordinated-dependency": coordinated("depends_on", ["cycle"]),
            "coordinated-prompt": coordinated("prompt_file", "prompts/prototypes/cycle.md"),
            "coordinated-input": coordinated("input_images", []),
            "coordinated-output": coordinated("output_path", "decoded/prototypes/cycle.png"),
            "coordinated-plan-hash": coordinated("prototype_plan_sha256", "0" * 64),
            "other-job-status": lambda journal, current: journal["manifest"]["jobs"][1].update(status="complete"),
            "active-status": lambda journal, current: journal["manifest"]["jobs"][0].update(status="running"),
            "embedded-metadata": lambda journal, current: journal["manifest"]["jobs"][0]["metadata"].update(unexpected="tampered"),
            "coordinated-metadata": lambda journal, current: (
                journal["manifest"]["jobs"][0]["metadata"].update(unexpected="tampered"),
                current["jobs"][0]["metadata"].update(unexpected="tampered"),
            ),
            "workflow-job-id": lambda journal, current: journal["workflow"]["blocked"].update(job_id="cycle"),
            "workflow-prior": lambda journal, current: journal["workflow"]["blocked"].update(prior_state="designing"),
            "workflow-code": lambda journal, current: journal["workflow"]["blocked"].update(code="other"),
            "workflow-root-key": lambda journal, current: journal["workflow"]["blocked"].update(root_failure_key="other"),
            "workflow-state": lambda journal, current: journal["workflow"].update(state="prototyping"),
            "workflow-extra": lambda journal, current: journal["workflow"].update(extra=True),
            "workflow-missing": lambda journal, current: journal["workflow"].pop("blocked"),
            "journal-extra": lambda journal, current: journal.update(extra=True),
            "journal-missing": lambda journal, current: journal.pop("workflow"),
        }
        for operation in ("status", "generate", "load"):
            for mutation, mutate in mutations.items():
                with self.subTest(operation=operation, mutation=mutation), tempfile.TemporaryDirectory() as name:
                    root = Path(name).resolve(); reference = root / "portrait.png"
                    Image.new("RGB", (8, 8), "red").save(reference); run_dir = root / "run"
                    initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
                    submit_intake(run_dir, valid_intake(run_dir))
                    contract, rationale, storyboard, plan, look = valid_design_documents(run_dir)
                    submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)
                    with patch("omnipet.prototype_jobs._failure_boundary", side_effect=KeyboardInterrupt("crash")):
                        with self.assertRaises(KeyboardInterrupt):
                            generate_next_prototype(run_dir, RecordingGenerator(fail=True))
                    journal_path = run_dir / "prototype-failure-publication.json"
                    manifest_path = run_dir / "imagegen-jobs.json"
                    workflow_path = run_dir / "workflow.json"
                    journal = read_json(journal_path); current = read_json(manifest_path)
                    mutate(journal, current)
                    journal_path.write_text(json.dumps(journal), encoding="utf-8")
                    manifest_path.write_text(json.dumps(current), encoding="utf-8")
                    sentinel = root / "sentinel"; sentinel.write_bytes(b"external")
                    before = {
                        "journal": journal_path.read_bytes(),
                        "manifest": manifest_path.read_bytes(),
                        "workflow": workflow_path.read_bytes(),
                    }
                    generator = RecordingGenerator()

                    if operation == "status":
                        with self.assertRaisesRegex(PrototypeJobError, "prototype job status is invalid"):
                            prototype_job_status(run_dir)
                    elif operation == "generate":
                        with self.assertRaisesRegex(PrototypeJobError, "prototype job validation failed"):
                            generate_next_prototype(run_dir, generator)
                    else:
                        with self.assertRaisesRegex(WorkflowError, "workflow is invalid"):
                            load_workflow_v2(run_dir)

                    self.assertEqual(generator.requests, [])
                    self.assertEqual(sentinel.read_bytes(), b"external")
                    self.assertEqual(journal_path.read_bytes(), before["journal"])
                    self.assertEqual(manifest_path.read_bytes(), before["manifest"])
                    self.assertEqual(workflow_path.read_bytes(), before["workflow"])

    def test_design_rejects_non_generate_methods_before_publication(self):
        for method in ("derive", "reuse"):
            documents = valid_design_documents(self.run_dir)
            documents[3]["prototypes"][1]["generation_method"] = method
            documents[3]["estimated_provider_calls"] = 1
            with self.subTest(method=method), self.assertRaises(Exception):
                self.submit(documents)
            self.assertFalse((self.run_dir / "imagegen-jobs.json").exists())

    def test_completion_publication_recovers_crash_at_each_boundary(self):
        from omnipet import prototype_jobs
        for boundary in ("prepared", "output-installed", "canonical-installed", "manifest-installed"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve(); reference = root / "portrait.png"
                Image.new("RGB", (8, 8), "red").save(reference); run_dir = root / "run"
                initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
                submit_intake(run_dir, valid_intake(run_dir))
                contract, rationale, storyboard, plan, look = valid_design_documents(run_dir)
                submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)
                original = prototype_jobs._completion_boundary

                def crash(current):
                    if current == boundary:
                        raise KeyboardInterrupt("crash")
                    return original(current)

                with patch("omnipet.prototype_jobs._completion_boundary", side_effect=crash):
                    with self.assertRaises(KeyboardInterrupt):
                        hatch_prototype_run(run_dir, RecordingGenerator())
                load_workflow_v2(run_dir)
                manifest = read_json(run_dir / "imagegen-jobs.json")
                canonical = run_dir / "decoded/canonical.png"
                promoted = run_dir / "references/canonical-base.png"
                source = run_dir / "generated-sources/prototypes/canonical.png"
                self.assertEqual(manifest["jobs"][0]["status"], "complete")
                self.assertEqual(source.read_bytes(), canonical.read_bytes())
                self.assertEqual(source.read_bytes(), promoted.read_bytes())
                self.assertFalse((run_dir / "prototype-completion-publication.json").exists())

    def test_noncanonical_completion_recovers_output_and_manifest_together(self):
        from omnipet import prototype_jobs
        self.submit(); hatch_prototype_run(self.run_dir, RecordingGenerator())
        with patch("omnipet.prototype_jobs._completion_boundary", side_effect=lambda state: (_ for _ in ()).throw(KeyboardInterrupt()) if state == "output-installed" else None):
            with self.assertRaises(KeyboardInterrupt):
                hatch_prototype_run(self.run_dir, RecordingGenerator())
        prototype_jobs.recover_prototype_jobs(self.run_dir)
        manifest = read_json(self.run_dir / "imagegen-jobs.json")
        self.assertEqual(manifest["jobs"][1]["status"], "complete")
        self.assertEqual(
            (self.run_dir / "generated-sources/prototypes/cycle.png").read_bytes(),
            (self.run_dir / "decoded/prototypes/cycle.png").read_bytes(),
        )

    def test_tampered_completion_journal_is_retained_without_any_write(self):
        mutations = {
            "job-id": lambda journal, root: journal.update(job_id="cycle"),
            "source-absolute": lambda journal, root: journal.update(source_path=str(root / "sentinel")),
            "source-traversal": lambda journal, root: journal.update(source_path="../sentinel"),
            "source-wrong-job": lambda journal, root: journal.update(source_path="generated-sources/prototypes/cycle.png"),
            "output-path": lambda journal, root: journal.update(output_path="decoded/prototypes/cycle.png"),
            "canonical-null": lambda journal, root: journal.update(canonical_path=None),
            "canonical-wrong": lambda journal, root: journal.update(canonical_path="decoded/canonical.png"),
            "manifest-job": lambda journal, root: journal["manifest"]["jobs"][0].update(id="cycle"),
            "manifest-status": lambda journal, root: journal["manifest"]["jobs"][0].update(status="running"),
            "manifest-hash": lambda journal, root: journal["manifest"]["jobs"][0]["metadata"].update(source_sha256="0" * 64),
            "manifest-other-job": lambda journal, root: journal["manifest"]["jobs"][1].update(status="complete"),
            "extra-key": lambda journal, root: journal.update(extra=True),
            "missing-key": lambda journal, root: journal.pop("output_path"),
            "digest": lambda journal, root: journal.update(sha256="0" * 64),
        }
        operations = ("status", "generate", "load")
        for operation in operations:
            for mutation, mutate in mutations.items():
                with self.subTest(operation=operation, mutation=mutation), tempfile.TemporaryDirectory() as name:
                    root = Path(name).resolve(); reference = root / "portrait.png"
                    Image.new("RGB", (8, 8), "red").save(reference); run_dir = root / "run"
                    initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
                    submit_intake(run_dir, valid_intake(run_dir))
                    contract, rationale, storyboard, plan, look = valid_design_documents(run_dir)
                    submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)
                    with patch("omnipet.prototype_jobs._completion_boundary", side_effect=KeyboardInterrupt("crash")):
                        with self.assertRaises(KeyboardInterrupt):
                            hatch_prototype_run(run_dir, RecordingGenerator())
                    journal_path = run_dir / "prototype-completion-publication.json"
                    journal = read_json(journal_path)
                    mutate(journal, root)
                    journal_path.write_text(json.dumps(journal), encoding="utf-8")
                    sentinel = root / "sentinel"; sentinel.write_bytes(b"external")
                    source = run_dir / "generated-sources/prototypes/canonical.png"
                    before = {
                        "manifest": (run_dir / "imagegen-jobs.json").read_bytes(),
                        "workflow": (run_dir / "workflow.json").read_bytes(),
                        "journal": journal_path.read_bytes(),
                        "source": source.read_bytes(),
                    }
                    generator = RecordingGenerator()

                    if operation == "status":
                        with self.assertRaisesRegex(PrototypeJobError, "prototype job status is invalid"):
                            prototype_job_status(run_dir)
                    elif operation == "generate":
                        with self.assertRaisesRegex(PrototypeJobError, "prototype job validation failed"):
                            generate_next_prototype(run_dir, generator)
                    else:
                        with self.assertRaisesRegex(WorkflowError, "workflow is invalid"):
                            load_workflow_v2(run_dir)

                    self.assertEqual(generator.requests, [])
                    self.assertEqual(sentinel.read_bytes(), b"external")
                    self.assertEqual((run_dir / "imagegen-jobs.json").read_bytes(), before["manifest"])
                    self.assertEqual((run_dir / "workflow.json").read_bytes(), before["workflow"])
                    self.assertEqual(journal_path.read_bytes(), before["journal"])
                    self.assertEqual(source.read_bytes(), before["source"])
                    self.assertFalse((run_dir / "decoded/canonical.png").exists())
                    self.assertFalse((run_dir / "references/canonical-base.png").exists())

    def test_completion_recovery_rejects_coordinated_current_and_embedded_manifest_tamper(self):
        self.submit()
        with patch("omnipet.prototype_jobs._completion_boundary", side_effect=KeyboardInterrupt("crash")):
            with self.assertRaises(KeyboardInterrupt):
                hatch_prototype_run(self.run_dir, RecordingGenerator())
        manifest_path = self.run_dir / "imagegen-jobs.json"
        journal_path = self.run_dir / "prototype-completion-publication.json"
        manifest = read_json(manifest_path)
        journal = read_json(journal_path)
        manifest["jobs"][1]["depends_on"] = []
        journal["manifest"]["jobs"][1]["depends_on"] = []
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        before_manifest = manifest_path.read_bytes()
        before_journal = journal_path.read_bytes()

        with self.assertRaisesRegex(PrototypeJobError, "prototype job status is invalid"):
            prototype_job_status(self.run_dir)

        self.assertEqual(manifest_path.read_bytes(), before_manifest)
        self.assertEqual(journal_path.read_bytes(), before_journal)

    def test_completion_recovery_rejects_coordinated_extra_completion_metadata(self):
        self.submit()

        def crash(state):
            if state == "manifest-installed":
                raise KeyboardInterrupt("crash")

        with patch("omnipet.prototype_jobs._completion_boundary", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                hatch_prototype_run(self.run_dir, RecordingGenerator())
        manifest_path = self.run_dir / "imagegen-jobs.json"
        journal_path = self.run_dir / "prototype-completion-publication.json"
        manifest = read_json(manifest_path); journal = read_json(journal_path)
        manifest["jobs"][0]["metadata"]["unexpected"] = "tampered"
        journal["manifest"]["jobs"][0]["metadata"]["unexpected"] = "tampered"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        before_manifest = manifest_path.read_bytes(); before_journal = journal_path.read_bytes()

        with self.assertRaisesRegex(PrototypeJobError, "prototype job status is invalid"):
            prototype_job_status(self.run_dir)

        self.assertEqual(manifest_path.read_bytes(), before_manifest)
        self.assertEqual(journal_path.read_bytes(), before_journal)

    def test_completion_recovery_rejects_swapped_parent_without_writes(self):
        parents = (
            "decoded", "decoded/prototypes", "references",
            "generated-sources/prototypes",
        )
        for operation in ("status", "generate", "load"):
            for relative in parents:
                for replacement_kind in ("symlink", "directory"):
                    with self.subTest(
                        operation=operation, relative=relative,
                        replacement_kind=replacement_kind,
                    ), tempfile.TemporaryDirectory() as name:
                        root = Path(name).resolve(); reference = root / "portrait.png"
                        Image.new("RGB", (8, 8), "red").save(reference); run_dir = root / "run"
                        initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
                        submit_intake(run_dir, valid_intake(run_dir))
                        contract, rationale, storyboard, plan, look = valid_design_documents(run_dir)
                        submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)
                        with patch("omnipet.prototype_jobs._completion_boundary", side_effect=KeyboardInterrupt("crash")):
                            with self.assertRaises(KeyboardInterrupt):
                                hatch_prototype_run(run_dir, RecordingGenerator())
                        journal_path = run_dir / "prototype-completion-publication.json"
                        manifest_path = run_dir / "imagegen-jobs.json"
                        workflow_path = run_dir / "workflow.json"
                        source = run_dir / "generated-sources/prototypes/canonical.png"
                        source_bytes = source.read_bytes()
                        target = run_dir / relative
                        original = target.with_name(f"{target.name}-original")
                        target.rename(original)
                        external = root / f"external-{relative.replace('/', '-')}-{replacement_kind}"
                        external.mkdir()
                        (external / "sentinel").write_bytes(b"external")
                        if replacement_kind == "symlink":
                            target.symlink_to(external, target_is_directory=True)
                            replacement = external
                        else:
                            target.mkdir()
                            (target / "sentinel").write_bytes(b"external")
                            replacement = target
                        before = {
                            "journal": journal_path.read_bytes(),
                            "manifest": manifest_path.read_bytes(),
                            "workflow": workflow_path.read_bytes(),
                        }
                        generator = RecordingGenerator()

                        if operation == "status":
                            with self.assertRaisesRegex(PrototypeJobError, "prototype job status is invalid"):
                                prototype_job_status(run_dir)
                        elif operation == "generate":
                            with self.assertRaisesRegex(PrototypeJobError, "prototype job validation failed"):
                                generate_next_prototype(run_dir, generator)
                        else:
                            with self.assertRaisesRegex(WorkflowError, "workflow is invalid"):
                                load_workflow_v2(run_dir)

                        self.assertEqual(generator.requests, [])
                        self.assertEqual(
                            {path.name for path in replacement.iterdir()}, {"sentinel"}
                        )
                        self.assertEqual((replacement / "sentinel").read_bytes(), b"external")
                        self.assertEqual(journal_path.read_bytes(), before["journal"])
                        self.assertEqual(manifest_path.read_bytes(), before["manifest"])
                        self.assertEqual(workflow_path.read_bytes(), before["workflow"])
                        retained_source = (
                            original / "canonical.png"
                            if relative == "generated-sources/prototypes" else source
                        )
                        self.assertEqual(retained_source.read_bytes(), source_bytes)

    def test_failure_recovery_run_swap_after_validation_never_writes_replacement(self):
        from omnipet import prototype_jobs

        self.submit()
        with patch("omnipet.prototype_jobs._failure_boundary", side_effect=KeyboardInterrupt("crash")):
            with self.assertRaises(KeyboardInterrupt):
                generate_next_prototype(self.run_dir, RecordingGenerator(fail=True))
        journal = self.run_dir / "prototype-failure-publication.json"
        before_journal = journal.read_bytes()
        original_validate = prototype_jobs._validate_failure_recovery
        original_run = self.root / "run-original"

        def validate_then_swap(value, manifest):
            result = original_validate(value, manifest)
            self.run_dir.rename(original_run)
            self.run_dir.mkdir()
            (self.run_dir / "sentinel").write_bytes(b"external")
            return result

        with patch(
            "omnipet.prototype_jobs._validate_failure_recovery",
            side_effect=validate_then_swap,
        ):
            with self.assertRaises(PrototypeJobError):
                prototype_job_status(self.run_dir)

        self.assertEqual({path.name for path in self.run_dir.iterdir()}, {"sentinel"})
        self.assertEqual((self.run_dir / "sentinel").read_bytes(), b"external")
        self.assertEqual(
            (original_run / "prototype-failure-publication.json").read_bytes(),
            before_journal,
        )

    def test_canonical_tamper_blocks_dependent_before_provider(self):
        self.submit(); hatch_prototype_run(self.run_dir, RecordingGenerator())
        (self.run_dir / "references/canonical-base.png").write_bytes(b"tampered")
        generator = RecordingGenerator()
        with self.assertRaises(PrototypeJobError):
            hatch_prototype_run(self.run_dir, generator)
        self.assertEqual(generator.requests, [])

    def test_tampering_and_symlinks_are_rejected_before_provider(self):
        mutations = (
            "prompt", "plan", "contract", "rationale", "storyboard", "look",
            "intake", "run-metadata", "input", "output", "metadata", "undeclared",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve()
                reference = root / "portrait.png"
                Image.new("RGB", (8, 8), "red").save(reference)
                run_dir = root / "run"
                initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
                submit_intake(run_dir, valid_intake(run_dir))
                contract, rationale, storyboard, plan, look = valid_design_documents(run_dir)
                submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)
                manifest = read_json(run_dir / "imagegen-jobs.json")
                if mutation == "prompt":
                    (run_dir / manifest["jobs"][0]["prompt_file"]).write_text("changed", encoding="utf-8")
                elif mutation == "plan":
                    (run_dir / "design/prototype-plan.json").write_text("{}", encoding="utf-8")
                elif mutation in {"contract", "storyboard", "look", "intake", "run-metadata"}:
                    relative = {
                        "contract": "design/design-contract.json", "storyboard": "design/state-storyboard.json",
                        "look": "design/look-mechanics.json", "intake": "design/intake.json",
                        "run-metadata": "omnipet-run.json",
                    }[mutation]
                    value = read_json(run_dir / relative); value["tampered"] = True
                    (run_dir / relative).write_text(json.dumps(value), encoding="utf-8")
                elif mutation == "rationale":
                    (run_dir / "design/design-rationale.md").write_text("changed", encoding="utf-8")
                elif mutation == "input":
                    (run_dir / manifest["jobs"][0]["input_images"][0]["path"]).write_bytes(b"changed")
                elif mutation == "output":
                    manifest["jobs"][0]["output_path"] = "decoded/base.png"
                    (run_dir / "imagegen-jobs.json").write_text(json.dumps(manifest), encoding="utf-8")
                elif mutation == "metadata":
                    manifest["jobs"][0]["metadata"]["unexpected"] = "tampered"
                    (run_dir / "imagegen-jobs.json").write_text(json.dumps(manifest), encoding="utf-8")
                else:
                    manifest["jobs"].append({**manifest["jobs"][0], "id": "idle", "pose_id": "idle"})
                    (run_dir / "imagegen-jobs.json").write_text(json.dumps(manifest), encoding="utf-8")
                generator = RecordingGenerator()
                with self.assertRaises(PrototypeJobError):
                    generate_next_prototype(run_dir, generator)
                self.assertEqual(generator.requests, [])

        prompt = self.run_dir / "outside.md"
        prompt.write_text("outside", encoding="utf-8")
        self.submit()
        target = self.run_dir / "prompts/prototypes/canonical.md"
        target.unlink()
        target.symlink_to(prompt)
        generator = RecordingGenerator()
        with self.assertRaises(PrototypeJobError):
            generate_next_prototype(self.run_dir, generator)
        self.assertEqual(generator.requests, [])

        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            reference = root / "portrait.png"
            Image.new("RGB", (8, 8), "red").save(reference)
            run_dir = root / "run"
            initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
            submit_intake(run_dir, valid_intake(run_dir))
            contract, rationale, storyboard, plan, look = valid_design_documents(run_dir)
            submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)
            outside = root / "outside.png"
            Image.new("RGB", (8, 8), "black").save(outside)
            destination = run_dir / "generated-sources/prototypes/canonical.png"
            destination.symlink_to(outside)
            generator = RecordingGenerator()
            with self.assertRaises(PrototypeJobError):
                generate_next_prototype(run_dir, generator)
            self.assertEqual(generator.requests, [])

    def test_concurrent_generation_holds_lock_across_provider_call(self):
        self.submit()
        entered = threading.Event()

        class SlowGenerator(RecordingGenerator):
            def edit(inner_self, request):
                entered.set()
                return super().edit(request)

        generator = SlowGenerator()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(generate_next_prototype, self.run_dir, generator)
            self.assertTrue(entered.wait(timeout=2))
            second = executor.submit(generate_next_prototype, self.run_dir, generator)
            first.result(timeout=5)
            second.result(timeout=5)

        self.assertEqual([request.task for request in generator.requests], ["canonical"])
        self.assertEqual(prototype_job_status(self.run_dir)["ready_ids"], ["cycle"])

    def test_generation_lock_replacement_is_rejected(self):
        from omnipet import prototype_jobs
        self.submit()
        original = prototype_jobs.fcntl.flock
        replaced = False

        def replace_after_lock(descriptor, operation):
            nonlocal replaced
            result = original(descriptor, operation)
            if operation & prototype_jobs.fcntl.LOCK_EX and not replaced:
                lock = self.run_dir / ".prototype-generation.lock"
                lock.unlink(); lock.write_bytes(b"replacement"); replaced = True
            return result

        generator = RecordingGenerator()
        with patch("omnipet.prototype_jobs.fcntl.flock", side_effect=replace_after_lock):
            with self.assertRaises(PrototypeJobError):
                hatch_prototype_run(self.run_dir, generator)
        self.assertEqual(generator.requests, [])

    def test_pre_pin_directory_replacement_is_never_trusted(self):
        from omnipet import prototype_jobs

        relatives = (
            ".", "generated-sources/prototypes", "decoded", "decoded/prototypes",
            "references", "prompts/prototypes", "design",
        )
        for operation in ("generate", "status"):
            for relative in relatives:
                with self.subTest(operation=operation, relative=relative), tempfile.TemporaryDirectory() as name:
                    root = Path(name).resolve(); reference = root / "portrait.png"
                    Image.new("RGB", (8, 8), "red").save(reference); run_dir = root / "run"
                    initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
                    submit_intake(run_dir, valid_intake(run_dir))
                    contract, rationale, storyboard, plan, look = valid_design_documents(run_dir)
                    submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)
                    target = run_dir if relative == "." else run_dir / relative
                    original = root / "run-original" if relative == "." else target.with_name(f"{target.name}-original")
                    replacement = run_dir if relative == "." else target
                    swapped = False

                    def swap_before_pin():
                        nonlocal swapped
                        if swapped:
                            return
                        target.rename(original)
                        replacement.mkdir()
                        (replacement / "sentinel").write_bytes(b"external")
                        swapped = True

                    generator = RecordingGenerator()
                    with patch.object(
                        prototype_jobs, "_pre_pin_boundary", side_effect=swap_before_pin,
                        create=True,
                    ):
                        if operation == "generate":
                            with self.assertRaises(PrototypeJobError):
                                generate_next_prototype(run_dir, generator)
                        else:
                            with self.assertRaises(PrototypeJobError):
                                prototype_job_status(run_dir)

                    self.assertTrue(swapped)
                    self.assertEqual(generator.requests, [])
                    self.assertEqual({path.name for path in replacement.iterdir()}, {"sentinel"})
                    self.assertEqual((replacement / "sentinel").read_bytes(), b"external")

    def test_run_or_generated_directory_swap_during_provider_never_completes_job(self):
        for target in ("run", "generated"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve(); reference = root / "portrait.png"
                Image.new("RGB", (8, 8), "red").save(reference); run_dir = root / "run"
                initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
                submit_intake(run_dir, valid_intake(run_dir))
                contract, rationale, storyboard, plan, look = valid_design_documents(run_dir)
                submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)

                class SwapGenerator(RecordingGenerator):
                    def edit(inner, request):
                        result = super().edit(request)
                        if target == "run":
                            original = root / "run-original"; run_dir.rename(original); run_dir.mkdir()
                            (run_dir / "marker").write_bytes(b"external")
                        else:
                            generated = run_dir / "generated-sources/prototypes"
                            original = run_dir / "generated-sources/prototypes-original"
                            generated.rename(original); generated.mkdir()
                            (generated / "marker").write_bytes(b"external")
                        return result

                with self.assertRaises(BaseException):
                    hatch_prototype_run(run_dir, SwapGenerator())
                replacement = run_dir if target == "run" else run_dir / "generated-sources/prototypes"
                self.assertEqual((replacement / "marker").read_bytes(), b"external")


if __name__ == "__main__":
    unittest.main()
