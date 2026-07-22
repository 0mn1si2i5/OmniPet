import io
import hashlib
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from omnipet.canvas import canvas_for_job
from omnipet.checkpoint import (
    CheckpointError,
    _normalized_evidence_bytes,
    export_checkpoint,
    restore_checkpoint,
)
from omnipet.cli import main
from omnipet.project import load_pet_project
from omnipet.release import (
    approve_project_stage,
    hatch_project,
    project_status,
    qa_project_stage,
)
from omnipet.run import EXPECTED_DEPENDENCIES, EXPECTED_JOB_IDS, STANDARD_JOB_IDS, load_run_state
from omnipet.approvals import load_approvals
from tests.test_release_workflow import FakeGenerator


class PortableCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "ember-pet"
        self._write_project(self.root)
        self.project = load_pet_project(self.root, ".")
        self.run_dir = self.root / ".omnipet" / "runs" / "ember"
        self._write_run(self.run_dir, {"base", "idle"})

    def test_export_and_clean_clone_restore_base_and_idle_actionable_state(self):
        checkpoint = export_checkpoint(self.project)
        manifest = json.loads((checkpoint / "checkpoint.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["pet_id"], "ember")
        self.assertEqual(manifest["sprite_version"], 2)
        self.assertEqual(manifest["completed_jobs"], ["base", "idle"])
        self.assertIn(
            {"job_id": "base", "role": "canonical"},
            [{"job_id": item["job_id"], "role": item["role"]} for item in manifest["artifacts"]],
        )
        self.assertTrue(all(not Path(item["path"]).is_absolute() for item in manifest["artifacts"]))
        self.assertNotIn(str(self.root), json.dumps(manifest))

        clone = self.root.parent / "clean-clone"
        shutil.copytree(self.root, clone, ignore=shutil.ignore_patterns(".omnipet"))
        clone_project = load_pet_project(clone, ".")

        with patch("omnipet.checkpoint.prepare_run", side_effect=self._fake_prepare):
            state = restore_checkpoint(clone_project)

        self.assertEqual(state.counts["complete"], 2)
        self.assertEqual(state.counts["ready"], 1)
        self.assertEqual(load_run_state(clone, "ember").counts, state.counts)
        restored = json.loads((state.run_dir / "imagegen-jobs.json").read_text(encoding="utf-8"))
        self.assertEqual([job["status"] for job in restored["jobs"][:3]], ["complete", "complete", "pending"])

    def test_guided_base_approval_roundtrips_without_regenerating_base(self):
        shutil.rmtree(self.root / ".omnipet")
        pet_yaml = self.root / "pet.yaml"
        pet_yaml.write_text(
            pet_yaml.read_text(encoding="utf-8").replace(
                "approved:\n  canonical_base: approved/canonical-base.png\n", ""
            ).replace(
                "references:\n  - path: references/portrait.png\n    role: identity reference\n",
                "references: []\n",
            ),
            encoding="utf-8",
        )
        (self.root / "approved/canonical-base.png").unlink()
        project = load_pet_project(self.root, ".")
        generator = FakeGenerator()
        hatch_project(project, generator_factory=lambda _project: generator)
        approve_project_stage(project, "base", note="identity accepted")
        project = load_pet_project(self.root, ".")

        checkpoint = export_checkpoint(project)
        payload = json.loads((checkpoint / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertIn("qa/base/review.json", [item["path"] for item in payload["accepted_qa"]])
        self.assertNotIn(str(self.root), json.dumps(payload))

        shutil.rmtree(self.root / ".omnipet")
        restored = restore_checkpoint(project)

        self.assertEqual(project_status(project)["workflow_state"], "generating_standard_rows")
        self.assertTrue((restored.run_dir / "qa/base/review.json").is_file())
        resumed_generator = FakeGenerator()
        state = hatch_project(project, generator_factory=lambda _project: resumed_generator)
        self.assertEqual(state.state, "generating_standard_rows")
        self.assertNotIn("base", resumed_generator.calls)
        self.assertEqual(
            project_status(project)["next_action"],
            "omnipet qa . --stage standard-rows --verdict-file standard-verdict.json",
        )

    def test_guided_standard_approval_roundtrips_with_portable_qa(self):
        shutil.rmtree(self.root / ".omnipet")
        pet_yaml = self.root / "pet.yaml"
        pet_yaml.write_text(
            pet_yaml.read_text(encoding="utf-8").replace(
                "references:\n  - path: references/portrait.png\n    role: identity reference\n",
                "references: []\n",
            ),
            encoding="utf-8",
        )
        project = load_pet_project(self.root, ".")
        generator = FakeGenerator()
        hatch_project(project, generator_factory=lambda _project: generator)
        approve_project_stage(project, "base")
        hatch_project(project, generator_factory=lambda _project: generator)
        verdict = self.root / "standard-verdict.json"
        verdict.write_text(json.dumps({
            "schema_version": 1,
            "stage": "standard-rows",
            "rows": [{
                "id": job_id,
                "verdict": "pass",
                "note": "identity and motion accepted",
                "evidence": self._bound_evidence(project.repository_root / ".omnipet/runs/ember", (
                    f"generated-sources/{job_id}.png",
                    f"decoded/{job_id}.png",
                    f"qa/rows/{job_id}/deterministic.json",
                    f"previews/{job_id}.gif",
                )),
            } for job_id in STANDARD_JOB_IDS],
        }), encoding="utf-8")
        qa_project_stage(project, "standard-rows", verdict_file=verdict)
        approve_project_stage(project, "standard-rows")

        checkpoint = export_checkpoint(project)
        report = json.loads((checkpoint / "qa/standard/review.json").read_text(encoding="utf-8"))
        self.assertEqual(report["frames_root"], "frames")
        self.assertNotIn(str(self.root), (checkpoint / "qa/standard/review.json").read_text())

        shutil.rmtree(self.root / ".omnipet")
        restored = restore_checkpoint(project)

        self.assertEqual([record.stage for record in load_approvals(restored.run_dir)], ["base", "standard-rows"])
        self.assertEqual(project_status(project)["workflow_state"], "generating_directions")

    def test_evidence_normalization_handles_nested_json_and_markdown_and_rejects_outside_paths(self):
        inside = self.run_dir / "qa/rows/idle/frames"
        outside = self.root.parent / "outside.png"
        json_source = self.run_dir / "nested.json"
        json_source.write_text(json.dumps({"nested": [str(inside)]}), encoding="utf-8")
        markdown_source = self.run_dir / "notes.md"
        markdown_source.write_text(f"Inspect `{inside}` before approval.\n", encoding="utf-8")

        self.assertEqual(
            json.loads(_normalized_evidence_bytes(self.run_dir, json_source)),
            {"nested": ["qa/rows/idle/frames"]},
        )
        self.assertEqual(
            _normalized_evidence_bytes(self.run_dir, markdown_source).decode(),
            "Inspect `qa/rows/idle/frames` before approval.\n",
        )

        json_source.write_text(json.dumps({"path": str(outside)}), encoding="utf-8")
        with self.assertRaises(ValueError):
            _normalized_evidence_bytes(self.run_dir, json_source)
        markdown_source.write_text(f"Inspect `{outside}`.\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            _normalized_evidence_bytes(self.run_dir, markdown_source)

    def test_export_refuses_invalid_completed_artifact_and_preserves_absent_destination(self):
        (self.run_dir / "decoded" / "idle.png").write_bytes(b"not a png")

        with self.assertRaises(CheckpointError):
            export_checkpoint(self.project)

        self.assertFalse((self.root / "checkpoint").exists())

    def test_export_keeps_sanitized_accepted_qa_without_runtime_paths(self):
        visual = self.run_dir / "qa" / "visual-jobs"
        visual.mkdir()
        (visual / "idle.result.json").write_text(json.dumps({
            "ok": True,
            "job_id": "idle",
            "source_path": str(self.run_dir / "generated-sources" / "idle.png"),
            "prompt_file": str(self.run_dir / "prompts" / "idle.md"),
            "attempts": 2,
            "completed_at": "2026-07-21T00:00:00+00:00",
            "sha256": "a" * 64,
        }), encoding="utf-8")

        checkpoint = export_checkpoint(self.project)
        manifest = json.loads((checkpoint / "checkpoint.json").read_text(encoding="utf-8"))
        qa_path = checkpoint / manifest["accepted_qa"][0]["path"]
        qa = json.loads(qa_path.read_text(encoding="utf-8"))

        self.assertEqual(qa, {
            "ok": True,
            "job_id": "idle",
            "completed_at": "2026-07-21T00:00:00+00:00",
            "sha256": "a" * 64,
        })

    def test_export_rejects_secret_text_values_but_allows_normal_notes(self):
        manifest_path = self.run_dir / "imagegen-jobs.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        secret_values = (
            "Bearer abc.def-123",
            "sk-proj-private123456",
            "api_key = private-value",
            "Authorization: private-value",
            "access_token=private-value",
        )
        for secret in secret_values:
            with self.subTest(secret=secret):
                shutil.rmtree(self.root / "checkpoint", ignore_errors=True)
                manifest = json.loads(json.dumps(original))
                manifest["jobs"][0]["adoption_decision"] = secret
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(CheckpointError):
                    export_checkpoint(self.project)
                self.assertFalse((self.root / "checkpoint").exists())

        manifest = json.loads(json.dumps(original))
        manifest["jobs"][0]["adoption_decision"] = (
            "The token animation settles naturally; authorization pose approved."
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(export_checkpoint(self.project), self.root / "checkpoint")

    def test_export_rejects_secret_text_in_selected_qa_and_bounds_scanning(self):
        visual = self.run_dir / "qa" / "visual-jobs"
        visual.mkdir()
        result = {
            "ok": True,
            "job_id": "idle",
            "adoption_decision": "Bearer qa-private-token",
        }
        (visual / "idle.result.json").write_text(json.dumps(result), encoding="utf-8")

        with self.assertRaises(CheckpointError):
            export_checkpoint(self.project)

        result["adoption_decision"] = "ordinary accepted QA note " + ("x" * 1_100_000)
        (visual / "idle.result.json").write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            export_checkpoint(self.project)
        self.assertFalse((self.root / "checkpoint").exists())

    def test_export_bounds_recursive_metadata_scanning(self):
        visual = self.run_dir / "qa" / "visual-jobs"
        visual.mkdir()
        (visual / "idle.result.json").write_text(json.dumps({
            "ok": True,
            "job_id": "idle",
            "metadata": {"width": ["ordinary note"] * 10_001},
        }), encoding="utf-8")

        with self.assertRaises(CheckpointError):
            export_checkpoint(self.project)

        self.assertFalse((self.root / "checkpoint").exists())

    def test_export_uses_pet_root_in_legacy_monorepo(self):
        monorepo = self.root.parent / "monorepo"
        pet_root = monorepo / "pets" / "ember"
        shutil.copytree(self.root, pet_root, ignore=shutil.ignore_patterns(".omnipet"))
        shutil.copytree(self.run_dir, monorepo / ".omnipet" / "runs" / "ember")
        manifest_path = monorepo / ".omnipet" / "runs" / "ember" / "imagegen-jobs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        old_run = Path(manifest["run_dir"])
        new_run = manifest_path.parent
        manifest["run_dir"] = str(new_run)
        for job in manifest["jobs"]:
            if isinstance(job.get("source_path"), str) and Path(job["source_path"]).is_absolute():
                job["source_path"] = str(new_run / Path(job["source_path"]).relative_to(old_run))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        checkpoint = export_checkpoint(load_pet_project(monorepo, "ember"))

        self.assertEqual(checkpoint, pet_root / "checkpoint")
        self.assertFalse((monorepo / "checkpoint").exists())

    def test_export_is_atomic_when_copy_fails(self):
        with patch("omnipet.checkpoint.shutil.copyfile", side_effect=OSError("failed")):
            with self.assertRaises(CheckpointError):
                export_checkpoint(self.project)

        self.assertFalse((self.root / "checkpoint").exists())
        self.assertEqual(list(self.root.glob(".checkpoint-*")), [])

    def test_export_refuses_overwrite_without_force(self):
        export_checkpoint(self.project)
        marker = self.root / "checkpoint" / "marker"
        marker.write_text("old", encoding="utf-8")

        with self.assertRaises(CheckpointError):
            export_checkpoint(self.project)
        export_checkpoint(self.project, force=True)

        self.assertFalse(marker.exists())

    def test_restore_rejects_tamper_unknown_fields_traversal_and_symlinks(self):
        export_checkpoint(self.project)
        manifest_path = self.root / "checkpoint" / "checkpoint.json"
        original = manifest_path.read_text(encoding="utf-8")
        mutations = ("hash", "unknown", "traversal", "symlink")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                manifest_path.write_text(original, encoding="utf-8")
                artifact = self.root / "checkpoint" / "artifacts" / "decoded" / "base.png"
                if artifact.is_symlink():
                    artifact.unlink()
                    Image.new("RGBA", (1, 1), (1, 2, 3, 255)).save(artifact, "PNG")
                data = json.loads(original)
                if mutation == "hash":
                    artifact.write_bytes(b"tampered")
                elif mutation == "unknown":
                    data["unexpected"] = True
                    manifest_path.write_text(json.dumps(data), encoding="utf-8")
                elif mutation == "traversal":
                    data["artifacts"][0]["path"] = "../outside.png"
                    manifest_path.write_text(json.dumps(data), encoding="utf-8")
                else:
                    outside = self.root / "outside.png"
                    shutil.copyfile(artifact, outside)
                    artifact.unlink()
                    artifact.symlink_to(outside)

                shutil.rmtree(self.root / ".omnipet", ignore_errors=True)
                with patch("omnipet.checkpoint.prepare_run") as prepare:
                    with self.assertRaises(CheckpointError):
                        restore_checkpoint(self.project)
                prepare.assert_not_called()
                self.assertFalse((self.root / ".omnipet").exists())

                if mutation == "hash":
                    Image.new("RGBA", (1, 1), (1, 2, 3, 255)).save(artifact, "PNG")

    def test_restore_rejects_unknown_nested_provenance_fields(self):
        export_checkpoint(self.project)
        manifest_path = self.root / "checkpoint" / "checkpoint.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provenance"][0]["metadata"] = {"unknown": "value"}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        shutil.rmtree(self.root / ".omnipet")

        with patch("omnipet.checkpoint.prepare_run") as prepare:
            with self.assertRaises(CheckpointError):
                restore_checkpoint(self.project)

        prepare.assert_not_called()

    def test_restore_rejects_tampered_status_frontier(self):
        export_checkpoint(self.project)
        manifest_path = self.root / "checkpoint" / "checkpoint.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status_frontier"] = ["look-row-10"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        shutil.rmtree(self.root / ".omnipet")

        with patch("omnipet.checkpoint.prepare_run") as prepare:
            with self.assertRaises(CheckpointError):
                restore_checkpoint(self.project)

        prepare.assert_not_called()

    def test_restore_rejects_checkpoint_canonical_that_differs_from_durable_project(self):
        export_checkpoint(self.project)
        canonical = self.root / "checkpoint" / "artifacts" / "canonical" / "base.png"
        Image.new("RGBA", (1, 1), (9, 8, 7, 255)).save(canonical, "PNG")
        self._update_checkpoint_hash(canonical)
        shutil.rmtree(self.root / ".omnipet")

        with patch("omnipet.checkpoint.prepare_run") as prepare:
            with self.assertRaises(CheckpointError):
                restore_checkpoint(self.project)

        prepare.assert_not_called()
        self.assertFalse((self.root / ".omnipet").exists())

    def test_export_requires_current_durable_canonical(self):
        pet_yaml = self.root / "pet.yaml"
        pet_yaml.write_text(
            pet_yaml.read_text(encoding="utf-8").replace(
                "approved:\n  canonical_base: approved/canonical-base.png\n", ""
            ),
            encoding="utf-8",
        )
        project = load_pet_project(self.root, ".")

        with self.assertRaises(CheckpointError):
            export_checkpoint(project)

        self.assertFalse((self.root / "checkpoint").exists())

    def test_restore_rejects_base_decoded_or_source_inconsistent_with_canonical(self):
        for role in ("decoded", "source"):
            with self.subTest(role=role):
                shutil.rmtree(self.root / "checkpoint", ignore_errors=True)
                export_checkpoint(self.project)
                artifact = self.root / "checkpoint" / "artifacts" / role / "base.png"
                Image.new("RGBA", (1, 1), (9, 8, 7, 255)).save(artifact, "PNG")
                self._update_checkpoint_hash(artifact)
                shutil.rmtree(self.root / ".omnipet", ignore_errors=True)

                with patch("omnipet.checkpoint.prepare_run") as prepare:
                    with self.assertRaises(CheckpointError):
                        restore_checkpoint(self.project)

                prepare.assert_not_called()
                self._write_run(self.run_dir, {"base", "idle"})

    def test_restore_refuses_existing_run_unless_force(self):
        export_checkpoint(self.project)

        with self.assertRaises(CheckpointError):
            restore_checkpoint(self.project)
        with patch("omnipet.checkpoint.prepare_run", side_effect=self._fake_prepare):
            state = restore_checkpoint(self.project, force=True)

        self.assertEqual(state.counts["complete"], 2)
        archives = self.root / ".omnipet" / "archives"
        self.assertEqual(len(list(archives.glob("ember-checkpoint-restore-*"))), 1)

    def test_force_restore_rejects_external_runtime_chain_symlinks_without_mutation(self):
        export_checkpoint(self.project)
        for component in ("omnipet", "runs", "run", "archives"):
            with self.subTest(component=component):
                runtime = self.root / ".omnipet"
                if runtime.is_symlink():
                    runtime.unlink()
                else:
                    shutil.rmtree(runtime, ignore_errors=True)
                self._write_run(self.run_dir, {"base", "idle"})
                outside = self.root.parent / f"outside-{component}"
                shutil.rmtree(outside, ignore_errors=True)
                outside.mkdir()
                marker = outside / "marker.txt"
                marker.write_text("unchanged", encoding="utf-8")
                if component == "omnipet":
                    shutil.rmtree(self.root / ".omnipet")
                    (self.root / ".omnipet").symlink_to(outside, target_is_directory=True)
                elif component == "runs":
                    shutil.rmtree(self.root / ".omnipet" / "runs")
                    (self.root / ".omnipet" / "runs").symlink_to(outside, target_is_directory=True)
                elif component == "run":
                    shutil.rmtree(self.run_dir)
                    self.run_dir.symlink_to(outside, target_is_directory=True)
                else:
                    shutil.rmtree(self.root / ".omnipet" / "archives", ignore_errors=True)
                    (self.root / ".omnipet" / "archives").symlink_to(
                        outside, target_is_directory=True
                    )

                with patch("omnipet.checkpoint.prepare_run") as prepare:
                    with self.assertRaises(CheckpointError):
                        restore_checkpoint(self.project, force=True)

                prepare.assert_not_called()
                self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
                self.assertEqual(tuple(outside.iterdir()), (marker,))

    def test_force_export_cleanup_failure_keeps_published_checkpoint_and_allows_retry(self):
        export_checkpoint(self.project)
        original_rmtree = shutil.rmtree

        def fail_backup_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".checkpoint-backup-"):
                raise OSError("cleanup failed")
            return original_rmtree(path, *args, **kwargs)

        with patch("omnipet.checkpoint.shutil.rmtree", side_effect=fail_backup_cleanup):
            checkpoint = export_checkpoint(self.project, force=True)

        self.assertTrue((checkpoint / "checkpoint.json").is_file())
        backups = list(self.root.glob(".checkpoint-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "checkpoint.json").is_file())

        self.assertEqual(export_checkpoint(self.project, force=True), checkpoint)
        self.assertEqual(list(self.root.glob(".checkpoint-backup-*")), [])

    def test_cli_exports_and_restores_checkpoint(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = main(["checkpoint", "export", ".", "--repo-root", str(self.root)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["completed_jobs"], ["base", "idle"])

        shutil.rmtree(self.root / ".omnipet")
        stdout = io.StringIO()
        with patch("omnipet.checkpoint.prepare_run", side_effect=self._fake_prepare), redirect_stdout(stdout):
            result = main(["checkpoint", "restore", ".", "--repo-root", str(self.root)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["counts"]["complete"], 2)

    def test_cli_checkpoint_errors_are_sanitized(self):
        stderr = io.StringIO()
        with patch("omnipet.cli.export_checkpoint", side_effect=CheckpointError("private")), redirect_stderr(stderr):
            result = main(["checkpoint", "export", ".", "--repo-root", str(self.root)])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(stderr.getvalue()), {"ok": False, "error": "checkpoint export failed"})

    def _write_project(self, root):
        (root / "references").mkdir(parents=True)
        (root / "approved").mkdir()
        (root / "brief.md").write_text("# Ember\n", encoding="utf-8")
        (root / "references" / "portrait.png").write_bytes(b"portrait")
        Image.new("RGBA", (1, 1), (1, 2, 3, 255)).save(root / "approved" / "canonical-base.png", "PNG")
        (root / "pet.yaml").write_text("""schema_version: 1
id: ember
display_name: Ember
description: A portable test pet.
brief: brief.md
style:
  preset: illustrated
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
""", encoding="utf-8")

    def _write_run(self, run_dir, completed):
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
                "status": "complete" if job_id in completed else "pending",
                "depends_on": list(EXPECTED_DEPENDENCIES[job_id]),
                "output_path": f"decoded/{job_id}.png",
                "canvas": {"aspect_ratio": canvas.aspect_ratio, "image_size": canvas.image_size},
            }
            if job_id in completed:
                output = run_dir / "decoded" / f"{job_id}.png"
                source = run_dir / "generated-sources" / f"{job_id}.png"
                output.parent.mkdir(parents=True, exist_ok=True)
                source.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (1, 1), (1, 2, 3, 255)).save(output, "PNG")
                shutil.copyfile(output, source)
                job.update(source_path=str(source), completed_at="2026-07-21T00:00:00+00:00")
            jobs.append(job)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "imagegen-jobs.json").write_text(
            json.dumps({"schema_version": 1, "run_dir": str(run_dir), "jobs": jobs}), encoding="utf-8"
        )
        qa = run_dir / "qa"
        qa.mkdir()

    def _fake_prepare(self, project, repo_root):
        run_dir = Path(repo_root) / ".omnipet" / "runs" / project.pet_id
        self._write_run(run_dir, set())
        return load_run_state(Path(repo_root), project.pet_id)

    def _update_checkpoint_hash(self, artifact):
        manifest_path = self.root / "checkpoint" / "checkpoint.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        relative = str(artifact.relative_to(self.root / "checkpoint"))
        for item in manifest["artifacts"]:
            if item["path"] == relative:
                item["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
                break
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def _bound_evidence(self, run_dir, relatives):
        return [{
            "path": relative,
            "sha256": hashlib.sha256((run_dir / relative).read_bytes()).hexdigest(),
        } for relative in relatives]


if __name__ == "__main__":
    unittest.main()
