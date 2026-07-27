import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from omnipet.cli import main
from omnipet.generation import GeneratedImage
from omnipet.project import load_pet_project
from omnipet.run import prepare_run
from omnipet.workflow import mark_blocked


class CliGenerator:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = 0

    def generate(self, request):
        return self._write(request)

    def edit(self, request):
        return self._write(request)

    def _write(self, request):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider failed")
        size = (1024, 1024) if request.task == "base" else (1536, 1024)
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, "#00FF00").save(request.destination)
        import hashlib
        data = request.destination.read_bytes()
        return GeneratedImage(
            request.destination,
            "image/png",
            hashlib.sha256(data).hexdigest(),
            *size,
            {},
        )


class GuidedCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def call(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def use_legacy_workflow(self, pet_id):
        manifest = self.root / "pets" / pet_id / "pet.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "  agent_workflow_version: 2\n", ""
            ),
            encoding="utf-8",
        )

    def test_pet_init_and_status_are_public_commands(self):
        result, stdout, stderr = self.call(["pet", "init", "cli-pet", "--repo-root", str(self.root)])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["pet_id"], "cli-pet")
        result, stdout, stderr = self.call(["status", "cli-pet", "--repo-root", str(self.root)])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "preparing")

        with patch("omnipet.release.OpenAIImageGenerator", return_value=CliGenerator()):
            result, stdout, stderr = self.call(["hatch", "cli-pet", "--repo-root", str(self.root)])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "intake")

    def test_hatch_approve_and_qa_dispatch_and_sanitize_errors(self):
        self.call(["pet", "init", "cli-pet", "--repo-root", str(self.root)])
        verdict = self.root / "verdict.json"
        verdict.write_text('{"private":"must-not-leak"}')
        state = type("State", (), {"state": "generating_standard_rows"})()
        with patch("omnipet.cli.hatch_project", return_value=state), patch(
            "omnipet.cli.project_status",
            return_value={"ok": True, "workflow_state": state.state, "next_action": "run hatch", "blocked": None},
        ):
            result, stdout, stderr = self.call(["hatch", "cli-pet", "--repo-root", str(self.root)])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], state.state)

        with patch("omnipet.cli.approve_project_stage", side_effect=ValueError("private must-not-leak")):
            result, stdout, stderr = self.call(["approve", "cli-pet", "--stage", "base", "--repo-root", str(self.root)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(stderr), {"ok": False, "error": "approve failed"})
        self.assertNotIn("private", stderr)

    def test_hatch_reset_failed_dispatches_explicit_reset(self):
        self.call(["pet", "init", "cli-pet", "--repo-root", str(self.root)])
        state = type("State", (), {"state": "preparing"})()
        with patch("omnipet.cli.reset_failed_job", return_value=state) as reset, patch(
            "omnipet.cli.project_status",
            return_value={"ok": True, "workflow_state": "preparing", "next_action": "run hatch", "blocked": None},
        ):
            result, stdout, stderr = self.call([
                "hatch", "cli-pet", "--reset-failed", "base", "--repo-root", str(self.root)
            ])
        self.assertEqual((result, stderr), (0, ""))
        reset.assert_called_once()

        verdict = self.root / "verdict.json"
        verdict.write_text('{"private":"must-not-leak"}')
        with patch("omnipet.cli.qa_project_stage", side_effect=ValueError("private must-not-leak")) as qa:
            result, stdout, stderr = self.call([
                "qa", "cli-pet", "--stage", "directions", "--verdict-file", str(verdict),
                "--repo-root", str(self.root),
            ])
        self.assertEqual(result, 1)
        qa.assert_called_once()
        self.assertEqual(qa.call_args.kwargs["verdict_file"], verdict)
        self.assertNotIn("private", stderr)

    def test_failed_hatch_requires_real_cli_reset_before_another_attempt(self):
        self.call(["pet", "init", "cli-pet", "--repo-root", str(self.root)])
        generator = CliGenerator(fail=True)

        with patch("omnipet.release.OpenAIImageGenerator", return_value=generator):
            result, stdout, stderr = self.call([
                "hatch", "cli-pet", "--repo-root", str(self.root)
            ])
            self.assertEqual((result, stderr), (0, ""))
            self.assertEqual(json.loads(stdout)["workflow_state"], "intake")
            result, stdout, stderr = self.call([
                "hatch", "cli-pet", "--repo-root", str(self.root)
            ])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "intake")
        self.assertEqual(generator.calls, 0)

    def test_clear_block_and_reset_are_mutually_exclusive_and_sanitized(self):
        self.call(["pet", "init", "cli-pet", "--repo-root", str(self.root)])
        with self.assertRaises(SystemExit):
            self.call([
                "hatch", "cli-pet", "--clear-block", "--reset-failed", "base",
                "--repo-root", str(self.root),
            ])
        with patch("omnipet.cli.clear_project_block", side_effect=ValueError("private secret")):
            result, _stdout, stderr = self.call([
                "hatch", "cli-pet", "--clear-block", "--repo-root", str(self.root)
            ])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(stderr), {"ok": False, "error": "hatch failed"})

    def test_clear_block_command_clears_only_real_aggregate_block(self):
        self.call(["pet", "init", "cli-pet", "--repo-root", str(self.root)])
        self.use_legacy_workflow("cli-pet")
        project = load_pet_project(self.root, "cli-pet")
        run_dir = prepare_run(project, self.root).run_dir
        manifest_before = (run_dir / "imagegen-jobs.json").read_bytes()
        mark_blocked(run_dir, code="aggregate-qa-failed", job=None, evidence=None)

        result, stdout, stderr = self.call([
            "hatch", "cli-pet", "--clear-block", "--repo-root", str(self.root)
        ])

        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["workflow_state"], "preparing")
        self.assertEqual((run_dir / "imagegen-jobs.json").read_bytes(), manifest_before)

    def test_package_check_publish_and_package_qa_are_dispatched_and_sanitized(self):
        self.call(["pet", "init", "cli-pet", "--repo-root", str(self.root)])
        manifest = {"spriteVersionNumber": 2}
        with patch("omnipet.cli.check_package", return_value=manifest) as check, patch(
            "omnipet.cli.publish_package", return_value=(Path("pet.json"), Path("spritesheet.webp"))
        ) as publish, patch("omnipet.cli.recover_package", return_value=None) as recover:
            result, stdout, stderr = self.call(["package", "cli-pet", "--check", "--repo-root", str(self.root)])
            self.assertEqual((result, stderr), (0, ""))
            self.assertFalse(json.loads(stdout)["published"])
            publish.assert_not_called()
            result, stdout, stderr = self.call(["package", "cli-pet", "--repo-root", str(self.root)])
            self.assertEqual((result, stderr), (0, ""))
            self.assertTrue(json.loads(stdout)["published"])
            publish.assert_called_once()
            self.assertEqual(check.call_count, 2)
            result, stdout, stderr = self.call(["package", "cli-pet", "--recover", "--repo-root", str(self.root)])
            self.assertEqual((result, stderr), (0, ""))
            self.assertTrue(json.loads(stdout)["recovered"])
            recover.assert_called_once()

        verdict = self.root / "package-verdict.json"
        verdict.write_text('{"secret":"must-not-leak"}')
        state = type("State", (), {"state": "awaiting_package_approval"})()
        with patch("omnipet.cli.qa_project_stage", return_value=state) as qa, patch(
            "omnipet.cli.project_status", return_value={
                "ok": True, "workflow_state": state.state, "next_action": "review", "blocked": None,
            }
        ):
            result, stdout, stderr = self.call([
                "qa", "cli-pet", "--stage", "package", "--verdict-file", str(verdict),
                "--repo-root", str(self.root),
            ])
        self.assertEqual((result, stderr), (0, ""))
        qa.assert_called_once()

        with patch("omnipet.cli.qa_project_stage", side_effect=ValueError("secret must-not-leak")):
            result, _stdout, stderr = self.call([
                "qa", "cli-pet", "--stage", "package", "--verdict-file", str(verdict),
                "--repo-root", str(self.root),
            ])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(stderr), {"ok": False, "error": "qa failed"})
        self.assertNotIn("secret", stderr)

    def test_release_verify_dispatches_without_loading_a_project(self):
        bundle = self.root / "bundle"
        bundle.mkdir()
        with patch("omnipet.cli.verify_public_release", return_value={
            "petId": "public-pet", "version": "1.2.3",
        }) as verify, patch(
            "omnipet.cli.load_pet_project",
            side_effect=AssertionError("verify must be clean-room"),
        ) as load:
            result, stdout, stderr = self.call([
                "release", "verify", str(bundle),
            ])

        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {
            "ok": True,
            "pet_id": "public-pet",
            "verified": True,
            "version": "1.2.3",
        })
        verify.assert_called_once_with(bundle)
        load.assert_not_called()

    def test_release_export_loads_project_and_sanitizes_failures(self):
        self.call(["pet", "init", "cli-pet", "--repo-root", str(self.root)])
        destination = self.root / "release-work/cli-pet-0.1.0"
        destination.mkdir(parents=True)
        (destination / "release.json").write_text(
            '{"petId":"cli-pet","version":"0.1.0"}'
        )
        with patch(
            "omnipet.cli.export_public_release", return_value=destination
        ) as export:
            result, stdout, stderr = self.call([
                "release", "export", "cli-pet",
                "--repo-root", str(self.root),
                "--output", str(destination),
            ])
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["version"], "0.1.0")
        self.assertEqual(export.call_args.args[1], destination)

        with patch(
            "omnipet.cli.export_public_release",
            side_effect=ValueError("private secret"),
        ):
            result, _stdout, stderr = self.call([
                "release", "export", "cli-pet",
                "--repo-root", str(self.root),
                "--output", str(destination),
            ])
        self.assertEqual(result, 1)
        self.assertEqual(
            json.loads(stderr),
            {"ok": False, "error": "release export failed"},
        )
        self.assertNotIn("secret", stderr)

    def test_package_check_rejection_is_nonzero_and_read_only(self):
        self.call(["pet", "init", "cli-pet", "--repo-root", str(self.root)])
        self.use_legacy_workflow("cli-pet")
        project = load_pet_project(self.root, "cli-pet")
        run_dir = prepare_run(project, self.root).run_dir
        before = tuple((str(path.relative_to(self.root)), path.stat().st_mtime_ns, path.read_bytes()) for path in sorted(self.root.rglob("*")) if path.is_file())
        with patch("omnipet.cli.check_package", side_effect=ValueError("pending row private")):
            result, stdout, stderr = self.call(["package", "cli-pet", "--check", "--repo-root", str(self.root)])
        after = tuple((str(path.relative_to(self.root)), path.stat().st_mtime_ns, path.read_bytes()) for path in sorted(self.root.rglob("*")) if path.is_file())
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr), {"ok": False, "error": "package failed"})
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
