import hashlib
import json
import tempfile
import copy
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from omnipet.actions import (
    ActionError,
    build_action_contract,
    compute_run_revision,
    _validate_contract,
    validate_action_request,
)
from omnipet.agent.resources import load_json_resource
from omnipet.design_pack import (
    reject_design_pack_action,
    revise_design_pack_action,
    submit_design,
    submit_intake,
    submit_intake_action,
    submit_prototype_evidence,
    submit_prototype_evidence_action,
)
from omnipet.generation import GeneratedImage
from omnipet.project import PetReference
from omnipet.release import hatch_prototype_run_action, initialize_design_run
from tests.design_pack_fixtures import (
    valid_design_documents,
    valid_intake,
    valid_prototype_evidence,
)
from omnipet.workflow import PHASE2_STATES


ACTION_KEYS = {
    "id", "kind", "command", "required_inputs", "bound_evidence",
    "preconditions", "owner_required", "reason_code",
}


class _Generator:
    def edit(self, request):
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (1024, 1024), "navy").save(request.destination)
        content = request.destination.read_bytes()
        return GeneratedImage(
            request.destination, "image/png", hashlib.sha256(content).hexdigest(),
            1024, 1024, {"principal": "test-generator"},
        )


class _BlockingGenerator(_Generator):
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def edit(self, request):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test provider timed out")
        return super().edit(request)


class ActionContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        reference = self.root / "reference.png"
        Image.new("RGB", (8, 8), "blue").save(reference)
        self.run_dir = self.root / "run"
        initialize_design_run(
            self.run_dir, "sample-pet", (PetReference(reference, "identity"),)
        )

    def _write_workflow(self, state, blocked=None):
        (self.run_dir / "workflow.json").write_text(
            json.dumps({"schema_version": 2, "state": state, "blocked": blocked}, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _action(self, contract, kind):
        return next(action for action in contract["actions"] if action["kind"] == kind)

    def _submit_design(self):
        submit_intake(self.run_dir, valid_intake(self.run_dir))
        contract, rationale, storyboard, plan, look = valid_design_documents(self.run_dir)
        submit_design(
            self.run_dir, contract=contract, rationale=rationale,
            storyboard=storyboard, prototype_plan=plan, look_mechanics=look,
        )

    def test_intake_contract_is_closed_revision_bound_and_uses_selector(self):
        result = build_action_contract(self.run_dir, ".")

        self.assertEqual(set(result), {
            "schema_version", "action_contract_version", "run_revision", "state",
            "actions", "budget",
        })
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["action_contract_version"], 1)
        self.assertRegex(result["run_revision"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(result["state"], "intake")
        self.assertEqual(result["budget"], {
            "authorized_usd": 0, "estimated_spent_usd": 0,
            "next_call_estimate_usd": 0,
        })
        action = self._action(result, "submit-intake")
        self.assertEqual(set(action), ACTION_KEYS)
        self.assertEqual(action["id"], f"submit-intake:{result['run_revision']}")
        self.assertIn(".", action["command"])
        self.assertEqual(action["required_inputs"], [{"name": "file", "type": "path"}])
        self.assertFalse(action["owner_required"])

    def test_revision_is_hash_of_canonical_path_digest_map_and_changes(self):
        selected = (
            "workflow.json", "omnipet-run.json", "references/reference-01.png",
        )
        digest_map = {
            relative: hashlib.sha256((self.run_dir / relative).read_bytes()).hexdigest()
            for relative in selected
        }
        expected = "sha256:" + hashlib.sha256(
            json.dumps(digest_map, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(compute_run_revision(self.run_dir), expected)

        before = compute_run_revision(self.run_dir)
        submit_intake(self.run_dir, valid_intake(self.run_dir))
        self.assertNotEqual(compute_run_revision(self.run_dir), before)

    def test_revision_changes_after_prototype_evidence_mutation(self):
        self._submit_design()
        generator = _Generator()
        from omnipet.release import hatch_prototype_run

        hatch_prototype_run(self.run_dir, generator)
        before = compute_run_revision(self.run_dir)
        submit_prototype_evidence(
            self.run_dir, valid_prototype_evidence(self.run_dir, "canonical")
        )
        self.assertNotEqual(compute_run_revision(self.run_dir), before)

    def test_design_action_exposes_intake_budget_and_bound_hash(self):
        submit_intake(self.run_dir, valid_intake(self.run_dir))
        result = build_action_contract(self.run_dir, "sample-pet")
        action = self._action(result, "submit-design")
        self.assertEqual(result["budget"]["authorized_usd"], 5.0)
        self.assertEqual(action["bound_evidence"], [{
            "path": "design/intake.json",
            "sha256": hashlib.sha256((self.run_dir / "design/intake.json").read_bytes()).hexdigest(),
        }])

    def test_prototype_actions_progress_generation_evidence_then_summary(self):
        self._submit_design()
        first = build_action_contract(self.run_dir, "sample-pet")
        self.assertEqual([item["kind"] for item in first["actions"]], ["generate-prototype"])
        self.assertEqual(first["actions"][0]["preconditions"][-1], {
            "kind": "ready-job-is", "value": "canonical",
        })

        first_action = first["actions"][0]
        hatch_prototype_run_action(
            self.run_dir, _Generator(), action_id=first_action["id"],
            run_revision=first["run_revision"],
        )
        second = build_action_contract(self.run_dir, "sample-pet")
        self.assertEqual([item["kind"] for item in second["actions"]], ["generate-prototype"])

        second_action = second["actions"][0]
        hatch_prototype_run_action(
            self.run_dir, _Generator(), action_id=second_action["id"],
            run_revision=second["run_revision"],
        )
        evidence = build_action_contract(self.run_dir, "sample-pet")
        self.assertEqual([item["kind"] for item in evidence["actions"]], ["submit-prototype-evidence"])
        action = evidence["actions"][0]
        self.assertEqual(action["id"], f"submit-prototype-evidence:{evidence['run_revision']}")
        pose_input = next(item for item in action["required_inputs"] if item["name"] == "pose_id")
        self.assertEqual(pose_input["allowed_values"], ["canonical", "cycle"])

        for pose_id in pose_input["allowed_values"]:
            qa = self.run_dir / "qa/design-pack/prototypes"
            qa.mkdir(exist_ok=True)
            (qa / f"{pose_id}.json").write_text("{}\n", encoding="utf-8")
        summary = build_action_contract(self.run_dir, "sample-pet")
        self.assertEqual([item["kind"] for item in summary["actions"]], ["submit-design-pack-summary"])
        self.assertEqual(
            [item["path"] for item in summary["actions"][0]["bound_evidence"]],
            sorted(item["path"] for item in summary["actions"][0]["bound_evidence"]),
        )

    def test_owner_later_blocked_and_terminal_state_actions(self):
        expected = {
            "awaiting_design_pack_approval": (
                ["approve-design-pack", "revise-design-pack", "reject-design-pack"], True
            ),
            "producing_standard_rows": (["hatch-standard-row", "submit-standard-row-verdict"], False),
            "producing_directions": (["hatch-direction", "submit-direction-verdict"], False),
            "building_package": (["build-package", "submit-package-verdict"], False),
            "awaiting_package_approval": (
                ["approve-package", "revise-package", "reject-package"], True
            ),
        }
        for state, (kinds, owner_required) in expected.items():
            with self.subTest(state=state):
                self._write_workflow(state)
                actions = build_action_contract(self.run_dir, ".")["actions"]
                self.assertEqual([action["kind"] for action in actions], kinds)
                self.assertTrue(all(action["owner_required"] is owner_required for action in actions))

        blocked = {
            "code": "budget-exhausted", "prior_state": "producing_standard_rows",
            "job_id": "idle", "evidence_path": None,
            "root_failure_key": "budget-exhausted",
            "recoveries": [],
            "diagnostic": None,
        }
        self._write_workflow("blocked", blocked)
        self.assertEqual(build_action_contract(self.run_dir, ".")["actions"], [])

        for state in ("complete", "rejected"):
            self._write_workflow(state)
            self.assertEqual(build_action_contract(self.run_dir, ".")["actions"], [])

    def test_validation_rejects_stale_id_revision_and_kind(self):
        result = build_action_contract(self.run_dir, ".")
        action = result["actions"][0]
        validate_action_request(
            self.run_dir, action["id"], result["run_revision"], "submit-intake"
        )
        for action_id, revision, kind in (
            ("wrong", result["run_revision"], "submit-intake"),
            (action["id"], "sha256:" + "0" * 64, "submit-intake"),
            (action["id"], result["run_revision"], "submit-design"),
        ):
            with self.subTest(action_id=action_id, revision=revision, kind=kind):
                with self.assertRaises(ActionError):
                    validate_action_request(self.run_dir, action_id, revision, kind)

    def test_stale_wrapper_rejects_before_mutating_bytes(self):
        result = build_action_contract(self.run_dir, ".")
        before = {
            path.relative_to(self.run_dir).as_posix(): path.read_bytes()
            for path in self.run_dir.rglob("*")
            if path.is_file() and path.name != ".workflow.lock"
        }
        with self.assertRaises(ActionError):
            submit_intake_action(
                self.run_dir, valid_intake(self.run_dir),
                action_id=result["actions"][0]["id"],
                run_revision="sha256:" + "0" * 64,
            )
        after = {
            path.relative_to(self.run_dir).as_posix(): path.read_bytes()
            for path in self.run_dir.rglob("*")
            if path.is_file() and path.name != ".workflow.lock"
        }
        self.assertEqual(after, before)

    def test_prototype_evidence_action_is_bound_to_its_pose(self):
        self._submit_design()
        from omnipet.release import hatch_prototype_run

        hatch_prototype_run(self.run_dir, _Generator())
        hatch_prototype_run(self.run_dir, _Generator())
        contract = build_action_contract(self.run_dir, ".")
        action = contract["actions"][0]
        payload = valid_prototype_evidence(self.run_dir, "cycle")
        payload["pose_id"] = "undeclared"
        with self.assertRaises(ActionError):
            submit_prototype_evidence_action(
                self.run_dir, payload, action_id=action["id"],
                run_revision=contract["run_revision"],
            )
        self.assertFalse(
            (self.run_dir / "qa/design-pack/prototypes/cycle.json").exists()
        )

    def test_revision_hashes_every_authoritative_evidence_root(self):
        roots = (
            "design/extra.json", "prompts/prototypes/extra.md",
            "decoded/canonical.png", "decoded/prototypes/extra.png",
            "references/extra.png", "qa/design-pack/extra.json",
        )
        for relative in roots:
            target = self.run_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"first")
        for relative in roots:
            with self.subTest(relative=relative):
                before = compute_run_revision(self.run_dir)
                target = self.run_dir / relative
                target.write_bytes(target.read_bytes() + b"x")
                self.assertNotEqual(compute_run_revision(self.run_dir), before)

    def test_runtime_validator_rejects_each_malformed_field_class(self):
        valid = build_action_contract(self.run_dir, ".")
        mutations = []
        for key, value in (
            ("schema_version", 2), ("action_contract_version", 2),
            ("run_revision", "bad"), ("state", "bad"), ("actions", {}),
            ("budget", {"authorized_usd": -1, "estimated_spent_usd": 0, "next_call_estimate_usd": 0}),
        ):
            item = copy.deepcopy(valid)
            item[key] = value
            mutations.append(item)
        action_changes = {
            "id": "bad", "kind": "", "command": [1],
            "required_inputs": [{"name": "", "type": "path"}],
            "bound_evidence": [{"path": "../bad", "sha256": "x"}],
            "preconditions": [{"kind": "", "value": ""}],
            "owner_required": 1, "reason_code": "",
        }
        for key, value in action_changes.items():
            item = copy.deepcopy(valid)
            item["actions"][0][key] = value
            mutations.append(item)
        for item in mutations:
            with self.subTest(item=item), self.assertRaises((TypeError, ValueError)):
                _validate_contract(item)

    def test_stale_intake_is_rejected_inside_mutator_lock_before_write(self):
        contract = build_action_contract(self.run_dir, ".")
        from omnipet import design_pack
        real_lock = design_pack._workflow_lock_pinned

        @contextmanager
        def mutate_then_lock(context):
            workflow = self.run_dir / "workflow.json"
            workflow.write_bytes(workflow.read_bytes() + b" ")
            with real_lock(context):
                yield

        with patch("omnipet.design_pack._workflow_lock_pinned", mutate_then_lock):
            with self.assertRaises(ActionError):
                submit_intake_action(
                    self.run_dir, valid_intake(self.run_dir),
                    action_id=contract["actions"][0]["id"],
                    run_revision=contract["run_revision"],
                )
        self.assertFalse((self.run_dir / "design/intake.json").exists())

    def test_stale_prototype_is_rejected_inside_generation_lock_without_provider_or_writes(self):
        self._submit_design()
        contract = build_action_contract(self.run_dir, ".")
        before = (self.run_dir / "imagegen-jobs.json").read_bytes()
        from omnipet import prototype_jobs
        real_lock = prototype_jobs._generation_lock

        @contextmanager
        def mutate_then_lock(path):
            manifest = self.run_dir / "imagegen-jobs.json"
            manifest.write_bytes(manifest.read_bytes() + b" ")
            with real_lock(path) as acquired:
                yield acquired

        generator = _Generator()
        with patch("omnipet.prototype_jobs._generation_lock", mutate_then_lock):
            with self.assertRaises(ActionError):
                hatch_prototype_run_action(
                    self.run_dir, generator, action_id=contract["actions"][0]["id"],
                    run_revision=contract["run_revision"],
                )
        self.assertEqual(
            json.loads((self.run_dir / "imagegen-jobs.json").read_bytes()),
            json.loads(before),
        )
        self.assertFalse((self.run_dir / "generated-sources/prototypes/canonical.png").exists())

    def test_revise_and_reject_design_pack_actions_are_functional(self):
        self._submit_design()
        self._write_workflow("awaiting_design_pack_approval")
        (self.run_dir / "design/design-pack.json").write_text("{}\n", encoding="utf-8")
        (self.run_dir / "qa/design-pack/review.json").write_text("{}\n", encoding="utf-8")
        revision = build_action_contract(self.run_dir, ".")
        revise = self._action(revision, "revise-design-pack")
        result = revise_design_pack_action(
            self.run_dir, "change silhouette", action_id=revise["id"],
            run_revision=revision["run_revision"],
        )
        self.assertEqual(result.state, "designing")
        self.assertFalse((self.run_dir / "imagegen-jobs.json").exists())
        self.assertFalse((self.run_dir / "design/design-pack.json").exists())
        self.assertFalse((self.run_dir / "decoded/canonical.png").exists())
        self.assertTrue((self.run_dir / "design/design-contract.json").exists())

        self._write_workflow("awaiting_design_pack_approval")
        (self.run_dir / "design/design-pack.json").write_text("{}\n", encoding="utf-8")
        rejection = build_action_contract(self.run_dir, ".")
        reject = self._action(rejection, "reject-design-pack")
        result = reject_design_pack_action(
            self.run_dir, "not acceptable", action_id=reject["id"],
            run_revision=rejection["run_revision"],
        )
        self.assertEqual(result.state, "rejected")
        self.assertTrue((self.run_dir / "design/design-pack.json").exists())

    def test_schema_requires_the_closed_runtime_shape(self):
        schema = load_json_resource("schemas/action-contract-v1.json")
        self.assertFalse(schema["additionalProperties"])
        action = schema["properties"]["actions"]["items"]
        self.assertFalse(action["additionalProperties"])
        self.assertEqual(set(action["required"]), ACTION_KEYS)
        self.assertEqual(
            schema["properties"]["run_revision"]["pattern"],
            "^sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(schema["properties"]["state"]["enum"], list(PHASE2_STATES))
        self.assertEqual(action["properties"]["command"]["minItems"], 1)
        self.assertEqual(action["properties"]["command"]["items"]["minLength"], 1)
        self.assertTrue(schema["properties"]["actions"]["uniqueItems"])
        evidence = action["properties"]["bound_evidence"]
        self.assertTrue(evidence["uniqueItems"])
        self.assertEqual(
            evidence["items"]["properties"]["path"]["pattern"],
            r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*\\)[^/]+(?:/[^/]+)*$",
        )
        self.assertIn("runtime", action["description"].lower())

    def test_action_commands_start_with_real_public_parser_routes(self):
        expected = {
            "submit-intake": ["omnipet", "design", "intake", "sample-pet"],
            "submit-design": ["omnipet", "design", "submit", "sample-pet"],
            "generate-prototype": ["omnipet", "hatch", "sample-pet"],
            "submit-prototype-evidence": ["omnipet", "design", "prototype", "sample-pet"],
            "submit-design-pack-summary": ["omnipet", "design", "pack", "sample-pet"],
            "approve-design-pack": ["omnipet", "approve", "sample-pet", "--stage", "design-pack"],
            "revise-design-pack": ["omnipet", "design", "revise", "sample-pet"],
            "reject-design-pack": ["omnipet", "design", "reject", "sample-pet"],
        }
        contract = build_action_contract(self.run_dir, "sample-pet")
        self.assertEqual(contract["actions"][0]["command"], expected["submit-intake"])
        submit_intake(self.run_dir, valid_intake(self.run_dir))
        contract = build_action_contract(self.run_dir, "sample-pet")
        self.assertEqual(contract["actions"][0]["command"], expected["submit-design"])
        contract_doc, rationale, storyboard, plan, look = valid_design_documents(self.run_dir)
        submit_design(
            self.run_dir, contract=contract_doc, rationale=rationale,
            storyboard=storyboard, prototype_plan=plan, look_mechanics=look,
        )
        contract = build_action_contract(self.run_dir, "sample-pet")
        prototype = contract["actions"][0]
        self.assertEqual(prototype["command"][:3], expected["generate-prototype"])
        self.assertEqual(prototype["command"][3:], [
            "--action-id", prototype["id"],
            "--run-revision", contract["run_revision"],
        ])
        self._write_workflow("awaiting_design_pack_approval")
        (self.run_dir / "design/design-pack.json").write_text("{}\n", encoding="utf-8")
        contract = build_action_contract(self.run_dir, "sample-pet")
        for action in contract["actions"]:
            self.assertEqual(action["command"], expected[action["kind"]])

    def test_evidence_mutation_waits_for_prototype_provider(self):
        self._submit_design()
        contract = build_action_contract(self.run_dir, ".")
        generator = _BlockingGenerator()
        generation_done = threading.Event()
        mutation_entered = threading.Event()

        def generate():
            try:
                hatch_prototype_run_action(
                    self.run_dir, generator,
                    action_id=contract["actions"][0]["id"],
                    run_revision=contract["run_revision"],
                )
            finally:
                generation_done.set()

        def mutate():
            try:
                metadata = json.loads((self.run_dir / "omnipet-run.json").read_text())
                submit_prototype_evidence(self.run_dir, {
                    "schema_version": 1,
                    "pet_id": metadata["pet_id"],
                    "design_revision": metadata["design_revision"],
                    "pose_id": "canonical",
                    "artifact": {
                        "path": "decoded/canonical.png",
                        "sha256": "0" * 64,
                    },
                    "verdicts": {
                        category: {
                            "decision": "pass",
                            "reviewer_role": (
                                "deterministic" if category == "structural"
                                else "production-agent"
                            ),
                            "reviewer_principal_id": "reviewer-1",
                            "criteria": ["verified"],
                        }
                        for category in (
                            "structural", "view-semantic", "identity", "pose-purpose"
                        )
                    },
                    "accepted_warnings": [],
                })
            except Exception:
                mutation_entered.set()

        generation = threading.Thread(target=generate)
        generation.start()
        self.assertTrue(generator.entered.wait(2))
        mutation = threading.Thread(target=mutate)
        mutation.start()
        self.assertFalse(mutation_entered.wait(0.2))
        generator.release.set()
        self.assertTrue(generation_done.wait(5))
        self.assertTrue(mutation_entered.wait(5))
        generation.join(1)
        mutation.join(1)


if __name__ == "__main__":
    unittest.main()
