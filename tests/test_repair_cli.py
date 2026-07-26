import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from omnipet.cli import main
from omnipet.repair import RepairResult
from omnipet.release import init_pet_project


class RepairCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        init_pet_project(self.root, "my-pet")

    def test_repair_returns_only_sanitized_identifiers_and_relative_archive(self):
        result = RepairResult(
            archive_path=".omnipet/archives/repairs/my-pet-idle-20260723T000000000000Z",
            repaired_job="idle",
            invalidated_jobs=("look-cardinals", "look-row-9", "look-row-10"),
            invalidated_stages=("standard-rows", "directions", "package", "delivery"),
        )
        stdout = io.StringIO()
        with patch("omnipet.cli.repair_project_job", return_value=result), redirect_stdout(stdout):
            code = main([
                "repair", "my-pet", "--job", "idle",
                "--reason", "Pose mismatch found in review.",
                "--repo-root", str(self.root),
            ])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(set(payload), {
            "ok", "pet_id", "repaired_job", "invalidated_jobs",
            "invalidated_stages", "archive",
        })
        self.assertEqual(payload["archive"], result.archive_path)
        self.assertNotIn("reason", payload)

    def test_repair_failure_is_fixed_and_does_not_echo_secret_inputs(self):
        stderr = io.StringIO()
        secret = "Authorization: Bearer must-not-echo"
        with redirect_stderr(stderr):
            code = main([
                "repair", "my-pet", "--job", "idle", "--reason", secret,
                "--repo-root", str(self.root),
            ])
        payload = json.loads(stderr.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload, {"ok": False, "error": "repair failed"})
        self.assertNotIn(secret, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
