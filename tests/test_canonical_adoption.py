import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from omnipet.cli import main
from omnipet.project import load_pet_project
from omnipet.run import RunPreparationError, adopt_canonical, load_run_state, prepare_run

from tests.test_project import VALID_PET_YAML
from tests.test_run import JOB_IDS, JOB_KINDS


class CanonicalAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_root = Path(self.temp_dir.name).resolve() / "repo"
        pet_root = self.repo_root / "pets" / "sample-pet"
        (pet_root / "references").mkdir(parents=True)
        (pet_root / "approved").mkdir()
        (pet_root / "brief.md").write_text(
            "# Sample Pet\n\n## Identity Lock\n\n"
            "One writing brush is secured in a fixed dark loop at the character's right waist. "
            "The right hand may rest on, steady, or release the secured brush as the state requires.\n",
            encoding="utf-8",
        )
        (pet_root / "README.md").write_text("# Sample Pet\n", encoding="utf-8")
        (pet_root / "README.zh-CN.md").write_text("# 示例宠物\n", encoding="utf-8")
        (pet_root / "LICENSE-ASSETS").write_text(
            "SPDX-License-Identifier: CC-BY-NC-4.0\n",
            encoding="utf-8",
        )
        (pet_root / "references" / "portrait.jpg").write_bytes(b"portrait")
        self._write_png(pet_root / "approved" / "canonical-base.png", (1, 2, 3, 255))
        (pet_root / "pet.yaml").write_text(VALID_PET_YAML, encoding="utf-8")
        self.project = load_pet_project(self.repo_root, "sample-pet")
        self.run_dir = self.repo_root / ".omnipet" / "runs" / "sample-pet"
        self._write_run()
        self._write_png(self.project.canonical_base_path, (9, 8, 7, 255))
        self.project = load_pet_project(self.repo_root, "sample-pet")

    def test_default_refuses_canonical_change_with_generated_work_without_mutation(self):
        before = self._snapshot(self.run_dir)

        with self.assertRaises(RunPreparationError):
            adopt_canonical(self.project, self.repo_root)

        self.assertEqual(self._snapshot(self.run_dir), before)
        self.assertFalse((self.repo_root / ".omnipet" / "archives").exists())

    def test_explicit_reset_archives_history_and_adopts_complete_canonical_provenance(self):
        state = adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

        digest = hashlib.sha256(self.project.canonical_base_path.read_bytes()).hexdigest()
        manifest = self._read_json(self.run_dir / "imagegen-jobs.json")
        base = manifest["jobs"][0]
        generated = Path(base["source_path"])
        result = self._read_json(
            self.run_dir / "qa" / "visual-jobs" / f"{generated.stem}.result.json"
        )
        metadata = self._read_json(self.run_dir / "omnipet-run.json")
        request = self._read_json(self.run_dir / "pet_request.json")

        self.assertEqual(state.counts["complete"], 1)
        self.assertEqual(state.counts["ready"], 2)
        self.assertEqual([job["status"] for job in manifest["jobs"]], ["complete"] + ["pending"] * 12)
        self.assertEqual(
            [job["canvas"] for job in manifest["jobs"]],
            [{"aspect_ratio": "1:1", "image_size": "1K"}]
            + [{"aspect_ratio": "21:9", "image_size": "2K"}] * 12,
        )
        for path in (
            self.project.canonical_base_path,
            self.run_dir / "references" / "canonical-base.png",
            generated,
            self.run_dir / "decoded" / "base.png",
        ):
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            with Image.open(path) as image:
                self.assertEqual(image.format, "PNG")
                image.verify()
        self.assertEqual(base["output_path"], "decoded/base.png")
        self.assertEqual(base["metadata"]["sha256"], digest)
        self.assertTrue(base["completed_at"])
        self.assertEqual(base["adoption_decision"], "approved durable canonical")
        self.assertEqual(result["canvas"], {"aspect_ratio": "1:1", "image_size": "1K"})
        self.assertEqual(result["completed_at"], base["completed_at"])
        self.assertEqual(result["adoption_decision"], "approved durable canonical")
        self.assertEqual(metadata["canonical_base"]["sha256"], digest)
        self.assertEqual(metadata["canonical_base"]["run_path"], "references/canonical-base.png")
        self.assertEqual(request["current_canonical"]["sha256"], digest)
        self.assertEqual(request["current_reference_path"], "references/canonical-base.png")
        self.assertFalse((self.run_dir / "decoded" / "idle.png").exists())
        self.assertFalse((self.run_dir / "generated-sources" / "idle-old.png").exists())
        self.assertFalse((self.run_dir / "qa" / "rows" / "idle" / "review.json").exists())
        self.assertFalse((self.run_dir / "frames").exists())
        self.assertFalse((self.run_dir / "final").exists())
        self.assertFalse((self.run_dir / "tmp").exists())
        self.assertFalse((self.run_dir / "previews").exists())
        self.assertFalse((self.run_dir / "provider-cache").exists())
        self.assertEqual(
            {path.name for path in self.run_dir.iterdir()},
            {
                "pet_request.json", "imagegen-jobs.json", "omnipet-run.json", "prompts",
                "references", "generated-sources", "decoded", "qa",
            },
        )

        archives = list((self.repo_root / ".omnipet" / "archives").glob("sample-pet-canonical-adoption-*"))
        self.assertEqual(len(archives), 1)
        archive = archives[0]
        self.assertTrue((archive / "generated-sources" / "idle-old.png").is_file())
        self.assertTrue((archive / "decoded" / "idle.png").is_file())
        self.assertTrue((archive / "qa" / "rows" / "idle" / "review.json").is_file())
        self.assertTrue((archive / "qa" / "visual-jobs" / "idle-old.result.json").is_file())
        for stale in ("frames", "final", "tmp", "previews", "provider-cache"):
            if stale in {"tmp", "provider-cache"}:
                self.assertFalse((archive / stale).exists())
            else:
                self.assertTrue((archive / stale / "stale.txt").is_file())
        self.assertFalse((archive / "qa" / "provider-secret.log").exists())
        self.assertFalse((archive / "qa" / "raw-response.json").exists())
        for unsafe in ("credentials.dat", ".env", "secret", "report.html", "blob.bin", "fake.png"):
            self.assertFalse((archive / "qa" / unsafe).exists())
        archive_manifest = self._read_json(archive / "archive-manifest.json")
        self.assertEqual(archive_manifest["policy"], "safe-evidence-v2")
        archive_manifest_sha256 = hashlib.sha256(
            (archive / "archive-manifest.json").read_bytes()
        ).hexdigest()
        self.assertEqual(result["archive_manifest_sha256"], archive_manifest_sha256)
        listed = {
            item["path"]: item["sha256"]
            for item in archive_manifest["files"]
        }
        actual = {
            str(path.relative_to(archive)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in archive.rglob("*")
            if path.is_file() and path.name != "archive-manifest.json"
        }
        self.assertEqual(listed, actual)

        generated_names = {path.name for path in (self.run_dir / "generated-sources").iterdir()}
        self.assertEqual(generated_names, {generated.name, "base.png"})
        self.assertNotEqual(generated.name, "base-old.png")
        prompt_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (self.run_dir / "prompts").rglob("*.md")
        ).lower()
        self.assertNotIn("physically attached to his right hand", prompt_text)
        self.assertNotIn("held in his right hand", prompt_text)
        self.assertNotIn("hand-held", prompt_text)
        self.assertIn("fixed dark loop", prompt_text)
        self.assertIn("right waist", prompt_text)
        self.assertIn("right hand", prompt_text)
        self.assertEqual(request["pet_notes"], self._current_pet_notes())
        self.assertEqual(
            self._read_json(self.run_dir / "qa" / "time-log.json")["allocations"],
            {
                "preparation": 2, "base": 3, "standard_rows": 10,
                "look_rows": 8, "final_qa": 5, "packaging": 2,
            },
        )
        prompt_manifest = metadata["prompt_manifest"]
        actual_prompt_hashes = {
            str(path.relative_to(self.run_dir / "prompts")): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.run_dir / "prompts").rglob("*.md")
        }
        self.assertEqual(prompt_manifest, actual_prompt_hashes)
        self._assert_no_dead_absolute_paths_or_secrets(self.run_dir)

    def test_adoption_creates_base_evidence_for_workflow_progression(self):
        from omnipet.workflow import refresh_workflow

        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)
        digest = hashlib.sha256(self.project.canonical_base_path.read_bytes()).hexdigest()

        candidate = self._read_json(self.run_dir / "qa" / "candidates" / "base.json")
        self.assertEqual(candidate["schema_version"], 1)
        self.assertEqual(candidate["job_id"], "base")
        self.assertEqual(candidate["source_path"], "generated-sources/base.png")
        self.assertEqual(candidate["canvas"], {"aspect_ratio": "1:1", "image_size": "1K"})
        self.assertEqual(candidate["sha256"], digest)

        base_source = self.run_dir / "generated-sources" / "base.png"
        self.assertTrue(base_source.is_file())
        self.assertFalse(base_source.is_symlink())
        self.assertEqual(hashlib.sha256(base_source.read_bytes()).hexdigest(), digest)

        review = self._read_json(self.run_dir / "qa" / "base" / "review.json")
        self.assertEqual(
            set(review),
            {"adoption_decision", "canvas", "completed_at", "job_id", "ok", "sha256"},
        )
        self.assertEqual(review["job_id"], "base")
        self.assertTrue(review["ok"])
        self.assertEqual(review["adoption_decision"], "approved durable canonical")
        self.assertEqual(review["canvas"], {"aspect_ratio": "1:1", "image_size": "1K"})
        self.assertEqual(review["sha256"], digest)

        state = refresh_workflow(self.run_dir)
        self.assertEqual(state.state, "awaiting_base_approval")

    def test_prepare_run_succeeds_after_adoption(self):
        """After adopt_canonical, omnipet-run.json has extra canonical_base
        and prompt_manifest fields. prepare_run (called by hatch) must not
        reject the run dir — it should validate only the core reference
        mapping and allow adoption-added metadata."""
        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

        state = prepare_run(self.project, self.repo_root)
        self.assertEqual(state.run_dir, self.run_dir)
        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)
        extra_result = self.run_dir / "qa" / "visual-jobs" / "idle.result.json"
        extra_result.write_text('{"job_id":"idle"}\n', encoding="utf-8")
        row = self.run_dir / "qa" / "rows" / "idle" / "review.json"
        row.parent.mkdir(parents=True)
        row.write_text('{"approved":false}\n', encoding="utf-8")
        preview = self.run_dir / "qa" / "previews" / "idle.gif"
        preview.parent.mkdir()
        Image.new("RGBA", (2, 2), (1, 2, 3, 255)).save(preview, format="GIF")
        before = self._snapshot(self.run_dir)

        with self.assertRaises(RunPreparationError):
            adopt_canonical(self.project, self.repo_root)

        self.assertEqual(self._snapshot(self.run_dir), before)
        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)
        digest = hashlib.sha256(self.project.canonical_base_path.read_bytes()).hexdigest()
        self.assertEqual(
            {str(path.relative_to(self.run_dir / "qa")) for path in (self.run_dir / "qa").rglob("*") if path.is_file()},
            {
                "progress.md", "time-log.json",
                f"visual-jobs/canonical-approved-{digest[:12]}.result.json",
                "candidates/base.json", "base/review.json",
            },
        )
        archive = sorted((self.repo_root / ".omnipet" / "archives").glob("sample-pet-canonical-adoption-*"))[-1]
        self.assertTrue((archive / "qa" / "rows" / "idle" / "review.json").is_file())
        self.assertTrue((archive / "qa" / "previews" / "idle.gif").is_file())

    def test_tampered_archive_provenance_forces_reconciliation(self):
        mutations = {
            "missing-archive": lambda archive, result: __import__("shutil").rmtree(archive),
            "symlink-archive": self._replace_archive_with_symlink,
            "wrong-prefix": self._rename_archive_and_update_result,
            "missing-manifest": lambda archive, result: (archive / "archive-manifest.json").unlink(),
            "manifest-hash": self._tamper_archive_manifest_hash,
            "extra-file": lambda archive, result: (archive / "unexpected.txt").write_text("extra\n", encoding="utf-8"),
            "external-directory-symlink": self._add_external_archive_directory_symlink,
            "internal-directory-symlink": self._add_internal_archive_directory_symlink,
            "file-symlink": self._add_archive_file_symlink,
            "unexpected-empty-directory": lambda archive, result: (archive / "unexpected-empty").mkdir(),
        }
        if hasattr(os, "mkfifo"):
            mutations["fifo"] = lambda archive, result: os.mkfifo(archive / "evidence.fifo")
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                if self.run_dir.exists():
                    __import__("shutil").rmtree(self.run_dir)
                archives = self.repo_root / ".omnipet" / "archives"
                if archives.exists():
                    __import__("shutil").rmtree(archives)
                self._write_run()
                adopt_canonical(self.project, self.repo_root, reset_generated_work=True)
                manifest = self._read_json(self.run_dir / "imagegen-jobs.json")
                source = Path(manifest["jobs"][0]["source_path"])
                result_path = self.run_dir / "qa" / "visual-jobs" / f"{source.stem}.result.json"
                result = self._read_json(result_path)
                archive = self.repo_root / result["archive"]["path"]
                mutate(archive, result)
                result_path.write_text(json.dumps(result), encoding="utf-8")

                adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

                current_result = self._read_json(result_path)
                current_archive = self.repo_root / current_result["archive"]["path"]
                self.assertNotEqual(current_archive, archive)
                self.assertTrue((current_archive / "archive-manifest.json").is_file())

    def test_rewritten_archive_and_manifest_fail_bound_digest(self):
        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)
        result_path, result, archive = self._active_archive()
        evidence = next(
            path for path in archive.rglob("*")
            if path.is_file() and path.name != "archive-manifest.json"
        )
        evidence.write_text("replacement evidence\n", encoding="utf-8")
        self._rewrite_archive_manifest(archive)
        original_digest = result["archive_manifest_sha256"]
        self.assertNotEqual(
            hashlib.sha256((archive / "archive-manifest.json").read_bytes()).hexdigest(),
            original_digest,
        )
        before = len(tuple((self.repo_root / ".omnipet" / "archives").iterdir()))

        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

        current_result = self._read_json(result_path)
        self.assertEqual(
            len(tuple((self.repo_root / ".omnipet" / "archives").iterdir())),
            before + 1,
        )
        self.assertNotEqual(current_result["archive_manifest_sha256"], original_digest)

    def test_empty_replacement_archive_and_rewritten_manifest_fail_bound_digest(self):
        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)
        result_path, result, archive = self._active_archive()
        for path in sorted(archive.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self._rewrite_archive_manifest(archive)
        self.assertEqual(self._read_json(archive / "archive-manifest.json")["files"], [])
        self.assertNotEqual(
            hashlib.sha256((archive / "archive-manifest.json").read_bytes()).hexdigest(),
            result["archive_manifest_sha256"],
        )
        before = len(tuple((self.repo_root / ".omnipet" / "archives").iterdir()))

        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

        current_result = self._read_json(result_path)
        current_archive = self.repo_root / current_result["archive"]["path"]
        self.assertEqual(
            len(tuple((self.repo_root / ".omnipet" / "archives").iterdir())),
            before + 1,
        )
        self.assertTrue(self._read_json(current_archive / "archive-manifest.json")["files"])

    def test_keyword_preserving_prompt_tampering_forces_reconciliation(self):
        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)
        prompt = self.run_dir / "prompts" / "rows" / "idle.md"
        prompt.write_text(
            "fixed dark loop right waist right hand\n",
            encoding="utf-8",
        )
        before_archives = len(tuple((self.repo_root / ".omnipet" / "archives").iterdir()))

        adopt_canonical(self.project, self.repo_root)

        self.assertGreater(len(prompt.read_text(encoding="utf-8")), 100)
        self.assertEqual(
            len(tuple((self.repo_root / ".omnipet" / "archives").iterdir())),
            before_archives + 1,
        )

    def test_swapped_prompt_content_forces_reconciliation(self):
        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)
        idle = self.run_dir / "prompts" / "rows" / "idle.md"
        waving = self.run_dir / "prompts" / "rows" / "waving.md"
        idle.write_bytes(waving.read_bytes())
        before_archives = len(tuple((self.repo_root / ".omnipet" / "archives").iterdir()))

        adopt_canonical(self.project, self.repo_root)

        self.assertIn("subtle breathing", idle.read_text(encoding="utf-8").lower())
        self.assertEqual(
            len(tuple((self.repo_root / ".omnipet" / "archives").iterdir())),
            before_archives + 1,
        )

    def test_tampered_adoption_invariants_force_reconciliation(self):
        mutations = {
            "base-status": lambda manifest, request, metadata, result: manifest["jobs"][0].update(status="pending"),
            "nonbase-status": lambda manifest, request, metadata, result: manifest["jobs"][1].update(status="failed"),
            "nonbase-provenance": lambda manifest, request, metadata, result: manifest["jobs"][1].update(metadata={"old": True}),
            "base-output": lambda manifest, request, metadata, result: manifest["jobs"][0].update(output_path="decoded/wrong.png"),
            "base-prompt": lambda manifest, request, metadata, result: manifest["jobs"][0].update(prompt_file="prompts/wrong.md"),
            "base-canvas": lambda manifest, request, metadata, result: manifest["jobs"][0].update(canvas={"aspect_ratio": "21:9", "image_size": "2K"}),
            "nonbase-canvas": lambda manifest, request, metadata, result: manifest["jobs"][1].update(canvas={"aspect_ratio": "21:9", "image_size": "1K"}),
            "request-reference": lambda manifest, request, metadata, result: request.update(current_reference_path="references/wrong.png"),
            "request-project-path": lambda manifest, request, metadata, result: request["current_canonical"].update(project_path="wrong.png"),
            "metadata-run-path": lambda manifest, request, metadata, result: metadata["canonical_base"].update(run_path="references/wrong.png"),
            "result-job": lambda manifest, request, metadata, result: result.update(job_id="idle"),
            "result-ok": lambda manifest, request, metadata, result: result.update(ok=False),
            "result-completed": lambda manifest, request, metadata, result: result.update(completed_at="different"),
            "result-canvas": lambda manifest, request, metadata, result: result.update(canvas={"aspect_ratio": "21:9", "image_size": "2K"}),
            "result-prompt": lambda manifest, request, metadata, result: result.update(prompt_file="prompts/wrong.md"),
            "result-archive-schema": lambda manifest, request, metadata, result: result.update(archived_sources="wrong"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                if self.run_dir.exists():
                    __import__("shutil").rmtree(self.run_dir)
                archives = self.repo_root / ".omnipet" / "archives"
                if archives.exists():
                    __import__("shutil").rmtree(archives)
                self._write_run()
                adopt_canonical(self.project, self.repo_root, reset_generated_work=True)
                manifest_path = self.run_dir / "imagegen-jobs.json"
                request_path = self.run_dir / "pet_request.json"
                metadata_path = self.run_dir / "omnipet-run.json"
                manifest = self._read_json(manifest_path)
                request = self._read_json(request_path)
                metadata = self._read_json(metadata_path)
                source = Path(manifest["jobs"][0]["source_path"])
                result_path = self.run_dir / "qa" / "visual-jobs" / f"{source.stem}.result.json"
                result = self._read_json(result_path)
                mutate(manifest, request, metadata, result)
                for path, payload in (
                    (manifest_path, manifest), (request_path, request),
                    (metadata_path, metadata), (result_path, result),
                ):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                before = len(tuple(archives.iterdir()))

                adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

                self.assertEqual(len(tuple(archives.iterdir())), before + 1)
                self.assertEqual(load_run_state(self.repo_root, "sample-pet").counts["ready"], 2)

    def test_equal_reference_with_partial_provenance_is_reconciled(self):
        canonical = self.project.canonical_base_path.read_bytes()
        (self.run_dir / "references" / "canonical-base.png").write_bytes(canonical)
        before_manifest = self._read_json(self.run_dir / "imagegen-jobs.json")

        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

        manifest = self._read_json(self.run_dir / "imagegen-jobs.json")
        source = Path(manifest["jobs"][0]["source_path"])
        digest = hashlib.sha256(canonical).hexdigest()
        self.assertNotEqual(manifest, before_manifest)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
        self.assertEqual(manifest["jobs"][0]["metadata"]["sha256"], digest)

    def test_fully_reconciled_equal_state_is_verified_without_new_archive(self):
        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)
        before = self._snapshot(self.run_dir)
        archives = tuple((self.repo_root / ".omnipet" / "archives").iterdir())

        state = adopt_canonical(self.project, self.repo_root)

        self.assertEqual(state.counts["ready"], 2)
        self.assertEqual(self._snapshot(self.run_dir), before)
        self.assertEqual(tuple((self.repo_root / ".omnipet" / "archives").iterdir()), archives)

    def test_cli_requires_explicit_reset_and_reports_reconciled_state(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            refused = main([
                "run", "adopt-canonical", "sample-pet", "--repo-root", str(self.repo_root)
            ])
        self.assertEqual(refused, 1)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"ok": False, "error": "canonical adoption failed"},
        )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            accepted = main([
                "run", "adopt-canonical", "sample-pet", "--reset-generated-work",
                "--repo-root", str(self.repo_root),
            ])
        self.assertEqual(accepted, 0)
        self.assertEqual(json.loads(stdout.getvalue())["counts"]["ready"], 2)

    def test_rejects_symlinked_run_content_without_touching_external_target(self):
        external = Path(self.temp_dir.name) / "external.png"
        external.write_bytes(b"external unchanged")
        stale = self.run_dir / "generated-sources" / "idle-old.png"
        stale.unlink()
        stale.symlink_to(external)
        before = self._snapshot(self.run_dir)

        with self.assertRaises(RunPreparationError):
            adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

        self.assertEqual(external.read_bytes(), b"external unchanged")
        self.assertEqual(self._snapshot(self.run_dir), before)

    def test_rejects_symlinked_archive_root_without_touching_external_target(self):
        external = Path(self.temp_dir.name) / "external-archives"
        external.mkdir()
        marker = external / "marker"
        marker.write_text("unchanged", encoding="utf-8")
        omnipet = self.repo_root / ".omnipet"
        (omnipet / "archives").symlink_to(external, target_is_directory=True)

        with self.assertRaises(RunPreparationError):
            adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(tuple(external.iterdir()), (marker,))

    def test_rejects_contained_directory_symlink_without_mutation(self):
        target = self.run_dir / "qa" / "rows" / "idle"
        link = self.run_dir / "qa" / "rows" / "idle-current"
        link.symlink_to(target, target_is_directory=True)
        before = self._snapshot(self.run_dir)

        with self.assertRaises(RunPreparationError):
            adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

        self.assertEqual(self._snapshot(self.run_dir), before)
        self.assertFalse((self.repo_root / ".omnipet" / "archives").exists())

    def test_rejects_self_and_ancestor_directory_symlink_cycles_without_mutation(self):
        for target in ("cycle", ".."):
            with self.subTest(target=target):
                link = self.run_dir / "cycle"
                link.symlink_to(target, target_is_directory=True)
                try:
                    before = self._snapshot(self.run_dir)
                    with self.assertRaises(RunPreparationError):
                        adopt_canonical(self.project, self.repo_root, reset_generated_work=True)
                    self.assertEqual(self._snapshot(self.run_dir), before)
                finally:
                    link.unlink(missing_ok=True)

    def test_backup_cleanup_failure_keeps_successful_state_and_recoverable_backup(self):
        real_rmtree = __import__("shutil").rmtree

        def fail_backup_cleanup(path, *args, **kwargs):
            if Path(path).name == ".sample-pet-adopt-backup":
                raise OSError("cleanup failed")
            return real_rmtree(path, *args, **kwargs)

        with patch("omnipet.run.shutil.rmtree", side_effect=fail_backup_cleanup):
            state = adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

        self.assertEqual(state.counts["ready"], 2)
        self.assertTrue((self.run_dir.parent / ".sample-pet-adopt-backup").is_dir())
        self.assertEqual(
            len(list((self.repo_root / ".omnipet" / "archives").glob("sample-pet-canonical-adoption-*"))),
            1,
        )

    def test_failed_atomic_swap_restores_original_run_and_publishes_no_archive(self):
        before = self._snapshot(self.run_dir)
        real_replace = os.replace

        def fail_staged_run(source, destination):
            source = Path(source)
            destination = Path(destination)
            if (
                source.is_dir()
                and source.name.startswith(".sample-pet-adopt-")
                and source.name != ".sample-pet-adopt-backup"
                and destination.name == "sample-pet"
                and destination.parent.name == "runs"
            ):
                raise OSError("simulated swap failure")
            return real_replace(source, destination)

        with patch("omnipet.run.os.replace", side_effect=fail_staged_run):
            with self.assertRaises(RunPreparationError):
                adopt_canonical(self.project, self.repo_root, reset_generated_work=True)

        self.assertEqual(self._snapshot(self.run_dir), before)
        archives = self.repo_root / ".omnipet" / "archives"
        self.assertFalse(archives.exists() and any(archives.iterdir()))

    def _write_run(self):
        for relative in (
            "generated-sources", "decoded", "references", "qa/visual-jobs", "qa/rows/idle",
            "prompts/rows", "frames", "final", "tmp", "previews", "provider-cache",
        ):
            (self.run_dir / relative).mkdir(parents=True, exist_ok=True)
        old_base = self.run_dir / "generated-sources" / "base-old.png"
        self._write_png(old_base, (1, 2, 3, 255))
        self._write_png(self.run_dir / "decoded" / "base.png", (1, 2, 3, 255))
        self._write_png(self.run_dir / "references" / "canonical-base.png", (1, 2, 3, 255))
        self._write_png(self.run_dir / "generated-sources" / "approved-candidate.png", (9, 8, 7, 255))
        self._write_png(self.run_dir / "generated-sources" / "idle-old.png", (4, 5, 6, 255))
        self._write_png(self.run_dir / "decoded" / "idle.png", (4, 5, 6, 255))
        (self.run_dir / "references" / "reference-01.jpg").write_bytes(b"portrait")
        (self.run_dir / "qa" / "rows" / "idle" / "review.json").write_text(
            '{"approved":false}\n', encoding="utf-8"
        )
        (self.run_dir / "qa" / "visual-jobs" / "idle-old.result.json").write_text(
            '{"job_id":"idle"}\n', encoding="utf-8"
        )
        (self.run_dir / "qa" / "progress.md").write_text("old progress\n", encoding="utf-8")
        (self.run_dir / "qa" / "time-log.json").write_text('{"entries":[]}\n', encoding="utf-8")
        (self.run_dir / "qa" / "provider-secret.log").write_text(
            "authorization: Bearer must-not-archive\n", encoding="utf-8"
        )
        (self.run_dir / "qa" / "raw-response.json").write_text(
            '{"raw_response":{"token":"must-not-archive"}}\n', encoding="utf-8"
        )
        (self.run_dir / "qa" / "credentials.dat").write_bytes(b"authorization=Bearer hidden")
        (self.run_dir / "qa" / ".env").write_text("API_KEY=hidden\n", encoding="utf-8")
        (self.run_dir / "qa" / "secret").write_text("password=hidden\n", encoding="utf-8")
        (self.run_dir / "qa" / "report.html").write_text("<p>authorization: hidden</p>\n", encoding="utf-8")
        (self.run_dir / "qa" / "blob.bin").write_bytes(b"\x00\x01unknown")
        (self.run_dir / "qa" / "fake.png").write_bytes(b"not a png")
        (self.run_dir / "prompts" / "base-pet.md").write_text(
            "writing brush physically attached to his right hand\n", encoding="utf-8"
        )
        (self.run_dir / "prompts" / "rows" / "idle.md").write_text(
            "brush held in his right hand\n", encoding="utf-8"
        )
        for stale in ("frames", "final", "tmp", "previews", "provider-cache"):
            (self.run_dir / stale / "stale.txt").write_text("stale\n", encoding="utf-8")

        jobs = []
        for job_id in JOB_IDS:
            dependencies = [] if job_id == "base" else ["base"]
            if job_id == "running-left":
                dependencies = ["base", "running-right"]
            elif job_id == "look-cardinals":
                dependencies = list(JOB_IDS[1:10])
            elif job_id == "look-row-9":
                dependencies = ["look-cardinals"]
            elif job_id == "look-row-10":
                dependencies = ["look-cardinals", "look-row-9"]
            job = {
                "id": job_id,
                "kind": JOB_KINDS[job_id],
                "status": "pending",
                "depends_on": dependencies,
                "output_path": f"decoded/{job_id}.png",
                "canvas": {"aspect_ratio": "1:1", "image_size": "1K"}
                if job_id == "base" else {"aspect_ratio": "21:9", "image_size": "2K"},
            }
            if job_id == "base":
                job.update({
                    "status": "complete", "source_path": str(old_base),
                    "completed_at": "2026-07-20T09:00:00+00:00",
                })
            if job_id == "idle":
                job.update({"status": "failed", "metadata": {"reason": "old failure"}})
            jobs.append(job)
        (self.run_dir / "imagegen-jobs.json").write_text(
            json.dumps({
                "schema_version": 1, "run_dir": str(self.run_dir), "jobs": jobs,
            }), encoding="utf-8"
        )
        (self.run_dir / "pet_request.json").write_text(
            json.dumps({"pet_id": "sample-pet", "references": []}), encoding="utf-8"
        )
        (self.run_dir / "omnipet-run.json").write_text(
            json.dumps({
                "schema_version": 1,
                "pet_id": "sample-pet",
                "references": [{
                    "project_path": "references/portrait.jpg",
                    "role": "historical character reference",
                    "run_path": "references/reference-01.jpg",
                    "sha256": hashlib.sha256(b"portrait").hexdigest(),
                }],
            }), encoding="utf-8"
        )

    def _assert_no_dead_absolute_paths_or_secrets(self, root):
        def strings(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn(key.lower(), {"api_key", "authorization", "token", "cookie"})
                    yield from strings(child)
            elif isinstance(value, list):
                for child in value:
                    yield from strings(child)
            elif isinstance(value, str):
                yield value

        for path in root.rglob("*.json"):
            for value in strings(self._read_json(path)):
                if Path(value).is_absolute():
                    self.assertTrue(Path(value).exists(), f"dead path in {path}: {value}")

    def _current_pet_notes(self):
        return " ".join(self.project.brief_path.read_text(encoding="utf-8").split())

    def _replace_archive_with_symlink(self, archive, result):
        outside = Path(self.temp_dir.name) / "outside-archive"
        archive.rename(outside)
        archive.symlink_to(outside, target_is_directory=True)

    def _add_external_archive_directory_symlink(self, archive, result):
        outside = Path(self.temp_dir.name) / "external-evidence"
        outside.mkdir(exist_ok=True)
        (outside / "outside.txt").write_text("must not traverse\n", encoding="utf-8")
        (archive / "external-link").symlink_to(outside, target_is_directory=True)

    def _add_internal_archive_directory_symlink(self, archive, result):
        target = next(path for path in archive.iterdir() if path.is_dir())
        (archive / "internal-link").symlink_to(target, target_is_directory=True)

    def _add_archive_file_symlink(self, archive, result):
        target = next(
            path for path in archive.rglob("*")
            if path.is_file() and path.name != "archive-manifest.json"
        )
        (archive / "evidence-link.txt").symlink_to(target)

    def _rename_archive_and_update_result(self, archive, result):
        renamed = archive.parent / "other-canonical-adoption"
        archive.rename(renamed)
        result["archive"]["path"] = str(renamed.relative_to(self.repo_root))

    def _tamper_archive_manifest_hash(self, archive, result):
        path = archive / "archive-manifest.json"
        manifest = self._read_json(path)
        manifest["files"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def _active_archive(self):
        manifest = self._read_json(self.run_dir / "imagegen-jobs.json")
        source = Path(manifest["jobs"][0]["source_path"])
        result_path = self.run_dir / "qa" / "visual-jobs" / f"{source.stem}.result.json"
        result = self._read_json(result_path)
        return result_path, result, self.repo_root / result["archive"]["path"]

    def _rewrite_archive_manifest(self, archive):
        manifest_path = archive / "archive-manifest.json"
        files = [
            {
                "path": str(path.relative_to(archive)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(archive.rglob("*"))
            if path.is_file() and path != manifest_path
        ]
        manifest_path.write_text(
            json.dumps({"schema_version": 1, "policy": "safe-evidence-v2", "files": files}),
            encoding="utf-8",
        )

    @staticmethod
    def _snapshot(root):
        return {
            path.relative_to(root): ("symlink", os.readlink(path))
            if path.is_symlink() else ("file", path.read_bytes())
            for path in root.rglob("*")
            if path.is_file() or path.is_symlink()
        }

    @staticmethod
    def _read_json(path):
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_png(path, color):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (2, 2), color).save(path, format="PNG")


if __name__ == "__main__":
    unittest.main()
