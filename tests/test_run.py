import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageOps

from omnipet.canvas import Canvas, canvas_for_job, validate_job_canvas
from omnipet.cli import main
from omnipet.project import load_pet_project
from omnipet.run import (
    EXPECTED_DEPENDENCIES,
    EXPECTED_JOB_IDS,
    RunPreparationError,
    load_run_state,
    prepare_run,
)


JOB_IDS = EXPECTED_JOB_IDS
JOB_KINDS = {
    job_id: "base-pet" if job_id == "base" else (
        "look-cardinals" if job_id == "look-cardinals" else
        "look-row-strip" if job_id.startswith("look-row") else "row-strip"
    )
    for job_id in JOB_IDS
}


class CanvasPolicyTests(unittest.TestCase):
    def test_canvas_policy_covers_all_authoritative_jobs(self):
        kinds = {
            job_id: "base-pet" if job_id == "base" else (
                "look-cardinals" if job_id == "look-cardinals" else
                "look-row-strip" if job_id.startswith("look-row") else "row-strip"
            )
            for job_id in EXPECTED_JOB_IDS
        }
        expected = {
            job_id: Canvas("1:1", "1K") if job_id == "base" else Canvas("21:9", "2K")
            for job_id in EXPECTED_JOB_IDS
        }

        self.assertEqual(
            {job_id: canvas_for_job(job_id, kinds[job_id]) for job_id in EXPECTED_JOB_IDS},
            expected,
        )

    def test_canvas_validation_rejects_inconsistent_values(self):
        valid = {
            "id": "idle",
            "kind": "row-strip",
            "canvas": {"aspect_ratio": "21:9", "image_size": "2K"},
        }
        self.assertEqual(validate_job_canvas(valid), Canvas("21:9", "2K"))
        for canvas in ({}, {"aspect_ratio": "1:1", "image_size": "1K"}, []):
            with self.subTest(canvas=canvas):
                with self.assertRaises(ValueError):
                    validate_job_canvas({**valid, "canvas": canvas})


class RunPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "pet"
        (self.root / "references").mkdir(parents=True)
        (self.root / "approved").mkdir()
        (self.root / "brief.md").write_text("# Ember\n", encoding="utf-8")
        Image.new("RGB", (2, 2), "red").save(self.root / "references" / "portrait.png")
        Image.new("RGBA", (2, 2), (1, 2, 3, 255)).save(
            self.root / "approved" / "canonical-base.png"
        )
        (self.root / "pet.yaml").write_text(
            """schema_version: 1
id: ember
display_name: Ember
description: A portable test pet.
brief: brief.md
style:
  preset: pixel
references:
  - path: references/portrait.png
    role: identity reference
image_generation:
  model: gpt-image-2
  quality: low
hatch_engine:
  minimum_sprite_version: 2
package:
  spritesheet: dist/spritesheet.webp
  manifest: dist/pet.json
approved:
  canonical_base: approved/canonical-base.png
""",
            encoding="utf-8",
        )
        self.project = load_pet_project(self.root, ".")

    def test_prepare_creates_and_resumes_builtin_run(self):
        first = prepare_run(self.project, self.root)
        marker = first.run_dir / "keep-me.txt"
        marker.write_text("unchanged", encoding="utf-8")

        second = prepare_run(self.project, self.root)

        self.assertEqual(first.job_ids, EXPECTED_JOB_IDS)
        self.assertEqual(second.run_dir, first.run_dir)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
        manifest = json.loads((first.run_dir / "imagegen-jobs.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["jobs"][10]["kind"], "look-cardinals")
        self.assertEqual(
            [job["canvas"] for job in manifest["jobs"]],
            [{"aspect_ratio": "1:1", "image_size": "1K"}]
            + [{"aspect_ratio": "21:9", "image_size": "2K"}] * 12,
        )

    def test_prepare_failure_is_sanitized_and_removes_partial_run(self):
        secret = "private-runtime-detail"
        with patch("omnipet.run.prepare_hatch_run", side_effect=OSError(secret)):
            with self.assertRaises(RunPreparationError) as raised:
                prepare_run(self.project, self.root)

        self.assertNotIn(secret, str(raised.exception))
        self.assertFalse((self.root / ".omnipet" / "runs" / "ember").exists())

    def test_status_preserves_dependency_frontier_and_stages(self):
        run_dir = self.root / ".omnipet" / "runs" / "ember"
        self._write_manifest(run_dir, {"base"})

        state = load_run_state(self.root, "ember", display_name="Ember")

        self.assertEqual(state.counts, {
            "total": 13,
            "complete": 1,
            "ready": 2,
            "pending": 10,
            "running": 0,
            "failed": 0,
        })
        self.assertEqual(
            [stage.status for stage in state.stages],
            ["complete", "complete", "active", "pending"],
        )

    def test_status_rejects_dependency_violation_and_corrupt_artifact(self):
        run_dir = self.root / ".omnipet" / "runs" / "ember"
        self._write_manifest(run_dir, {"idle"})
        with self.assertRaises(RunPreparationError):
            load_run_state(self.root, "ember")

    def test_run_paths_and_completed_artifacts_reject_symlink_escapes(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        marker = outside / "marker"
        marker.write_text("unchanged", encoding="utf-8")
        (self.root / ".omnipet").mkdir()
        (self.root / ".omnipet" / "runs").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RunPreparationError):
            prepare_run(self.project, self.root)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

        (self.root / ".omnipet" / "runs").unlink()
        run_dir = self.root / ".omnipet" / "runs" / "ember"
        self._write_manifest(run_dir, {"base"})
        output = run_dir / "decoded" / "base.png"
        output.unlink()
        output.symlink_to(self.root / "approved" / "canonical-base.png")
        with self.assertRaises(RunPreparationError):
            load_run_state(self.root, "ember")

    def test_status_rejects_escaping_output_source_mismatch_and_malformed_status(self):
        run_dir = self.root / ".omnipet" / "runs" / "ember"
        for mutation in ("escape", "mismatch", "relative-source", "bad-status", "bad-dependency"):
            with self.subTest(mutation=mutation):
                shutil.rmtree(run_dir, ignore_errors=True)
                self._write_manifest(run_dir, {"base"})
                manifest_path = run_dir / "imagegen-jobs.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if mutation == "escape":
                    manifest["jobs"][0]["output_path"] = "../outside.png"
                elif mutation == "mismatch":
                    Image.new("RGBA", (1, 1), (9, 8, 7, 255)).save(run_dir / "decoded" / "base.png")
                elif mutation == "relative-source":
                    manifest["jobs"][0]["source_path"] = "generated-sources/base.png"
                elif mutation == "bad-status":
                    manifest["jobs"][0]["status"] = "ready"
                else:
                    manifest["jobs"][1]["depends_on"] = ["idle"]
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(RunPreparationError):
                    load_run_state(self.root, "ember")

    def test_prepare_resume_rejects_reference_tamper_without_mutation(self):
        state = prepare_run(self.project, self.root)
        copied = state.run_dir / "references" / "reference-01.png"
        copied.write_bytes(b"tampered")
        marker = state.run_dir / "marker"
        marker.write_text("unchanged", encoding="utf-8")

        with self.assertRaises(RunPreparationError):
            prepare_run(self.project, self.root)

        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_running_left_requires_exact_framewise_mirror_provenance(self):
        run_dir = self.root / ".omnipet" / "runs" / "ember"
        self._write_approved_running_left_mirror(run_dir)
        self.assertEqual(load_run_state(self.root, "ember").counts["complete"], 4)

        right_path = run_dir / "decoded" / "running-right.png"
        with Image.open(right_path) as right:
            ImageOps.mirror(right).save(run_dir / "decoded" / "running-left.png")
        with self.assertRaises(RunPreparationError):
            load_run_state(self.root, "ember")

        self._write_manifest(run_dir, {"base"})
        (run_dir / "decoded" / "base.png").write_bytes(b"corrupt")
        with self.assertRaises(RunPreparationError):
            load_run_state(self.root, "ember")

    def test_cli_prepare_status_and_errors_are_sanitized(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["run", "prepare", ".", "--repo-root", str(self.root)]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["counts"]["total"], 13)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["run", "status", ".", "--repo-root", str(self.root)]), 0)
        self.assertEqual(len(json.loads(stdout.getvalue())["stages"]), 4)

        stderr = io.StringIO()
        with patch("omnipet.cli.prepare_run", side_effect=RunPreparationError("private")), redirect_stderr(stderr):
            self.assertEqual(main(["run", "prepare", ".", "--repo-root", str(self.root)]), 1)
        self.assertEqual(json.loads(stderr.getvalue()), {"ok": False, "error": "run preparation failed"})

    def _write_manifest(self, run_dir, complete):
        jobs = []
        for job_id in EXPECTED_JOB_IDS:
            kind = "base-pet" if job_id == "base" else (
                "look-cardinals" if job_id == "look-cardinals" else
                "look-row-strip" if job_id.startswith("look-row") else "row-strip"
            )
            canvas = canvas_for_job(job_id, kind)
            job = {
                "id": job_id,
                "kind": kind,
                "status": "complete" if job_id in complete else "pending",
                "depends_on": list(EXPECTED_DEPENDENCIES[job_id]),
                "output_path": f"decoded/{job_id}.png",
                "canvas": {"aspect_ratio": canvas.aspect_ratio, "image_size": canvas.image_size},
            }
            if job_id in complete:
                output = run_dir / "decoded" / f"{job_id}.png"
                source = run_dir / "generated-sources" / f"{job_id}.png"
                output.parent.mkdir(parents=True, exist_ok=True)
                source.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (1, 1), (1, 2, 3, 255)).save(output)
                source.write_bytes(output.read_bytes())
                job.update(source_path=str(source), completed_at="2026-07-22T00:00:00+00:00")
            jobs.append(job)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "imagegen-jobs.json").write_text(
            json.dumps({"schema_version": 1, "run_dir": str(run_dir), "jobs": jobs}),
            encoding="utf-8",
        )

    def _write_approved_running_left_mirror(self, run_dir):
        complete = {"base", "idle", "running-right", "running-left"}
        self._write_manifest(run_dir, complete)
        right_path = run_dir / "decoded" / "running-right.png"
        right = Image.new("RGBA", (16, 2))
        for slot in range(8):
            right.putpixel((slot * 2, 0), (slot, 10, 20, 255))
            right.putpixel((slot * 2 + 1, 0), (slot, 30, 40, 255))
        right.save(right_path)
        shutil.copyfile(right_path, run_dir / "generated-sources" / "running-right.png")
        left = Image.new("RGBA", right.size)
        for slot in range(8):
            box = (slot * 2, 0, (slot + 1) * 2, right.height)
            left.alpha_composite(ImageOps.mirror(right.crop(box)), (slot * 2, 0))
        left.save(run_dir / "decoded" / "running-left.png")
        manifest_path = run_dir / "imagegen-jobs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        job = next(item for item in manifest["jobs"] if item["id"] == "running-left")
        job.update({
            "source_path": "decoded/running-right.png",
            "derived_from": "running-right",
            "mirror_policy": {
                "may_derive": True,
                "may_derive_from": "running-right",
                "derivation": "framewise-horizontal-mirror-preserving-order",
                "requires_explicit_approval": True,
            },
            "mirror_decision": {
                "approved": True,
                "transform": "framewise-horizontal-mirror-preserving-order",
                "note": "Symmetric design is safe to mirror.",
                "approved_at": job["completed_at"],
            },
            "metadata": {"width": 16, "height": 2, "mode": "RGBA", "format": "PNG"},
        })
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
