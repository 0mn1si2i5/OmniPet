import concurrent.futures
import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from PIL import Image

from omnipet.design_pack import (
    DesignPackError, approve_design_pack, submit_design, submit_design_pack_summary, submit_intake,
    submit_prototype_evidence,
)
from omnipet.generation import GeneratedImage
from omnipet.prototype_jobs import generate_next_prototype
from omnipet.project import PetReference
from omnipet.release import initialize_design_run
from omnipet.workflow import load_workflow, load_workflow_v2, refresh_workflow
from tests.design_pack_fixtures import (
    STANDARD_STATES, changed, napoleon_design_documents, read_json, valid_design_documents,
    valid_design_pack_review, valid_intake, valid_prototype_evidence,
)


DESIGN_FILES = (
    "design-contract.json", "design-rationale.md", "state-storyboard.json",
    "prototype-plan.json", "look-mechanics.json",
)


class _PrototypeGenerator:
    def edit(self, request):
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (16, 16), (20, 40, 80, 255)).save(request.destination)
        content = request.destination.read_bytes()
        return GeneratedImage(
            request.destination, "image/png", hashlib.sha256(content).hexdigest(),
            16, 16, {"principal": "test-generator"},
        )


class DesignPackBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        reference = self.root / "portrait.png"
        Image.new("RGB", (12, 12), "navy").save(reference)
        self.run_dir = self.root / "run"
        initialize_design_run(self.run_dir, "sample-pet", (PetReference(reference, "identity"),))
        submit_intake(self.run_dir, valid_intake(self.run_dir))
        contract, rationale, storyboard, plan, look = valid_design_documents(self.run_dir)
        submit_design(
            self.run_dir, contract=contract, rationale=rationale, storyboard=storyboard,
            prototype_plan=plan, look_mechanics=look,
        )

    def complete_all(self):
        generator = _PrototypeGenerator()
        while generate_next_prototype(self.run_dir, generator) is not None:
            pass

    def submit_all_evidence(self, *, warning=None):
        for job in read_json(self.run_dir / "imagegen-jobs.json")["jobs"]:
            submit_prototype_evidence(
                self.run_dir,
                valid_prototype_evidence(
                    self.run_dir, job["id"], warning=warning if job["id"] == "cycle" else None,
                ),
            )

    def contact_sheet(self):
        path = self.root / "contact.png"
        Image.new("RGBA", (32, 16), (10, 20, 30, 255)).save(path)
        return path

    def ready_for_approval(self, *, documents=None):
        if documents is not None:
            raise AssertionError("documents must be supplied before setUp submission")
        self.complete_all()
        self.submit_all_evidence()
        contact = self.contact_sheet()
        submit_design_pack_summary(
            self.run_dir, contact, valid_design_pack_review(self.run_dir, contact)
        )

    def assert_evidence_fails(self, payload):
        with self.assertRaisesRegex(DesignPackError, "^prototype evidence submission failed$"):
            submit_prototype_evidence(self.run_dir, payload)

    def test_evidence_requires_complete_job_and_all_four_nonfailing_verdicts(self):
        self.complete_all()
        valid = valid_prototype_evidence(self.run_dir, "canonical")
        manifest_path = self.run_dir / "imagegen-jobs.json"
        complete_manifest = read_json(manifest_path)
        incomplete_manifest = json.loads(json.dumps(complete_manifest))
        incomplete_manifest["jobs"][0]["status"] = "pending"
        for key in ("source_path", "source_sha256", "completed_at", "principal"):
            incomplete_manifest["jobs"][0]["metadata"].pop(key)
        manifest_path.write_text(json.dumps(incomplete_manifest), encoding="utf-8")
        self.assert_evidence_fails(valid)
        manifest_path.write_text(json.dumps(complete_manifest), encoding="utf-8")
        for mutation in ("missing", "fail"):
            payload = changed(valid)
            if mutation == "missing":
                payload["verdicts"].pop("identity")
            else:
                payload["verdicts"]["identity"]["decision"] = "fail"
            with self.subTest(mutation=mutation):
                self.assert_evidence_fails(payload)

    def test_evidence_enforces_category_specific_reviewer_roles(self):
        self.complete_all()
        valid = valid_prototype_evidence(self.run_dir, "canonical")
        submit_prototype_evidence(self.run_dir, valid)

        for category, role in (
            ("structural", "production-agent"),
            ("view-semantic", "deterministic"),
            ("identity", "owner"),
            ("pose-purpose", "deterministic"),
        ):
            payload = valid_prototype_evidence(self.run_dir, "cycle")
            payload["verdicts"][category]["reviewer_role"] = role
            with self.subTest(category=category, role=role):
                self.assert_evidence_fails(payload)

        independent = valid_prototype_evidence(self.run_dir, "cycle")
        for category in ("view-semantic", "identity", "pose-purpose"):
            independent["verdicts"][category]["reviewer_role"] = "independent-visual-reviewer"
        self.assertEqual(submit_prototype_evidence(self.run_dir, independent).state, "prototyping")

    def test_evidence_rejects_stale_hash_path_revision_undeclared_and_unknown_fields(self):
        self.complete_all()
        valid = valid_prototype_evidence(self.run_dir, "canonical")
        invalid = (
            changed(valid, artifact={**valid["artifact"], "sha256": "0" * 64}),
            changed(valid, artifact={**valid["artifact"], "path": "decoded/prototypes/canonical.png"}),
            changed(valid, design_revision="design-0002"),
            changed(valid, pose_id="undeclared"),
            changed(valid, extra=True),
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assert_evidence_fails(payload)

    def test_warning_must_be_disclosed_and_identical_duplicate_is_idempotent(self):
        self.complete_all()
        payload = valid_prototype_evidence(self.run_dir, "canonical", warning="Minor edge softness.")
        bad = changed(payload, accepted_warnings=[])
        self.assert_evidence_fails(bad)
        first = submit_prototype_evidence(self.run_dir, payload)
        before = (self.run_dir / "qa/design-pack/prototypes/canonical.json").read_bytes()
        second = submit_prototype_evidence(self.run_dir, payload)
        self.assertEqual((first.state, second.state), ("prototyping", "prototyping"))
        self.assertEqual((self.run_dir / "qa/design-pack/prototypes/canonical.json").read_bytes(), before)
        changed_payload = changed(payload, accepted_warnings=["Changed warning."])
        self.assert_evidence_fails(changed_payload)

    def test_evidence_rejects_symlinked_artifact(self):
        self.complete_all()
        payload = valid_prototype_evidence(self.run_dir, "cycle")
        artifact = self.run_dir / payload["artifact"]["path"]
        outside = self.root / "outside.png"
        outside.write_bytes(artifact.read_bytes())
        artifact.unlink(); artifact.symlink_to(outside)
        self.assert_evidence_fails(payload)

    def test_summary_requires_every_declared_prototype_review(self):
        self.complete_all()
        submit_prototype_evidence(self.run_dir, valid_prototype_evidence(self.run_dir, "canonical"))
        contact = self.contact_sheet()
        review = valid_design_pack_review(self.run_dir, contact)
        with self.assertRaisesRegex(DesignPackError, "^design pack summary submission failed$"):
            submit_design_pack_summary(self.run_dir, contact, review)

    def test_summary_rejects_warning_budget_call_and_evidence_mismatches(self):
        self.complete_all(); self.submit_all_evidence(warning="Minor edge softness.")
        contact = self.contact_sheet(); valid = valid_design_pack_review(self.run_dir, contact)
        invalid = (
            changed(valid, accepted_warnings=[]),
            changed(valid, budget_authorized_usd=99),
            changed(valid, expected_provider_calls=99),
            changed(valid, evidence={**valid["evidence"], "contact_sheet_sha256": "0" * 64}),
            changed(valid, decision="warning"),
            changed(valid, extra=True),
        )
        for review in invalid:
            with self.subTest(review=review), self.assertRaisesRegex(DesignPackError, "^design pack summary submission failed$"):
                submit_design_pack_summary(self.run_dir, contact, review)

    def test_simple_pack_is_hash_bound_sorted_and_transitions(self):
        self.complete_all(); self.submit_all_evidence(warning="Minor edge softness.")
        contact = self.contact_sheet(); review = valid_design_pack_review(self.run_dir, contact)
        state = submit_design_pack_summary(self.run_dir, contact, review)
        self.assertEqual(state.state, "awaiting_design_pack_approval")
        self.assertEqual(load_workflow_v2(self.run_dir).state, state.state)
        self.assertEqual((self.run_dir / "qa/design-pack/contact-sheet.png").read_bytes(), contact.read_bytes())
        self.assertEqual(read_json(self.run_dir / "qa/design-pack/review.json"), review)
        manifest = read_json(self.run_dir / "design/design-pack.json")
        paths = [item["path"] for item in manifest["artifacts"]]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn("design/prototype-jobs-approved.json", paths)
        self.assertNotIn("imagegen-jobs.json", paths)
        self.assertIn("prompts/prototypes/canonical.md", paths)
        self.assertEqual(manifest["accepted_warnings"], ["Minor edge softness."])
        for item in manifest["artifacts"]:
            self.assertEqual(item["sha256"], hashlib.sha256((self.run_dir / item["path"]).read_bytes()).hexdigest())

    def test_duplicate_warning_text_is_aggregated_once_in_plan_order(self):
        warning = "Shared minor edge softness."
        self.complete_all()
        for job in read_json(self.run_dir / "imagegen-jobs.json")["jobs"]:
            submit_prototype_evidence(
                self.run_dir,
                valid_prototype_evidence(self.run_dir, job["id"], warning=warning),
            )
        contact = self.contact_sheet()
        review = valid_design_pack_review(self.run_dir, contact)

        state = submit_design_pack_summary(self.run_dir, contact, review)

        self.assertEqual(state.state, "awaiting_design_pack_approval")
        self.assertEqual(review["accepted_warnings"], [warning])
        self.assertEqual(
            read_json(self.run_dir / "qa/design-pack/review.json")["accepted_warnings"],
            [warning],
        )
        self.assertEqual(
            read_json(self.run_dir / "design/design-pack.json")["accepted_warnings"],
            [warning],
        )

    def test_summary_transition_failure_rolls_back_all_outputs(self):
        self.complete_all(); self.submit_all_evidence()
        contact = self.contact_sheet(); review = valid_design_pack_review(self.run_dir, contact)
        before = (self.run_dir / "workflow.json").read_bytes()
        with patch("omnipet.design_pack._transition_workflow_pinned", side_effect=OSError("failure")):
            with self.assertRaisesRegex(DesignPackError, "^design pack summary submission failed$"):
                submit_design_pack_summary(self.run_dir, contact, review)
        self.assertEqual((self.run_dir / "workflow.json").read_bytes(), before)
        for relative in (
            "qa/design-pack/contact-sheet.png", "qa/design-pack/review.json",
            "design/design-pack.json",
        ):
            self.assertFalse((self.run_dir / relative).exists())

    def test_complex_pack_binds_every_declared_prototype(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve(); reference = root / "portrait.png"
            Image.new("RGB", (12, 12), "navy").save(reference); run_dir = root / "run"
            initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
            submit_intake(run_dir, valid_intake(run_dir)); documents = napoleon_design_documents(run_dir)
            submit_design(run_dir, contract=documents[0], rationale=documents[1], storyboard=documents[2], prototype_plan=documents[3], look_mechanics=documents[4])
            generator = _PrototypeGenerator()
            while generate_next_prototype(run_dir, generator) is not None: pass
            for job in read_json(run_dir / "imagegen-jobs.json")["jobs"]:
                submit_prototype_evidence(run_dir, valid_prototype_evidence(run_dir, job["id"]))
            contact = root / "contact.png"; Image.new("RGBA", (32, 16), "navy").save(contact)
            submit_design_pack_summary(run_dir, contact, valid_design_pack_review(run_dir, contact))
            paths = {item["path"] for item in read_json(run_dir / "design/design-pack.json")["artifacts"]}
            for job in read_json(run_dir / "imagegen-jobs.json")["jobs"]:
                self.assertIn(job["output_path"], paths)
                self.assertIn(f"qa/design-pack/prototypes/{job['id']}.json", paths)

    def test_owner_approval_materializes_exactly_nine_bound_standard_jobs_without_provider(self):
        self.ready_for_approval()
        prototype_ids = [job["id"] for job in read_json(self.run_dir / "imagegen-jobs.json")["jobs"]]
        self.assertEqual(prototype_ids, ["canonical", "cycle"])

        with patch("omnipet.release.OpenAIImageGenerator", side_effect=AssertionError("provider called")):
            state = approve_design_pack(self.run_dir, "owner-1", note="Approved for rows.")

        self.assertEqual(state.state, "producing_standard_rows")
        jobs = read_json(self.run_dir / "imagegen-jobs.json")["jobs"]
        manifest = read_json(self.run_dir / "imagegen-jobs.json")
        self.assertEqual(set(manifest), {
            "schema_version", "manifest_kind", "design_revision",
            "design_pack_sha256", "jobs",
        })
        self.assertEqual(manifest["manifest_kind"], "standard-rows")
        self.assertEqual([job["id"] for job in jobs], list(STANDARD_STATES))
        self.assertTrue(all(job["status"] == "pending" for job in jobs))
        self.assertEqual(jobs[2]["depends_on"], [])
        pack_sha = hashlib.sha256((self.run_dir / "design/design-pack.json").read_bytes()).hexdigest()
        for job in jobs:
            self.assertEqual(job["design_revision"], "design-0001")
            self.assertEqual(job["design_pack_sha256"], pack_sha)
            self.assertEqual(job["canvas"], {"aspect_ratio": "21:9", "image_size": "2K"})
            self.assertEqual(job["metadata"]["prompt_sha256"], hashlib.sha256((self.run_dir / job["prompt_file"]).read_bytes()).hexdigest())
            self.assertEqual(job["metadata"]["retry_prompt_sha256"], hashlib.sha256((self.run_dir / job["retry_prompt_file"]).read_bytes()).hexdigest())
            for item in job["input_images"]:
                self.assertEqual(item["sha256"], hashlib.sha256((self.run_dir / item["path"]).read_bytes()).hexdigest())
        approval = read_json(self.run_dir / "qa/approvals-v2.json")
        self.assertEqual(approval["schema_version"], 2)
        self.assertEqual(approval["approvals"][0]["stage"], "design-pack")
        self.assertEqual(approval["approvals"][0]["owner_principal_id"], "owner-1")
        self.assertEqual(approval["approvals"][0]["note"], "Approved for rows.")
        self.assertIn("design/design-pack.json", {item["path"] for item in approval["approvals"][0]["artifacts"]})
        pack = read_json(self.run_dir / "design/design-pack.json")
        for item in pack["artifacts"]:
            self.assertEqual(
                item["sha256"],
                hashlib.sha256((self.run_dir / item["path"]).read_bytes()).hexdigest(),
            )

    def test_approval_rejects_tampered_prototype_manifest_snapshot(self):
        self.ready_for_approval()
        snapshot = self.run_dir / "design/prototype-jobs-approved.json"
        snapshot.write_bytes(snapshot.read_bytes() + b" ")

        with self.assertRaisesRegex(DesignPackError, "^design pack approval failed$"):
            approve_design_pack(self.run_dir, "owner-1")

        self.assertFalse((self.run_dir / "qa/approvals-v2.json").exists())

    def test_v2_standard_row_action_executes_exactly_one_row(self):
        self.ready_for_approval()
        approve_design_pack(self.run_dir, "owner-1")
        from omnipet.actions import build_action_contract
        from omnipet.release import generate_next_standard_row_v2

        contract = build_action_contract(self.run_dir, "sample-pet")
        action = next(item for item in contract["actions"] if item["kind"] == "hatch-standard-row")

        class RowGenerator:
            calls = 0

            def edit(self, request):
                self.calls += 1
                request.destination.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (1536, 1024), "#00FF00").save(request.destination)
                content = request.destination.read_bytes()
                return GeneratedImage(
                    request.destination, "image/png", hashlib.sha256(content).hexdigest(),
                    1536, 1024, {"principal": "row-generator"},
                )

        generator = RowGenerator()
        project = SimpleNamespace(pet_id="sample-pet")
        with patch("omnipet.release._qa_row", return_value=None):
            state = generate_next_standard_row_v2(
                project, self.run_dir, lambda _project: generator,
                action_id=action["id"], run_revision=contract["run_revision"],
            )

        self.assertEqual(state.state, "producing_standard_rows")
        self.assertEqual(generator.calls, 1)
        statuses = [job["status"] for job in read_json(self.run_dir / "imagegen-jobs.json")["jobs"]]
        self.assertEqual(statuses.count("complete"), 1)
        self.assertEqual(statuses.count("pending"), 8)

    def test_v2_standard_row_revalidates_after_dispatch_race_before_begin(self):
        self.ready_for_approval()
        approve_design_pack(self.run_dir, "owner-1")
        from omnipet.actions import ActionError, build_action_contract
        from omnipet.release import generate_next_standard_row_v2
        from omnipet.workflow import _workflow_lock

        contract = build_action_contract(self.run_dir, "sample-pet")
        action = next(item for item in contract["actions"] if item["kind"] == "hatch-standard-row")
        manifest_path = self.run_dir / "imagegen-jobs.json"
        raced = threading.Event()

        def mutate_between_dispatch_and_transaction():
            def mutate():
                with _workflow_lock(self.run_dir):
                    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
                raced.set()

            thread = threading.Thread(target=mutate)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())

        factory_calls = []
        with patch(
            "omnipet.release._standard_row_dispatch_boundary",
            side_effect=mutate_between_dispatch_and_transaction,
        ):
            with self.assertRaises(ActionError):
                generate_next_standard_row_v2(
                    SimpleNamespace(pet_id="sample-pet"), self.run_dir,
                    lambda _project: factory_calls.append(True),
                    action_id=action["id"], run_revision=contract["run_revision"],
                )

        self.assertTrue(raced.is_set())
        after_race = manifest_path.read_bytes()
        self.assertEqual(factory_calls, [])
        self.assertTrue(after_race.endswith(b" "))
        self.assertTrue(all(
            job["status"] == "pending" for job in json.loads(after_race)["jobs"]
        ))

    def test_standard_prompts_use_contract_grammar_asymmetry_prohibitions_and_pose_anchors(self):
        self.ready_for_approval()
        approve_design_pack(self.run_dir, "owner-1")
        prompt = (self.run_dir / "prompts/rows/running-left.md").read_text(encoding="utf-8")
        self.assertIn("running-left start", prompt)
        self.assertIn("running-left key", prompt)
        self.assertIn("Do not mirror asymmetric designs.", prompt)
        self.assertIn("Approved pose anchors", prompt)
        self.assertIn("decoded/canonical.png", prompt)
        retry = (self.run_dir / "prompts/row-retries/running-left.md").read_text(encoding="utf-8")
        self.assertIn("Approved Design Pack", retry)

    def test_approval_rejects_wrong_state_invalid_owner_and_bound_artifact_tamper(self):
        with self.assertRaisesRegex(DesignPackError, "^design pack approval failed$"):
            approve_design_pack(self.run_dir, "owner-1")
        self.ready_for_approval()
        for owner in ("", "   "):
            with self.subTest(owner=owner), self.assertRaisesRegex(DesignPackError, "^design pack approval failed$"):
                approve_design_pack(self.run_dir, owner)
        (self.run_dir / "design/state-storyboard.json").write_bytes(b"{}\n")
        with self.assertRaisesRegex(DesignPackError, "^design pack approval failed$"):
            approve_design_pack(self.run_dir, "owner-1")
        self.assertFalse((self.run_dir / "qa/approvals-v2.json").exists())

    def test_phase1_approval_does_not_satisfy_design_pack_gate(self):
        self.ready_for_approval()
        (self.run_dir / "qa/approvals.json").write_text(
            json.dumps({"schema_version": 1, "approvals": []}), encoding="utf-8"
        )
        self.assertEqual(load_workflow_v2(self.run_dir).state, "awaiting_design_pack_approval")
        approve_design_pack(self.run_dir, "owner-1")
        self.assertTrue((self.run_dir / "qa/approvals.json").is_file())
        self.assertTrue((self.run_dir / "qa/approvals-v2.json").is_file())

    def test_approval_rollback_restores_exact_files(self):
        self.ready_for_approval()
        targets = (self.run_dir / "imagegen-jobs.json", self.run_dir / "workflow.json")
        before = {path: path.read_bytes() for path in targets}
        with patch("omnipet.design_pack._transition_workflow_pinned", side_effect=OSError("failure")):
            with self.assertRaisesRegex(DesignPackError, "^design pack approval failed$"):
                approve_design_pack(self.run_dir, "owner-1")
        self.assertEqual({path: path.read_bytes() for path in targets}, before)
        self.assertFalse((self.run_dir / "qa/approvals-v2.json").exists())
        self.assertFalse((self.run_dir / "prompts/rows/idle.md").exists())

    def test_artifact_mutation_makes_approval_stale_and_blocks_row_execution(self):
        self.ready_for_approval()
        approve_design_pack(self.run_dir, "owner-1")
        (self.run_dir / "design/design-contract.json").write_bytes(b"{}\n")
        from omnipet.design_pack import _validate_current_design_pack_approval
        with self.assertRaisesRegex(DesignPackError, "^design pack approval is invalid$"):
            _validate_current_design_pack_approval(self.run_dir)
        from omnipet.release import _generate_standard_rows
        with self.assertRaisesRegex(ValueError, "design pack approval is invalid"):
            _generate_standard_rows(None, self.run_dir, object())

    def test_phase2_manifest_marker_deletion_and_job_binding_tamper_call_no_provider(self):
        self.ready_for_approval()
        approve_design_pack(self.run_dir, "owner-1")
        manifest_path = self.run_dir / "imagegen-jobs.json"
        original = read_json(manifest_path)
        mutations = (
            lambda value: value.pop("manifest_kind"),
            lambda value: value.pop("design_pack_sha256"),
            lambda value: value["jobs"][0].pop("design_revision"),
            lambda value: value["jobs"][0].update(design_pack_sha256="0" * 64),
            lambda value: value["jobs"][0]["metadata"].update(prompt_sha256="0" * 64),
            lambda value: value["jobs"][0]["input_images"][0].update(sha256="0" * 64),
            lambda value: value["jobs"][0].update(kind="prototype"),
        )
        from omnipet.release import _generate_standard_rows

        class NeverProvider:
            calls = 0

            def edit(self, request):
                self.calls += 1
                raise AssertionError("provider called")

        for mutate in mutations:
            manifest = changed(original)
            mutate(manifest)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            provider = NeverProvider()
            with self.subTest(mutate=mutate), self.assertRaisesRegex(
                ValueError, "design pack approval is invalid"
            ):
                _generate_standard_rows(None, self.run_dir, provider)
            self.assertEqual(provider.calls, 0)

    def test_simple_design_rows_bind_only_shared_canonical_and_motion_anchors(self):
        self.ready_for_approval()
        approve_design_pack(self.run_dir, "owner-1")
        jobs = read_json(self.run_dir / "imagegen-jobs.json")["jobs"]
        expected = {"decoded/canonical.png", "decoded/prototypes/cycle.png"}
        for job in jobs:
            anchors = {
                item["path"] for item in job["input_images"]
                if item["role"].startswith("approved")
            }
            self.assertEqual(anchors, expected, job["id"])

    def test_complex_design_rows_bind_only_state_relevant_pose_anchors(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            reference = root / "portrait.png"
            Image.new("RGB", (12, 12), "navy").save(reference)
            run_dir = root / "run"
            initialize_design_run(run_dir, "sample-pet", (PetReference(reference, "identity"),))
            submit_intake(run_dir, valid_intake(run_dir))
            documents = napoleon_design_documents(run_dir)
            documents[0]["state_grammar"]["idle"]["key_pose"] = "right"
            submit_design(
                run_dir, contract=documents[0], rationale=documents[1],
                storyboard=documents[2], prototype_plan=documents[3],
                look_mechanics=documents[4],
            )
            generator = _PrototypeGenerator()
            while generate_next_prototype(run_dir, generator) is not None:
                pass
            for job in read_json(run_dir / "imagegen-jobs.json")["jobs"]:
                submit_prototype_evidence(run_dir, valid_prototype_evidence(run_dir, job["id"]))
            contact = root / "contact.png"
            Image.new("RGBA", (32, 16), "navy").save(contact)
            submit_design_pack_summary(run_dir, contact, valid_design_pack_review(run_dir, contact))
            prototype_outputs = {
                job["output_path"] for job in read_json(run_dir / "imagegen-jobs.json")["jobs"]
            }
            approve_design_pack(run_dir, "owner-1")

            shared = {"decoded/canonical.png", "decoded/prototypes/cycle.png"}
            extras = {
                "idle": {"decoded/prototypes/right.png"},
                "running-right": {"decoded/prototypes/right.png"},
                "running-left": {"decoded/prototypes/left.png", "decoded/prototypes/view-rear.png"},
                "waving": set(),
                "jumping": {
                    "decoded/prototypes/anticipation.png",
                    "decoded/prototypes/airborne.png",
                    "decoded/prototypes/return.png",
                    "decoded/prototypes/attachment.png",
                    "decoded/prototypes/view-overhead.png",
                },
                "failed": {"decoded/prototypes/extreme-failed.png"},
                "waiting": set(),
                "running": {"decoded/prototypes/attachment.png"},
                "review": {"decoded/prototypes/extreme-review.png", "decoded/prototypes/view-rear.png"},
            }
            for job in read_json(run_dir / "imagegen-jobs.json")["jobs"]:
                expected = shared | extras[job["id"]]
                anchors = {
                    item["path"] for item in job["input_images"]
                    if item["role"].startswith("approved")
                }
                self.assertEqual(anchors, expected, job["id"])
                prompt = (run_dir / job["prompt_file"]).read_text(encoding="utf-8")
                for path in expected:
                    self.assertIn(path, prompt)
                for path in prototype_outputs - expected:
                    self.assertNotIn(path, prompt, f"{job['id']} includes unrelated {path}")


class SubmitDesignTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.reference = self.root / "portrait.png"
        self.reference.write_bytes(b"portrait")
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

    def assert_design_fails(self, documents):
        before = (self.run_dir / "workflow.json").read_bytes()
        with self.assertRaisesRegex(DesignPackError, "^design submission failed$") as raised:
            self.submit(documents)
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual((self.run_dir / "workflow.json").read_bytes(), before)
        self.assertFalse(any((self.run_dir / "design" / name).exists() for name in DESIGN_FILES))

    def test_minimum_design_publishes_five_files_and_prototype_only_jobs_without_provider(self):
        with patch("omnipet.release.OpenAIImageGenerator", side_effect=AssertionError("provider called")):
            state = self.submit()
        self.assertEqual(state.state, "prototyping")
        self.assertEqual(set(DESIGN_FILES), {
            path.name for path in (self.run_dir / "design").iterdir()
            if path.name != "intake.json"
        })
        manifest = read_json(self.run_dir / "imagegen-jobs.json")
        self.assertEqual([job["id"] for job in manifest["jobs"]], ["canonical", "cycle"])
        self.assertTrue(all(job["kind"] == "prototype" for job in manifest["jobs"]))

    def test_napoleon_like_compound_design_satisfies_expanded_matrix(self):
        state = self.submit(napoleon_design_documents(self.run_dir))
        self.assertEqual(state.state, "prototyping")

    def test_missing_or_derived_direction_anchor_under_unsafe_mirror_is_rejected(self):
        for mutation in ("missing", "derived"):
            documents = napoleon_design_documents(self.run_dir)
            contract, rationale, storyboard, plan, look = documents
            if mutation == "missing":
                contract["prototype_requirements"] = [item for item in contract["prototype_requirements"] if item["evidence_kind"] != "screen-right-anchor"]
                plan["prototypes"] = [item for item in plan["prototypes"] if item["evidence_kind"] != "screen-right-anchor"]
                plan["estimated_provider_calls"] -= 1
            else:
                next(item for item in plan["prototypes"] if item["evidence_kind"] == "screen-right-anchor")["generation_method"] = "derive"
            with self.subTest(mutation=mutation):
                self.assert_design_fails(documents)

    def test_multiple_state_and_view_risks_require_distinct_scoped_anchors(self):
        for prefix in ("state-extreme-anchor:", "unsupported-view-anchor:"):
            documents = napoleon_design_documents(self.run_dir)
            contract, rationale, storyboard, plan, look = documents
            matching = [item for item in plan["prototypes"] if item["evidence_kind"].startswith(prefix)]
            removed = matching[-1]
            contract["prototype_requirements"] = [item for item in contract["prototype_requirements"] if item["pose_id"] != removed["pose_id"]]
            plan["prototypes"] = [item for item in plan["prototypes"] if item["pose_id"] != removed["pose_id"]]
            plan["estimated_provider_calls"] -= 1
            with self.subTest(prefix=prefix):
                self.assert_design_fails(documents)

    def test_bad_typed_exception_and_unsafe_mirror_exception_are_rejected(self):
        for exception in (
            {"risk": "state_extreme", "omitted_requirement_id": "state-extreme-anchor:review", "rationale": "Use substitute", "substitute_pose_ids": ["missing"], "reviewer_principal_id": "reviewer-1"},
            {"risk": "unsafe_mirror", "omitted_requirement_id": "screen-right-anchor", "rationale": "Mirror anyway", "substitute_pose_ids": ["canonical"], "reviewer_principal_id": "reviewer-1"},
        ):
            documents = napoleon_design_documents(self.run_dir)
            documents[3]["exceptions"] = [exception]
            with self.subTest(exception=exception):
                self.assert_design_fails(documents)

    def test_typed_exception_substitute_must_cover_the_omitted_purpose(self):
        documents = napoleon_design_documents(self.run_dir)
        contract, rationale, storyboard, plan, look = documents
        contract["prototype_requirements"] = [item for item in contract["prototype_requirements"] if item["evidence_kind"] != "state-extreme-anchor:review"]
        plan["prototypes"] = [item for item in plan["prototypes"] if item["evidence_kind"] != "state-extreme-anchor:review"]
        substitute = next(item for item in plan["prototypes"] if item["pose_id"] == "extreme-failed")
        substitute["purpose"] = "Explanatory prose unrelated to requirement identifiers."
        next(item for item in contract["prototype_requirements"] if item["pose_id"] == "extreme-failed")["purpose"] = substitute["purpose"]
        substitute["covers_requirements"].append("state-extreme-anchor:review")
        plan["estimated_provider_calls"] -= 1
        plan["exceptions"] = [{
            "risk": "state_extreme",
            "omitted_requirement_id": "state-extreme-anchor:review",
            "rationale": "The failed extreme also proves the review silhouette.",
            "substitute_pose_ids": ["extreme-failed"],
            "reviewer_principal_id": "reviewer-1",
        }]
        self.assertEqual(self.submit(documents).state, "prototyping")

    def test_closed_shapes_identity_rationale_and_plan_counts_are_enforced(self):
        invalid = []
        for index in (0, 2, 3, 4):
            documents = list(valid_design_documents(self.run_dir))
            documents[index]["extra"] = True
            invalid.append(tuple(documents))
        documents = list(valid_design_documents(self.run_dir)); documents[2]["pet_id"] = "other"; invalid.append(tuple(documents))
        documents = list(valid_design_documents(self.run_dir)); documents[3]["estimated_provider_calls"] = 1; invalid.append(tuple(documents))
        documents = list(valid_design_documents(self.run_dir)); documents[1] = "api_key=private-value"; invalid.append(tuple(documents))
        documents = list(valid_design_documents(self.run_dir)); documents[1] = "x" * 65537; invalid.append(tuple(documents))
        for documents in invalid:
            with self.subTest(kind=next((i for i, value in enumerate(documents) if value != self.documents[i]), None)):
                self.assert_design_fails(documents)

    def test_contract_and_plan_requirements_have_exact_runtime_parity(self):
        documents = list(valid_design_documents(self.run_dir))
        documents[3]["prototypes"][1]["purpose"] = "different purpose"
        self.assert_design_fails(tuple(documents))

    def test_evidence_kinds_are_unique(self):
        documents = list(valid_design_documents(self.run_dir))
        requirement = {"pose_id": "cycle-copy", "purpose": "duplicate motion", "evidence_kind": "motion-cycle"}
        documents[0]["prototype_requirements"].append(requirement)
        documents[3]["prototypes"].append({**requirement, "covers_requirements": ["motion-cycle"], "generation_method": "generate", "reference_roles": []})
        documents[3]["estimated_provider_calls"] += 1
        self.assert_design_fails(tuple(documents))

    def test_reserved_standard_and_direction_pose_ids_are_rejected_before_publication(self):
        for pose_id in ("base", "idle", "running-right", "look-cardinals", "look-row-9", "look-row-10"):
            documents = valid_design_documents(self.run_dir)
            documents[0]["prototype_requirements"][1]["pose_id"] = pose_id
            documents[3]["prototypes"][1]["pose_id"] = pose_id
            with self.subTest(pose_id=pose_id):
                self.assert_design_fails(documents)

    def test_plan_a_schema_and_runtime_allow_generate_only(self):
        for method in ("derive", "reuse"):
            documents = valid_design_documents(self.run_dir)
            documents[3]["prototypes"][1]["generation_method"] = method
            documents[3]["estimated_provider_calls"] = 1
            with self.subTest(method=method):
                self.assert_design_fails(documents)

    def test_contract_defaults_and_owner_decisions_exactly_match_intake_order(self):
        intake = read_json(self.run_dir / "design/intake.json")
        intake["accepted_defaults"] = ["first default", "second default"]
        intake["owner_decisions"] = ["first decision", "second decision"]
        (self.run_dir / "design/intake.json").write_text(json.dumps(intake), encoding="utf-8")
        for field in ("accepted_defaults", "owner_decisions"):
            for replacement in (
                [],
                list(reversed(intake[field])),
                [*intake[field], "invented value"],
            ):
                documents = list(valid_design_documents(self.run_dir))
                documents[0][field] = replacement
                with self.subTest(field=field, replacement=replacement):
                    self.assert_design_fails(tuple(documents))

    def test_exception_coverage_uses_exact_typed_ids_not_purpose_text(self):
        documents = napoleon_design_documents(self.run_dir)
        contract, rationale, storyboard, plan, look = documents
        contract["prototype_requirements"] = [item for item in contract["prototype_requirements"] if item["evidence_kind"] != "state-extreme-anchor:review"]
        plan["prototypes"] = [item for item in plan["prototypes"] if item["evidence_kind"] != "state-extreme-anchor:review"]
        substitute = next(item for item in plan["prototypes"] if item["pose_id"] == "extreme-failed")
        substitute["purpose"] = "This prose says state-extreme-anchor:review but is not typed coverage."
        plan["estimated_provider_calls"] -= 1
        plan["exceptions"] = [{
            "risk": "state_extreme",
            "omitted_requirement_id": "state-extreme-anchor:review",
            "rationale": "Use the failed pose.",
            "substitute_pose_ids": ["extreme-failed"],
            "reviewer_principal_id": "reviewer-1",
        }]
        self.assert_design_fails(documents)

    def test_multiple_exceptions_for_distinct_instances_of_one_risk_are_allowed(self):
        documents = napoleon_design_documents(self.run_dir)
        contract, rationale, storyboard, plan, look = documents
        omitted = {"state-extreme-anchor:failed", "state-extreme-anchor:review"}
        removed_pose_ids = {
            item["pose_id"] for item in contract["prototype_requirements"]
            if item["evidence_kind"] in omitted
        }
        contract["prototype_requirements"] = [item for item in contract["prototype_requirements"] if item["evidence_kind"] not in omitted]
        plan["prototypes"] = [item for item in plan["prototypes"] if item["pose_id"] not in removed_pose_ids]
        substitute = next(item for item in plan["prototypes"] if item["pose_id"] == "canonical")
        substitute["covers_requirements"].extend(sorted(omitted))
        plan["estimated_provider_calls"] -= 2
        plan["exceptions"] = [
            {
                "risk": "state_extreme",
                "omitted_requirement_id": requirement_id,
                "rationale": "The canonical pose is approved as substitute evidence.",
                "substitute_pose_ids": ["canonical"],
                "reviewer_principal_id": "reviewer-1",
            }
            for requirement_id in sorted(omitted)
        ]
        self.assertEqual(self.submit(documents).state, "prototyping")

    def test_unknown_or_undeclared_requirement_coverage_is_rejected(self):
        for requirement_id in (
            "arbitrary-proof",
            "state-extreme-anchor:BAD VALUE",
            "state-extreme-anchor:idle",
            "unsupported-view-anchor:side",
            "stable-attachment",
        ):
            documents = list(valid_design_documents(self.run_dir))
            documents[3]["prototypes"][0]["covers_requirements"].append(requirement_id)
            with self.subTest(requirement_id=requirement_id):
                self.assert_design_fails(tuple(documents))

    def test_primary_evidence_kind_must_be_in_coverage_for_base_and_scoped_requirements(self):
        for fixture, pose_id in (
            (valid_design_documents, "canonical"),
            (napoleon_design_documents, "extreme-failed"),
            (napoleon_design_documents, "view-rear"),
        ):
            documents = fixture(self.run_dir)
            prototype = next(item for item in documents[3]["prototypes"] if item["pose_id"] == pose_id)
            prototype["covers_requirements"].remove(prototype["evidence_kind"])
            with self.subTest(pose_id=pose_id):
                self.assert_design_fails(documents)

    def test_runtime_enforces_schema_requirement_list_and_text_constraints(self):
        invalid_documents = []
        documents = list(valid_design_documents(self.run_dir))
        documents[3]["prototypes"][0]["covers_requirements"].append("animation-ready-canonical")
        invalid_documents.append(tuple(documents))
        documents = list(napoleon_design_documents(self.run_dir))
        documents[3]["exceptions"] = [{
            "risk": "state_extreme",
            "omitted_requirement_id": "state-extreme-anchor:review",
            "rationale": "approved substitute",
            "substitute_pose_ids": ["canonical", "canonical"],
            "reviewer_principal_id": "reviewer-1",
        }]
        invalid_documents.append(tuple(documents))
        documents = list(valid_design_documents(self.run_dir))
        documents[3]["prototypes"][0]["purpose"] = "x" * 10_001
        invalid_documents.append(tuple(documents))
        documents = list(napoleon_design_documents(self.run_dir))
        prototype = next(item for item in documents[3]["prototypes"] if item["pose_id"] == "extreme-failed")
        prototype["covers_requirements"] = ["state-extreme-anchor:BAD"]
        invalid_documents.append(tuple(documents))
        for documents in invalid_documents:
            with self.subTest(documents=invalid_documents.index(documents)):
                self.assert_design_fails(documents)

    def test_hyphenated_declared_scoped_requirement_matches_schema_pattern(self):
        documents = napoleon_design_documents(self.run_dir)
        contract, rationale, storyboard, plan, look = documents
        risk = next(item for item in contract["generation_risks"] if item["risk"] == "state_extreme")
        risk["affected_states"] = ["running-left"]
        for requirements in (contract["prototype_requirements"], plan["prototypes"]):
            item = next(item for item in requirements if item["evidence_kind"] == "state-extreme-anchor:failed")
            item["evidence_kind"] = "state-extreme-anchor:running-left"
            if "covers_requirements" in item:
                item["covers_requirements"] = ["state-extreme-anchor:running-left"]
        contract["prototype_requirements"] = [item for item in contract["prototype_requirements"] if item["evidence_kind"] != "state-extreme-anchor:review"]
        plan["prototypes"] = [item for item in plan["prototypes"] if item["evidence_kind"] != "state-extreme-anchor:review"]
        plan["estimated_provider_calls"] -= 1
        self.assertEqual(self.submit(documents).state, "prototyping")

    def test_unowned_design_recovery_evidence_is_preserved_and_rejected(self):
        evidence_names = [
            *(f".design-backup-{name}" for name in DESIGN_FILES),
            ".design-backup-workflow.json",
        ]
        for evidence_name in evidence_names:
            with self.subTest(evidence_name=evidence_name), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve()
                reference = root / "portrait.png"
                reference.write_bytes(b"portrait")
                run_dir = root / "run"
                initialize_design_run(run_dir, pet_id="sample-pet", references=(PetReference(reference, "identity"),))
                submit_intake(run_dir, valid_intake(run_dir))
                evidence = run_dir / evidence_name
                original = f"unowned:{evidence_name}".encode()
                evidence.write_bytes(original)
                documents = valid_design_documents(run_dir)

                with self.assertRaisesRegex(DesignPackError, "^design submission failed$") as raised:
                    contract, rationale, storyboard, plan, look = documents
                    submit_design(run_dir, contract=contract, rationale=rationale, storyboard=storyboard, prototype_plan=plan, look_mechanics=look)

                self.assertIsNone(raised.exception.__cause__)
                self.assertEqual(evidence.read_bytes(), original)
                self.assertFalse((run_dir / ".design-publication.prepare").exists())
                self.assertFalse((run_dir / "design-publication.json").exists())
                self.assertEqual(load_workflow_v2(run_dir).state, "designing")

    def test_all_cardinal_pose_ids_must_exist_in_prototype_plan(self):
        for direction in ("000", "090", "180", "270"):
            documents = list(valid_design_documents(self.run_dir))
            documents[4]["cardinal_families"][direction]["pose_id"] = "missing-pose"
            with self.subTest(direction=direction):
                self.assert_design_fails(tuple(documents))

    def test_unsafe_mirror_cardinals_require_side_coverage_and_generated_prototypes(self):
        for direction, pose_id, requirement in (
            ("090", "right", "screen-right-anchor"),
            ("270", "left", "screen-left-anchor"),
        ):
            for mutation in ("coverage", "generation"):
                documents = napoleon_design_documents(self.run_dir)
                prototype = next(item for item in documents[3]["prototypes"] if item["pose_id"] == pose_id)
                if mutation == "coverage":
                    prototype["covers_requirements"].remove(requirement)
                else:
                    prototype["generation_method"] = "derive"
                    documents[3]["estimated_provider_calls"] -= 1
                with self.subTest(direction=direction, mutation=mutation):
                    self.assert_design_fails(documents)

    def test_generated_call_count_may_be_under_or_equal_to_intake_authorization(self):
        self.assertEqual(self.submit().state, "prototyping")

    def test_generated_call_count_equal_to_intake_authorization_is_allowed(self):
        intake = read_json(self.run_dir / "design/intake.json")
        intake["budget"]["estimated_provider_calls"] = 2
        (self.run_dir / "design/intake.json").write_text(json.dumps(intake), encoding="utf-8")
        self.assertEqual(self.submit(valid_design_documents(self.run_dir)).state, "prototyping")

    def test_generated_call_count_over_intake_authorization_is_rejected(self):
        intake = read_json(self.run_dir / "design/intake.json")
        intake["budget"]["estimated_provider_calls"] = 1
        (self.run_dir / "design/intake.json").write_text(json.dumps(intake), encoding="utf-8")
        self.assert_design_fails(valid_design_documents(self.run_dir))

    def test_missing_compound_and_airborne_evidence_is_rejected_explicitly(self):
        for evidence_kind in ("stable-attachment", "grounded-anticipation", "airborne", "return-pose"):
            documents = napoleon_design_documents(self.run_dir)
            contract, rationale, storyboard, plan, look = documents
            contract["prototype_requirements"] = [item for item in contract["prototype_requirements"] if item["evidence_kind"] != evidence_kind]
            plan["prototypes"] = [item for item in plan["prototypes"] if item["evidence_kind"] != evidence_kind]
            plan["estimated_provider_calls"] -= 1
            with self.subTest(evidence_kind=evidence_kind):
                self.assert_design_fails(documents)

    def test_unknown_risk_is_rejected(self):
        documents = napoleon_design_documents(self.run_dir)
        documents[0]["generation_risks"][0]["risk"] = "arbitrary-risk"
        self.assert_design_fails(documents)

    def test_revision_mismatch_in_each_design_document_is_rejected(self):
        for index in (0, 2, 3, 4):
            documents = list(valid_design_documents(self.run_dir))
            documents[index]["design_revision"] = "design-0002"
            with self.subTest(document=index):
                self.assert_design_fails(tuple(documents))

    def test_full_intake_is_revalidated_under_lock(self):
        from omnipet import design_pack
        original = design_pack._workflow_lock_pinned

        def mutate_then_lock(context):
            intake = read_json(self.run_dir / "design/intake.json")
            intake["rights"]["status"] = "unresolved"
            (self.run_dir / "design/intake.json").write_text(json.dumps(intake), encoding="utf-8")
            return original(context)

        with patch("omnipet.design_pack._workflow_lock_pinned", side_effect=mutate_then_lock):
            self.assert_design_fails(self.documents)

    def test_caller_mutation_is_frozen_and_only_one_concurrent_submit_succeeds(self):
        from omnipet import design_pack
        documents = valid_design_documents(self.run_dir)
        original = design_pack._workflow_lock_pinned

        def mutate_then_lock(context):
            documents[0]["known_limitations"].append("late mutation")
            return original(context)

        with patch("omnipet.design_pack._workflow_lock_pinned", side_effect=mutate_then_lock):
            self.assert_design_fails(documents)

        documents = valid_design_documents(self.run_dir)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self._captured_submit(documents), range(2)))
        self.assertEqual(sum(not isinstance(result, Exception) for result in results), 1)
        self.assertEqual(load_workflow_v2(self.run_dir).state, "prototyping")

    def _captured_submit(self, documents):
        try:
            return self.submit(documents)
        except DesignPackError as error:
            return error

    def test_write_failure_rolls_back_all_five_files_and_workflow(self):
        from omnipet import design_pack
        write = design_pack._write_pinned

        def fail_third(context, location, filename, content):
            if filename == "state-storyboard.json":
                raise OSError("private path")
            return write(context, location, filename, content)

        with patch("omnipet.design_pack._write_pinned", side_effect=fail_third):
            self.assert_design_fails(self.documents)

    def test_manifest_failure_rolls_back_prompts_manifest_design_and_workflow(self):
        from omnipet import design_pack
        write = design_pack._write_pinned

        def fail_manifest(context, location, filename, content):
            if filename == "imagegen-jobs.json":
                raise OSError("private path")
            return write(context, location, filename, content)

        with patch("omnipet.design_pack._write_pinned", side_effect=fail_manifest):
            self.assert_design_fails(self.documents)
        self.assertFalse((self.run_dir / "imagegen-jobs.json").exists())
        self.assertFalse((self.run_dir / "prompts").exists())
        self.assertFalse((self.run_dir / "generated-sources").exists())

    def test_restart_recovers_crash_to_exact_designing_state(self):
        from omnipet import design_pack
        write = design_pack._write_pinned

        def crash_on_third(context, location, filename, content):
            if filename == "state-storyboard.json":
                raise KeyboardInterrupt("crash")
            return write(context, location, filename, content)

        with patch("omnipet.design_pack._write_pinned", side_effect=crash_on_third):
            with self.assertRaises(KeyboardInterrupt):
                self.submit()
        self.assertEqual(load_workflow_v2(self.run_dir).state, "designing")
        self.assertFalse(any((self.run_dir / "design" / name).exists() for name in DESIGN_FILES))

    def test_restart_finalizes_committed_five_file_publication(self):
        with patch("omnipet.design_pack._finalize_design_publication", side_effect=KeyboardInterrupt("crash")):
            with self.assertRaises(KeyboardInterrupt):
                self.submit()
        self.assertEqual(load_workflow_v2(self.run_dir).state, "prototyping")
        self.assertTrue(all((self.run_dir / "design" / name).is_file() for name in DESIGN_FILES))
        self.assertFalse((self.run_dir / "design-publication.json").exists())

    def test_prepare_crash_is_cleaned_by_next_operation(self):
        from omnipet import design_pack
        write = design_pack._write_pinned

        def crash_during_backup(context, location, filename, content):
            if filename == ".design-backup-workflow.json":
                raise KeyboardInterrupt("crash")
            return write(context, location, filename, content)

        with patch("omnipet.design_pack._write_pinned", side_effect=crash_during_backup):
            with self.assertRaises(KeyboardInterrupt):
                self.submit()
        self.assertEqual(load_workflow_v2(self.run_dir).state, "designing")
        self.assertEqual(self.submit().state, "prototyping")


class SubmitIntakeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.reference = self.root / "portrait.png"
        self.reference.write_bytes(b"portrait")
        self.run_dir = self.root / "run"
        initialize_design_run(
            self.run_dir,
            pet_id="sample-pet",
            references=(PetReference(self.reference, "identity"),),
        )
        self.payload = valid_intake(self.run_dir)

    def assert_submission_fails_without_mutation(self, payload):
        workflow = self.run_dir / "workflow.json"
        before = workflow.read_bytes()

        with self.assertRaisesRegex(DesignPackError, "^intake submission failed$") as caught:
            submit_intake(self.run_dir, payload)

        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(workflow.read_bytes(), before)
        self.assertFalse((self.run_dir / "design/intake.json").exists())

    def test_valid_intake_is_persisted_and_advances_workflow(self):
        state = submit_intake(self.run_dir, self.payload)

        self.assertEqual(state.state, "designing")
        self.assertEqual(read_json(self.run_dir / "design/intake.json"), self.payload)
        self.assertEqual(load_workflow_v2(self.run_dir).state, "designing")
        self.assertFalse(any(self.run_dir.joinpath("design").glob(".intake.json-*")))

    def test_top_level_and_nested_records_are_closed(self):
        invalid = []
        for key in self.payload:
            value = dict(self.payload)
            value.pop(key)
            invalid.append(value)
        invalid.extend((
            changed(self.payload, extra=True),
            changed(self.payload, references=[{**self.payload["references"][0], "extra": True}]),
            changed(self.payload, rights={**self.payload["rights"], "extra": True}),
            changed(self.payload, budget={**self.payload["budget"], "extra": True}),
        ))

        for payload in invalid:
            with self.subTest(payload=payload):
                self.assert_submission_fails_without_mutation(payload)

    def test_schema_identity_and_revision_must_match_exactly(self):
        for updates in (
            {"schema_version": True},
            {"schema_version": 1.0},
            {"schema_version": 2},
            {"pet_id": "other-pet"},
            {"pet_id": "Sample_Pet"},
            {"pet_id": "sample--pet"},
            {"design_revision": "design-0002"},
            {"design_revision": "design-1"},
            {"design_revision": "revision-0001"},
        ):
            with self.subTest(updates=updates):
                self.assert_submission_fails_without_mutation(changed(self.payload, **updates))

    def test_mutation_between_prevalidation_and_lock_is_rejected(self):
        from omnipet import design_pack

        original = design_pack._workflow_lock_pinned
        for mutate in (
            lambda payload: payload.update(style_request="Changed after validation"),
            lambda payload: payload["budget"].update(authorized_usd=99),
            lambda payload: payload["observed_facts"].append("Late fact"),
        ):
            with self.subTest(mutate=mutate):
                payload = valid_intake(self.run_dir)

                def mutate_then_lock(context):
                    mutate(payload)
                    return original(context)

                with patch(
                    "omnipet.design_pack._workflow_lock_pinned",
                    side_effect=mutate_then_lock,
                ):
                    self.assert_submission_fails_without_mutation(payload)

    def test_references_must_match_metadata_and_snapshot_bytes_exactly(self):
        reference = self.payload["references"][0]
        invalid_references = (
            [],
            [reference, dict(reference)],
            [{**reference, "path": "../portrait.png"}],
            [{**reference, "path": "/tmp/portrait.png"}],
            [{**reference, "path": "references/other.png"}],
            [{**reference, "role": "style"}],
            [{**reference, "sha256": "0" * 64}],
            [{**reference, "sha256": "A" * 64}],
            [{**reference, "sha256": "not-a-hash"}],
        )
        for references in invalid_references:
            with self.subTest(references=references):
                self.assert_submission_fails_without_mutation(
                    changed(self.payload, references=references)
                )

        snapshot = self.run_dir / reference["path"]
        snapshot.write_bytes(b"changed")
        self.assert_submission_fails_without_mutation(self.payload)

    def test_rejects_symlinked_run_artifacts_and_reference_snapshots(self):
        cases = ("workflow.json", "omnipet-run.json", "design")
        for relative in cases:
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as name:
                    root = Path(name).resolve()
                    run_dir = root / "run"
                    initialize_design_run(run_dir, pet_id="sample-pet")
                    payload = valid_intake(run_dir)
                    path = run_dir / relative
                    external = root / f"external-{path.name}"
                    if path.is_dir():
                        path.rmdir()
                        external.mkdir()
                        path.symlink_to(external, target_is_directory=True)
                    else:
                        content = path.read_bytes()
                        path.unlink()
                        external.write_bytes(content)
                        path.symlink_to(external)
                    with self.assertRaisesRegex(DesignPackError, "^intake submission failed$"):
                        submit_intake(run_dir, payload)

        snapshot = self.run_dir / self.payload["references"][0]["path"]
        outside = self.root / "outside.png"
        outside.write_bytes(snapshot.read_bytes())
        snapshot.unlink()
        snapshot.symlink_to(outside)
        self.assert_submission_fails_without_mutation(self.payload)

    def test_rights_and_budget_must_be_resolved_positive_and_exactly_typed(self):
        invalid = (
            changed(self.payload, rights={"status": "unresolved", "note": "Pending"}),
            changed(self.payload, rights={"status": True, "note": "Owner"}),
            changed(self.payload, budget={"authorized_usd": 0, "estimated_provider_calls": 6}),
            changed(self.payload, budget={"authorized_usd": True, "estimated_provider_calls": 6}),
            changed(self.payload, budget={"authorized_usd": float("inf"), "estimated_provider_calls": 6}),
            changed(self.payload, budget={"authorized_usd": 5, "estimated_provider_calls": 0}),
            changed(self.payload, budget={"authorized_usd": 5, "estimated_provider_calls": True}),
            changed(self.payload, budget={"authorized_usd": 5, "estimated_provider_calls": 2.0}),
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assert_submission_fails_without_mutation(payload)

    def test_text_rejects_empty_control_sensitive_and_duplicate_values(self):
        text_fields = (
            "style_request", "observed_facts", "inferred_facts", "unknowns",
            "accepted_defaults", "owner_decisions",
        )
        for field in text_fields:
            values = ("", "   ", "line\nsecret", "api_key=private-value")
            for value in values:
                replacement = value if field == "style_request" else [value]
                with self.subTest(field=field, value=value):
                    self.assert_submission_fails_without_mutation(
                        changed(self.payload, **{field: replacement})
                    )
        for field in text_fields[1:]:
            value = "Repeated fact"
            with self.subTest(field=field, duplicate=value):
                self.assert_submission_fails_without_mutation(
                    changed(self.payload, **{field: [value, value]})
                )
        self.assert_submission_fails_without_mutation(
            changed(self.payload, rights={"status": "declared", "note": "token=private"})
        )

    def test_only_observed_facts_must_be_nonempty(self):
        self.assert_submission_fails_without_mutation(
            changed(self.payload, observed_facts=[])
        )
        payload = changed(
            self.payload,
            inferred_facts=[],
            unknowns=[],
            accepted_defaults=[],
            owner_decisions=[],
        )

        state = submit_intake(self.run_dir, payload)

        self.assertEqual(state.state, "designing")
        self.assertEqual(read_json(self.run_dir / "design/intake.json"), payload)

    def test_runtime_enforces_schema_collection_and_path_bounds(self):
        fact = self.payload["observed_facts"][0]
        invalid = (
            changed(
                self.payload,
                observed_facts=[f"{fact} {index}" for index in range(257)],
            ),
            changed(
                self.payload,
                references=[
                    {
                        "path": f"references/reference-{index:02d}.png",
                        "role": "identity",
                        "sha256": "0" * 64,
                    }
                    for index in range(65)
                ],
            ),
            changed(
                self.payload,
                references=[{
                    **self.payload["references"][0],
                    "path": "references/" + "a" * 245,
                }],
            ),
        )
        for payload in invalid:
            with self.subTest(field=next(
                key for key in ("observed_facts", "references")
                if payload[key] != self.payload[key]
            )):
                self.assert_submission_fails_without_mutation(payload)

    def test_wrong_state_is_rejected_without_replacing_existing_intake(self):
        submit_intake(self.run_dir, self.payload)
        intake = self.run_dir / "design/intake.json"
        before_intake = intake.read_bytes()
        before_workflow = (self.run_dir / "workflow.json").read_bytes()

        with self.assertRaisesRegex(DesignPackError, "^intake submission failed$"):
            submit_intake(self.run_dir, self.payload)

        self.assertEqual(intake.read_bytes(), before_intake)
        self.assertEqual((self.run_dir / "workflow.json").read_bytes(), before_workflow)

    def test_metadata_change_between_validation_and_lock_is_rejected(self):
        from omnipet import design_pack

        original = design_pack._workflow_lock_pinned

        def mutate_then_lock(context):
            metadata = read_json(context.run_dir / "omnipet-run.json")
            metadata["design_revision"] = "design-0002"
            (context.run_dir / "omnipet-run.json").write_text(
                json.dumps(metadata), encoding="utf-8"
            )
            return original(context)

        with patch(
            "omnipet.design_pack._workflow_lock_pinned",
            side_effect=mutate_then_lock,
        ):
            self.assert_submission_fails_without_mutation(self.payload)

    def test_write_or_transition_failure_restores_exact_previous_bytes(self):
        intake = self.run_dir / "design/intake.json"
        intake.write_bytes(b"preexisting exact bytes\n")
        workflow = self.run_dir / "workflow.json"
        before_workflow = workflow.read_bytes()

        with patch(
            "omnipet.design_pack._transition_workflow_pinned",
            side_effect=OSError("private transition detail"),
        ):
            with self.assertRaisesRegex(DesignPackError, "^intake submission failed$") as caught:
                submit_intake(self.run_dir, self.payload)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(intake.read_bytes(), b"preexisting exact bytes\n")
        self.assertEqual(workflow.read_bytes(), before_workflow)

        intake.unlink()
        with patch("omnipet.design_pack.os.replace", side_effect=OSError("private path")):
            self.assert_submission_fails_without_mutation(self.payload)

    def test_two_concurrent_submissions_allow_exactly_one_success(self):
        def submit():
            try:
                return submit_intake(self.run_dir, self.payload)
            except DesignPackError as error:
                return error

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: submit(), range(2)))

        self.assertEqual(sum(not isinstance(result, Exception) for result in results), 1)
        self.assertEqual(sum(isinstance(result, DesignPackError) for result in results), 1)
        self.assertEqual(load_workflow_v2(self.run_dir).state, "designing")
        self.assertEqual(read_json(self.run_dir / "design/intake.json"), self.payload)

    def test_restart_rolls_back_crash_after_intake_install(self):
        from omnipet import design_pack

        before = (self.run_dir / "workflow.json").read_bytes()
        update = design_pack._update_journal_state

        def crash_before_intake_marker(run_dir, journal, state):
            if state == "intake-installed":
                raise KeyboardInterrupt("simulated crash")
            return update(run_dir, journal, state)

        with patch(
            "omnipet.design_pack._update_journal_state",
            side_effect=crash_before_intake_marker,
        ):
            with self.assertRaises(KeyboardInterrupt):
                submit_intake(self.run_dir, self.payload)

        self.assertTrue((self.run_dir / "design-intake-publication.json").is_file())
        self.assertTrue((self.run_dir / "design/intake.json").is_file())

        state = load_workflow_v2(self.run_dir)

        self.assertEqual(state.state, "intake")
        self.assertEqual((self.run_dir / "workflow.json").read_bytes(), before)
        self.assertFalse((self.run_dir / "design/intake.json").exists())
        self.assert_publication_recovery_files_absent()

    def test_restart_rolls_back_crash_after_workflow_install_before_marker(self):
        from omnipet import design_pack

        before = (self.run_dir / "workflow.json").read_bytes()
        update = design_pack._update_journal_state

        def crash_before_workflow_marker(run_dir, journal, state):
            if state == "workflow-installed":
                raise KeyboardInterrupt("simulated crash")
            return update(run_dir, journal, state)

        with patch(
            "omnipet.design_pack._update_journal_state",
            side_effect=crash_before_workflow_marker,
        ):
            with self.assertRaises(KeyboardInterrupt):
                submit_intake(self.run_dir, self.payload)

        self.assertEqual(load_workflow_v2(self.run_dir).state, "intake")
        self.assertEqual((self.run_dir / "workflow.json").read_bytes(), before)
        self.assertFalse((self.run_dir / "design/intake.json").exists())
        self.assert_publication_recovery_files_absent()

    def test_refresh_recovers_incomplete_publication_before_observing_state(self):
        from omnipet import design_pack

        update = design_pack._update_journal_state

        def crash_before_workflow_marker(run_dir, journal, state):
            if state == "workflow-installed":
                raise KeyboardInterrupt("simulated crash")
            return update(run_dir, journal, state)

        with patch(
            "omnipet.design_pack._update_journal_state",
            side_effect=crash_before_workflow_marker,
        ):
            with self.assertRaises(KeyboardInterrupt):
                submit_intake(self.run_dir, self.payload)

        state = refresh_workflow(self.run_dir)

        self.assertEqual(state.state, "intake")
        self.assertFalse((self.run_dir / "design/intake.json").exists())
        self.assert_publication_recovery_files_absent()

    def test_restart_finalizes_valid_workflow_installed_pair(self):
        with patch(
            "omnipet.design_pack._finalize_publication",
            side_effect=KeyboardInterrupt("simulated crash"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                submit_intake(self.run_dir, self.payload)

        self.assertEqual(
            read_json(self.run_dir / "design-intake-publication.json")["state"],
            "workflow-installed",
        )

        state = load_workflow_v2(self.run_dir)

        self.assertEqual(state.state, "designing")
        self.assertEqual(read_json(self.run_dir / "design/intake.json"), self.payload)
        self.assert_publication_recovery_files_absent()

    def test_generic_loader_recovers_each_incomplete_publication(self):
        from omnipet import design_pack

        for crash_state in ("intake-installed", "workflow-installed"):
            with self.subTest(crash_state=crash_state):
                with tempfile.TemporaryDirectory() as name:
                    root = Path(name).resolve()
                    run_dir = root / "run"
                    initialize_design_run(run_dir, pet_id="sample-pet")
                    payload = valid_intake(run_dir)
                    update = design_pack._update_journal_state

                    def crash_at_marker(run_dir, journal, state):
                        if state == crash_state:
                            raise KeyboardInterrupt("simulated crash")
                        return update(run_dir, journal, state)

                    with patch(
                        "omnipet.design_pack._update_journal_state",
                        side_effect=crash_at_marker,
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            submit_intake(run_dir, payload)

                    self.assertEqual(load_workflow(run_dir).state, "intake")
                    self.assertFalse((run_dir / "design/intake.json").exists())

    def test_cleanup_crash_at_every_recovery_file_is_replayable(self):
        from omnipet import design_pack

        recovery_names = (
            ".design-intake.previous",
            ".design-intake.previous.absent",
            ".design-workflow.previous",
            "design-intake-publication.json",
        )
        for crash_name in recovery_names:
            with self.subTest(crash_name=crash_name):
                with tempfile.TemporaryDirectory() as name:
                    root = Path(name).resolve()
                    run_dir = root / "run"
                    initialize_design_run(run_dir, pet_id="sample-pet")
                    payload = valid_intake(run_dir)
                    if crash_name == ".design-intake.previous":
                        (run_dir / "design/intake.json").write_bytes(b"prior intake\n")
                    unlink = design_pack._unlink_recovery_file
                    crashed = False

                    def crash_after_unlink(context, filename):
                        nonlocal crashed
                        result = unlink(context, filename)
                        if filename == crash_name and not crashed:
                            crashed = True
                            raise KeyboardInterrupt("simulated cleanup crash")
                        return result

                    with patch(
                        "omnipet.design_pack._unlink_recovery_file",
                        side_effect=crash_after_unlink,
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            submit_intake(run_dir, payload)

                    state = load_workflow(run_dir)
                    self.assertEqual(state.state, "designing")
                    self.assertEqual(read_json(run_dir / "design/intake.json"), payload)
                    for relative in recovery_names:
                        self.assertFalse((run_dir / relative).exists(), relative)

    def test_cleanup_crash_after_rollback_restoration_is_replayable(self):
        from omnipet import design_pack

        for crash_name in (
            ".design-intake.previous.absent",
            ".design-workflow.previous",
            "design-intake-publication.json",
        ):
            with self.subTest(crash_name=crash_name):
                with tempfile.TemporaryDirectory() as name:
                    root = Path(name).resolve()
                    run_dir = root / "run"
                    initialize_design_run(run_dir, pet_id="sample-pet")
                    payload = valid_intake(run_dir)
                    unlink = design_pack._unlink_recovery_file
                    crashed = False

                    def crash_after_unlink(context, filename):
                        nonlocal crashed
                        result = unlink(context, filename)
                        if filename == crash_name and not crashed:
                            crashed = True
                            raise KeyboardInterrupt("simulated cleanup crash")
                        return result

                    with (
                        patch(
                            "omnipet.design_pack._transition_workflow_pinned",
                            side_effect=OSError("transition failed"),
                        ),
                        patch(
                            "omnipet.design_pack._unlink_recovery_file",
                            side_effect=crash_after_unlink,
                        ),
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            submit_intake(run_dir, payload)

                    self.assertEqual(load_workflow(run_dir).state, "intake")
                    self.assertFalse((run_dir / "design/intake.json").exists())
                    self.assert_publication_recovery_files_absent_for(run_dir)

    def test_design_directory_swap_at_write_boundary_never_mutates_replacement(self):
        from omnipet import design_pack

        original_design = self.run_dir / "design-original"
        replacement = self.run_dir / "design"
        marker = b"external replacement"
        write = design_pack._write_pinned
        swapped = False

        def swap_then_write(context, location, filename, content):
            nonlocal swapped
            if location == "design" and filename == "intake.json" and not swapped:
                replacement.rename(original_design)
                replacement.mkdir()
                (replacement / "marker").write_bytes(marker)
                swapped = True
            return write(context, location, filename, content)

        with patch("omnipet.design_pack._write_pinned", side_effect=swap_then_write):
            with self.assertRaisesRegex(DesignPackError, "^intake submission failed$"):
                submit_intake(self.run_dir, self.payload)

        self.assertEqual((replacement / "marker").read_bytes(), marker)
        self.assertFalse((replacement / "intake.json").exists())

    def test_run_directory_swap_at_write_boundary_never_mutates_replacement(self):
        from omnipet import design_pack

        original_run = self.root / "run-original"
        marker = b"external replacement"
        write = design_pack._write_pinned
        swapped = False

        def swap_then_write(context, location, filename, content):
            nonlocal swapped
            if location == "run" and filename == ".design-workflow.previous" and not swapped:
                self.run_dir.rename(original_run)
                self.run_dir.mkdir()
                (self.run_dir / "marker").write_bytes(marker)
                swapped = True
            return write(context, location, filename, content)

        with patch("omnipet.design_pack._write_pinned", side_effect=swap_then_write):
            with self.assertRaisesRegex(DesignPackError, "^intake submission failed$"):
                submit_intake(self.run_dir, self.payload)

        self.assertEqual((self.run_dir / "marker").read_bytes(), marker)
        self.assertEqual(set(path.name for path in self.run_dir.iterdir()), {"marker"})

    def test_failure_at_every_prepare_step_leaves_no_poisoned_evidence(self):
        from omnipet import design_pack

        steps = (
            "preparation-owned",
            "intake-backup-written",
            "workflow-backup-written",
            "backups-fsynced",
        )
        for failed_step in steps:
            with self.subTest(failed_step=failed_step):
                with tempfile.TemporaryDirectory() as name:
                    root = Path(name).resolve()
                    run_dir = root / "run"
                    initialize_design_run(run_dir, pet_id="sample-pet")
                    payload = valid_intake(run_dir)
                    workflow_before = (run_dir / "workflow.json").read_bytes()
                    step = design_pack._prepare_step

                    def fail_at_step(context, current):
                        step(context, current)
                        if current == failed_step:
                            raise OSError("simulated preparation failure")

                    with patch(
                        "omnipet.design_pack._prepare_step",
                        side_effect=fail_at_step,
                    ):
                        with self.assertRaisesRegex(
                            DesignPackError, "^intake submission failed$"
                        ):
                            submit_intake(run_dir, payload)

                    self.assertEqual(
                        (run_dir / "workflow.json").read_bytes(), workflow_before
                    )
                    self.assertFalse((run_dir / "design/intake.json").exists())

                    state = submit_intake(run_dir, valid_intake(run_dir))

                    self.assertEqual(state.state, "designing")
                    self.assertFalse(any(
                        path.name.startswith(".design-intake.prepare")
                        for path in run_dir.iterdir()
                    ))
                    self.assert_publication_recovery_files_absent_for(run_dir)

    def test_run_replacement_before_lock_receives_no_writes(self):
        from omnipet import design_pack

        original_run = self.root / "run-original"
        replacement_source = self.root / "replacement-source"
        shutil.copytree(self.run_dir, replacement_source)
        lock = design_pack._workflow_lock_pinned
        replaced = False

        def replace_then_lock(context):
            nonlocal replaced
            if not replaced:
                self.run_dir.rename(original_run)
                replacement_source.rename(self.run_dir)
                replaced = True
            return lock(context)

        with patch(
            "omnipet.design_pack._workflow_lock_pinned",
            side_effect=replace_then_lock,
        ):
            with self.assertRaisesRegex(DesignPackError, "^intake submission failed$"):
                submit_intake(self.run_dir, self.payload)

        self.assertFalse((self.run_dir / "design/intake.json").exists())
        self.assertFalse((self.run_dir / "design-intake-publication.json").exists())

    def test_lock_entry_is_rechecked_before_release_when_body_fails(self):
        from omnipet import design_pack

        context = design_pack._pin_directories(self.run_dir)
        self.addCleanup(context.close)

        with self.assertRaisesRegex(
            Exception, "workflow lock is unavailable"
        ):
            with design_pack._workflow_lock_pinned(context):
                lock_path = self.run_dir / ".workflow.lock"
                lock_path.unlink()
                lock_path.write_bytes(b"replacement")
                raise OSError("body failed")

    def test_design_replacement_before_lock_receives_no_writes(self):
        from omnipet import design_pack

        original_design = self.run_dir / "design-original"
        replacement_source = self.run_dir / "design-replacement"
        shutil.copytree(self.run_dir / "design", replacement_source)
        lock = design_pack._workflow_lock_pinned
        replaced = False

        def replace_then_lock(context):
            nonlocal replaced
            if not replaced:
                (self.run_dir / "design").rename(original_design)
                replacement_source.rename(self.run_dir / "design")
                replaced = True
            return lock(context)

        with patch(
            "omnipet.design_pack._workflow_lock_pinned",
            side_effect=replace_then_lock,
        ):
            with self.assertRaisesRegex(DesignPackError, "^intake submission failed$"):
                submit_intake(self.run_dir, self.payload)

        self.assertFalse((self.run_dir / "design/intake.json").exists())

    def test_lock_entry_replacement_after_flock_is_rejected(self):
        from omnipet import design_pack

        flock = design_pack.fcntl.flock
        replaced = False

        def replace_after_lock(descriptor, operation):
            nonlocal replaced
            result = flock(descriptor, operation)
            if operation == design_pack.fcntl.LOCK_EX and not replaced:
                lock_path = self.run_dir / ".workflow.lock"
                lock_path.unlink()
                lock_path.write_bytes(b"replacement")
                replaced = True
            return result

        with patch("omnipet.design_pack.fcntl.flock", side_effect=replace_after_lock):
            with self.assertRaisesRegex(DesignPackError, "^intake submission failed$"):
                submit_intake(self.run_dir, self.payload)

        self.assertFalse((self.run_dir / "design/intake.json").exists())
        self.assertFalse((self.run_dir / "design-intake-publication.json").exists())

    def test_submission_uses_only_pinned_authoritative_readers(self):
        with (
            patch(
                "omnipet.design_pack._read_json",
                side_effect=AssertionError("pathname JSON read"),
            ),
            patch(
                "omnipet.design_pack._hash_regular_file",
                side_effect=AssertionError("pathname reference read"),
            ),
            patch(
                "omnipet.design_pack._transition_workflow_pinned",
                wraps=__import__("omnipet.design_pack", fromlist=["x"])._transition_workflow_pinned,
            ) as transition,
        ):
            state = submit_intake(self.run_dir, self.payload)

        transition.assert_called_once()
        self.assertEqual(state.state, "designing")

    def test_pathname_transition_is_not_used_by_submission(self):
        with patch(
                "omnipet.workflow._transition_workflow_v2_unlocked",
                side_effect=AssertionError("pathname transition"),
        ):
            state = submit_intake(self.run_dir, self.payload)

        self.assertEqual(state.state, "designing")

    def test_recovery_run_replacement_before_lock_receives_no_writes(self):
        from omnipet import design_pack

        update = design_pack._update_journal_state

        def crash_before_intake_marker(context, journal, state):
            if state == "intake-installed":
                raise KeyboardInterrupt("simulated crash")
            return update(context, journal, state)

        with patch(
            "omnipet.design_pack._update_journal_state",
            side_effect=crash_before_intake_marker,
        ):
            with self.assertRaises(KeyboardInterrupt):
                submit_intake(self.run_dir, self.payload)

        original_run = self.root / "run-original"
        replacement_source = self.root / "replacement-source"
        shutil.copytree(self.run_dir, replacement_source)
        lock = design_pack._workflow_lock_pinned
        replaced = False

        def replace_then_lock(context):
            nonlocal replaced
            if not replaced:
                self.run_dir.rename(original_run)
                replacement_source.rename(self.run_dir)
                replaced = True
            return lock(context)

        with patch(
            "omnipet.design_pack._workflow_lock_pinned",
            side_effect=replace_then_lock,
        ):
            with self.assertRaises(Exception):
                design_pack.recover_intake_submission(self.run_dir)

        self.assertTrue((self.run_dir / "design-intake-publication.json").exists())
        self.assertTrue((self.run_dir / "design/intake.json").exists())

    def test_recovery_design_replacement_before_lock_receives_no_writes(self):
        from omnipet import design_pack

        update = design_pack._update_journal_state

        def crash_before_intake_marker(context, journal, state):
            if state == "intake-installed":
                raise KeyboardInterrupt("simulated crash")
            return update(context, journal, state)

        with patch(
            "omnipet.design_pack._update_journal_state",
            side_effect=crash_before_intake_marker,
        ):
            with self.assertRaises(KeyboardInterrupt):
                submit_intake(self.run_dir, self.payload)

        original_design = self.run_dir / "design-original"
        replacement_source = self.run_dir / "design-replacement"
        shutil.copytree(self.run_dir / "design", replacement_source)
        lock = design_pack._workflow_lock_pinned
        replaced = False

        def replace_then_lock(context):
            nonlocal replaced
            if not replaced:
                (self.run_dir / "design").rename(original_design)
                replacement_source.rename(self.run_dir / "design")
                replaced = True
            return lock(context)

        with patch(
            "omnipet.design_pack._workflow_lock_pinned",
            side_effect=replace_then_lock,
        ):
            with self.assertRaises(Exception):
                design_pack.recover_intake_submission(self.run_dir)

        self.assertTrue((self.run_dir / "design/intake.json").exists())
        self.assertTrue((self.run_dir / "design-intake-publication.json").exists())

    def test_recovery_replacement_between_presence_check_and_full_pin_is_rejected(self):
        from omnipet import design_pack

        update = design_pack._update_journal_state

        def crash_before_intake_marker(context, journal, state):
            if state == "intake-installed":
                raise KeyboardInterrupt("simulated crash")
            return update(context, journal, state)

        with patch(
            "omnipet.design_pack._update_journal_state",
            side_effect=crash_before_intake_marker,
        ):
            with self.assertRaises(KeyboardInterrupt):
                submit_intake(self.run_dir, self.payload)

        original_run = self.root / "run-original"
        replacement_source = self.root / "replacement-source"
        shutil.copytree(self.run_dir, replacement_source)
        complete_pin = design_pack._pin_remaining_directories
        replaced = False

        def replace_then_complete(run_dir, run_fd, run_identity):
            nonlocal replaced
            if not replaced:
                self.run_dir.rename(original_run)
                replacement_source.rename(self.run_dir)
                replaced = True
            return complete_pin(run_dir, run_fd, run_identity)

        with patch(
            "omnipet.design_pack._pin_remaining_directories",
            side_effect=replace_then_complete,
        ):
            with self.assertRaises(Exception):
                design_pack.recover_intake_submission(self.run_dir)

        self.assertTrue((self.run_dir / "design-intake-publication.json").exists())
        self.assertTrue((self.run_dir / "design/intake.json").exists())

    def test_metadata_entry_swap_at_pinned_read_does_not_touch_external_file(self):
        from omnipet import design_pack

        metadata = self.run_dir / "omnipet-run.json"
        external = self.root / "external-metadata.json"
        external_before = metadata.read_bytes()
        external.write_bytes(external_before)
        read = design_pack._read_pinned
        swapped = False

        def swap_then_read(context, location, filename, **kwargs):
            nonlocal swapped
            if location == "run" and filename == "omnipet-run.json" and not swapped:
                metadata.unlink()
                metadata.symlink_to(external)
                swapped = True
            return read(context, location, filename, **kwargs)

        with patch("omnipet.design_pack._read_pinned", side_effect=swap_then_read):
            with self.assertRaisesRegex(DesignPackError, "^intake submission failed$"):
                submit_intake(self.run_dir, self.payload)

        self.assertEqual(external.read_bytes(), external_before)
        self.assertFalse((self.run_dir / "design/intake.json").exists())

    def test_workflow_entry_swap_at_transition_does_not_touch_external_file(self):
        from omnipet import design_pack

        workflow = self.run_dir / "workflow.json"
        external = self.root / "external-workflow.json"
        external_before = workflow.read_bytes()
        external.write_bytes(external_before)
        transition = design_pack._transition_workflow_pinned

        def swap_then_transition(context, event):
            workflow.unlink()
            workflow.symlink_to(external)
            return transition(context, event)

        with patch(
            "omnipet.design_pack._transition_workflow_pinned",
            side_effect=swap_then_transition,
        ):
            with self.assertRaisesRegex(DesignPackError, "^intake submission failed$"):
                submit_intake(self.run_dir, self.payload)

        self.assertEqual(external.read_bytes(), external_before)
        self.assertEqual(load_workflow_v2(self.run_dir).state, "intake")
        self.assertFalse((self.run_dir / "design/intake.json").exists())

    def test_failed_live_rollback_is_recovered_by_next_submission(self):
        from omnipet import design_pack

        restore = design_pack._restore_from_backup
        attempts = 0

        def fail_first_restore(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated rollback write failure")
            return restore(*args, **kwargs)

        with (
            patch(
                "omnipet.design_pack._transition_workflow_pinned",
                side_effect=OSError("transition failed"),
            ),
            patch(
                "omnipet.design_pack._restore_from_backup",
                side_effect=fail_first_restore,
            ),
        ):
            with self.assertRaisesRegex(DesignPackError, "^intake submission failed$"):
                submit_intake(self.run_dir, self.payload)

        self.assertTrue((self.run_dir / "design-intake-publication.json").is_file())

        state = submit_intake(self.run_dir, valid_intake(self.run_dir))

        self.assertEqual(state.state, "designing")
        self.assert_publication_recovery_files_absent()

    def assert_publication_recovery_files_absent(self):
        self.assert_publication_recovery_files_absent_for(self.run_dir)

    def assert_publication_recovery_files_absent_for(self, run_dir):
        for relative in (
            "design-intake-publication.json",
            ".design-intake.previous",
            ".design-intake.previous.absent",
            ".design-workflow.previous",
        ):
            self.assertFalse((run_dir / relative).exists(), relative)


if __name__ == "__main__":
    unittest.main()
