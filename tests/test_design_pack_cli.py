import io
import json
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from omnipet.actions import build_action_contract
from omnipet.cli import _build_parser, main
from omnipet.design_pack import submit_design, submit_intake
from omnipet.generation import GeneratedImage
from omnipet.project import load_pet_project
from omnipet.run import prepare_run
from tests.design_pack_fixtures import (
    valid_design_documents,
    valid_design_pack_review,
    valid_intake,
    valid_prototype_evidence,
)


class _Generator:
    calls = 0

    def edit(self, request):
        type(self).calls += 1
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (16, 16), "navy").save(request.destination)
        content = request.destination.read_bytes()
        import hashlib
        return GeneratedImage(
            request.destination, "image/png", hashlib.sha256(content).hexdigest(),
            16, 16, {"principal": "cli-test"},
        )


class _Phase1Generator:
    def generate(self, request):
        return self.edit(request)

    def edit(self, request):
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1024, 1024), "#00FF00").save(request.destination)
        content = request.destination.read_bytes()
        import hashlib
        return GeneratedImage(
            request.destination, "image/png", hashlib.sha256(content).hexdigest(),
            1024, 1024, {"principal": "phase1-cli-test"},
        )


class DesignPackCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.assertEqual(self.call(["pet", "init", "cli-pet"])[0], 0)
        pet_root = self.root / "pets/cli-pet"
        reference = pet_root / "references/portrait.png"
        reference.parent.mkdir(exist_ok=True)
        Image.new("RGB", (12, 12), "navy").save(reference)
        manifest = pet_root / "pet.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "references: []",
                "references:\n  - path: references/portrait.png\n    role: identity",
            ),
            encoding="utf-8",
        )
        self.run_dir = self.root / ".omnipet/runs/cli-pet"

    def call(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = [*args, "--repo-root", str(self.root)]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def make_prototyping_run(self):
        self.assertEqual(self.call(["design", "init", "cli-pet"])[0], 0)
        submit_intake(self.run_dir, valid_intake(self.run_dir))
        contract, rationale, storyboard, plan, look = valid_design_documents(self.run_dir)
        submit_design(
            self.run_dir, contract=contract, rationale=rationale,
            storyboard=storyboard, prototype_plan=plan, look_mechanics=look,
        )

    def test_parser_exposes_public_v2_routes(self):
        parser = _build_parser()
        cases = (
            ["design", "init", "pet"],
            ["hatch", "pet", "--action-id", "a", "--run-revision", "r"],
            ["design", "intake", "pet", "--file", "i.json", "--action-id", "a", "--run-revision", "r"],
            ["design", "submit", "pet", "--contract", "c", "--rationale", "r", "--storyboard", "s", "--prototype-plan", "p", "--look-mechanics", "l", "--action-id", "a", "--run-revision", "r"],
            ["design", "prototype", "pet", "--file", "p", "--action-id", "a", "--run-revision", "r"],
            ["design", "pack", "pet", "--contact-sheet", "c", "--review", "r", "--action-id", "a", "--run-revision", "r"],
            ["design", "revise", "pet", "--action-id", "a", "--run-revision", "r"],
            ["design", "reject", "pet", "--action-id", "a", "--run-revision", "r"],
            ["approve", "pet", "--stage", "design-pack", "--principal", "owner", "--action-id", "a", "--run-revision", "r"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertIsNotNone(parser.parse_args(argv))

    def test_status_hatch_command_executes_once_and_old_command_becomes_stale(self):
        self.make_prototyping_run()
        result, stdout, stderr = self.call(["status", "cli-pet"])
        self.assertEqual((result, stderr), (0, ""))
        command = shlex.split(json.loads(stdout)["next_action"])
        self.assertIn("--action-id", command)
        self.assertIn("--run-revision", command)

        _Generator.calls = 0
        with patch("omnipet.release.OpenAIImageGenerator", return_value=_Generator()):
            result, stdout, stderr = self.call(command[1:])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(_Generator.calls, 1)

        with patch(
            "omnipet.release.OpenAIImageGenerator", return_value=_Generator()
        ) as factory:
            result, stdout, stderr = self.call(command[1:])
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr), {"ok": False, "error": "hatch failed"})
        factory.assert_not_called()
        self.assertEqual(_Generator.calls, 1)

    def test_existing_v2_hatch_requires_complete_action_pair_before_provider(self):
        self.make_prototyping_run()
        contract = build_action_contract(self.run_dir, "cli-pet")
        action_id = contract["actions"][0]["id"]
        for extra in ([], ["--action-id", action_id], ["--run-revision", contract["run_revision"]]):
            with self.subTest(extra=extra), patch(
                "omnipet.release.OpenAIImageGenerator", return_value=_Generator()
            ) as factory:
                result, stdout, stderr = self.call(["hatch", "cli-pet", *extra])
            self.assertEqual(result, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(json.loads(stderr), {"ok": False, "error": "hatch failed"})
            factory.assert_not_called()

    def test_existing_phase1_hatch_remains_usable_without_action_flags(self):
        manifest = self.root / "pets/cli-pet/pet.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "  agent_workflow_version: 2\n", ""
            ),
            encoding="utf-8",
        )
        project = load_pet_project(self.root, "cli-pet")
        prepare_run(project, self.root)
        with patch(
            "omnipet.release.OpenAIImageGenerator", return_value=_Phase1Generator()
        ):
            result, stdout, stderr = self.call(["hatch", "cli-pet"])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "awaiting_base_approval")

    def test_v2_directions_hatch_validates_but_never_calls_legacy_or_provider(self):
        self.make_prototyping_run()
        workflow = self.run_dir / "workflow.json"
        workflow.write_text(json.dumps({
            "schema_version": 2, "state": "producing_directions", "blocked": None,
        }), encoding="utf-8")
        contract = build_action_contract(self.run_dir, "cli-pet")
        action = next(item for item in contract["actions"] if item["kind"] == "hatch-direction")
        with patch(
            "omnipet.release.OpenAIImageGenerator", return_value=_Generator()
        ) as factory, patch(
            "omnipet.release._hatch_project_locked",
            side_effect=AssertionError("legacy route called"),
        ) as legacy:
            result, stdout, stderr = self.call(action["command"][1:])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "producing_directions")
        factory.assert_not_called()
        legacy.assert_not_called()

    def test_design_init_status_and_stale_intake_are_sanitized(self):
        result, stdout, stderr = self.call(["design", "init", "cli-pet"])
        self.assertEqual((result, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["workflow_state"], "intake")
        self.assertEqual(payload["action_contract_version"], 1)
        self.assertIn("omnipet design intake cli-pet", payload["next_action"])

        intake = self.root / "intake.json"
        intake.write_text(json.dumps(valid_intake(self.run_dir)), encoding="utf-8")
        marker = "private-payload-must-not-leak"
        content = json.loads(intake.read_text())
        content["style_request"] = marker
        intake.write_text(json.dumps(content), encoding="utf-8")
        contract = build_action_contract(self.run_dir, "cli-pet")
        result, stdout, stderr = self.call([
            "design", "intake", "cli-pet", "--file", str(intake),
            "--action-id", contract["actions"][0]["id"],
            "--run-revision", "sha256:" + "0" * 64,
        ])
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr), {"ok": False, "error": "design intake failed"})
        self.assertNotIn(marker, stderr)

    def test_design_init_explicitly_uses_v2_for_legacy_project(self):
        manifest = self.root / "pets/cli-pet/pet.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "  agent_workflow_version: 2\n", ""
            ),
            encoding="utf-8",
        )

        result, stdout, stderr = self.call(["design", "init", "cli-pet"])

        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "intake")
        self.assertEqual(
            json.loads((self.run_dir / "workflow.json").read_text())["schema_version"],
            2,
        )

    def test_first_hatch_initializes_v2_without_provider_and_standalone_selector(self):
        _Generator.calls = 0
        with patch("omnipet.release.OpenAIImageGenerator", return_value=_Generator()):
            result, stdout, stderr = self.call(["hatch", "cli-pet"])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "intake")
        self.assertEqual(_Generator.calls, 0)

        standalone = self.root / "pets/cli-pet"
        result, stdout, stderr = self.call(["status", str(standalone)])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["pet_id"], "cli-pet")

    def test_vertical_slice_materializes_standard_rows_only_after_approval(self):
        def write_json(name, value):
            path = self.root / name
            path.write_text(json.dumps(value), encoding="utf-8")
            return path

        self.assertEqual(self.call(["design", "init", "cli-pet"])[0], 0)
        action = build_action_contract(self.run_dir, "cli-pet")
        intake = write_json("intake.json", valid_intake(self.run_dir))
        result, stdout, stderr = self.call([
            "design", "intake", "cli-pet", "--file", str(intake),
            "--action-id", action["actions"][0]["id"],
            "--run-revision", action["run_revision"],
        ])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "designing")

        contract, rationale, storyboard, plan, look = valid_design_documents(self.run_dir)
        files = {
            "contract": write_json("contract.json", contract),
            "storyboard": write_json("storyboard.json", storyboard),
            "prototype-plan": write_json("plan.json", plan),
            "look-mechanics": write_json("look.json", look),
        }
        rationale_path = self.root / "rationale.md"
        rationale_path.write_text(rationale, encoding="utf-8")
        action = build_action_contract(self.run_dir, "cli-pet")
        argv = ["design", "submit", "cli-pet"]
        for flag, path in files.items():
            argv.extend((f"--{flag}", str(path)))
        argv.extend((
            "--rationale", str(rationale_path), "--action-id", action["actions"][0]["id"],
            "--run-revision", action["run_revision"],
        ))
        result, stdout, stderr = self.call(argv)
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "prototyping")
        self.assertEqual(
            [item["kind"] for item in json.loads((self.run_dir / "imagegen-jobs.json").read_text())["jobs"]],
            ["prototype", "prototype"],
        )

        _Generator.calls = 0
        with patch("omnipet.release.OpenAIImageGenerator", return_value=_Generator()):
            for _ in range(2):
                status_result, status_stdout, status_stderr = self.call(["status", "cli-pet"])
                self.assertEqual((status_result, status_stderr), (0, ""))
                command = shlex.split(json.loads(status_stdout)["next_action"])
                result, _stdout, stderr = self.call(command[1:])
                self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(_Generator.calls, 2)
        for pose_id in ("canonical", "cycle"):
            action = build_action_contract(self.run_dir, "cli-pet")
            evidence = write_json(
                f"{pose_id}-evidence.json",
                valid_prototype_evidence(self.run_dir, pose_id),
            )
            result, _stdout, stderr = self.call([
                "design", "prototype", "cli-pet", "--file", str(evidence),
                "--action-id", action["actions"][0]["id"],
                "--run-revision", action["run_revision"],
            ])
            self.assertEqual((result, stderr), (0, ""))

        contact = self.root / "contact.png"
        Image.new("RGBA", (32, 16), "navy").save(contact)
        review = write_json(
            "review.json", valid_design_pack_review(self.run_dir, contact)
        )
        action = build_action_contract(self.run_dir, "cli-pet")
        result, stdout, stderr = self.call([
            "design", "pack", "cli-pet", "--contact-sheet", str(contact),
            "--review", str(review), "--action-id", action["actions"][0]["id"],
            "--run-revision", action["run_revision"],
        ])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "awaiting_design_pack_approval")
        self.assertTrue(all(
            item["kind"] == "prototype"
            for item in json.loads((self.run_dir / "imagegen-jobs.json").read_text())["jobs"]
        ))

        action = build_action_contract(self.run_dir, "cli-pet")
        approve = next(item for item in action["actions"] if item["kind"] == "approve-design-pack")
        result, stdout, stderr = self.call([
            "approve", "cli-pet", "--stage", "design-pack", "--principal", "owner-1",
            "--action-id", approve["id"], "--run-revision", action["run_revision"],
        ])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "producing_standard_rows")
        jobs = json.loads((self.run_dir / "imagegen-jobs.json").read_text())["jobs"]
        self.assertEqual(len(jobs), 9)
        self.assertTrue(all(item["kind"] == "row-strip" for item in jobs))

        result, stdout, stderr = self.call(["status", "cli-pet"])
        self.assertEqual((result, stderr), (0, ""))
        command = shlex.split(json.loads(stdout)["next_action"])
        self.assertIn("--action-id", command)
        self.assertIn("--run-revision", command)
        with patch(
            "omnipet.release.OpenAIImageGenerator", return_value=_Generator()
        ) as factory:
            result, stdout, stderr = self.call(["hatch", "cli-pet"])
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr), {"ok": False, "error": "hatch failed"})
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
