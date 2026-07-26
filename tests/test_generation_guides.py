import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from PIL import Image

from omnipet.checkpoint import export_checkpoint
from omnipet.cli import main
from omnipet.guides import (
    add_generation_guide,
    clear_generation_guides,
    load_generation_guides,
)
from omnipet.project import load_pet_project
from omnipet.release import (
    approve_project_stage,
    hatch_project,
    init_pet_project,
)
from omnipet.run import prepare_run
from tests.test_release_workflow import FakeGenerator


class GenerationGuideTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        init_pet_project(self.root, "my-pet")
        self.project = load_pet_project(self.root, "my-pet")
        self.run_dir = prepare_run(self.project, self.root).run_dir
        self.source = self.root / "pose-guide.png"
        Image.new("RGB", (24, 24), "red").save(self.source)

    def test_registration_writes_closed_bound_record_and_supports_authorities(self):
        records = [
            add_generation_guide(
                self.project,
                "base",
                self.source,
                role=f"guide {authority}",
                authority=authority,
            )
            for authority in ("identity", "pose-only", "layout-only")
        ]

        registry = json.loads(
            (self.run_dir / "qa/guides.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(registry), {"schema_version", "guides"})
        self.assertEqual(registry["schema_version"], 1)
        self.assertEqual(registry["guides"], records)
        for record in records:
            self.assertEqual(
                set(record),
                {"path", "sha256", "role", "target_job", "authority"},
            )
            self.assertEqual(record["target_job"], "base")
            self.assertTrue(
                record["path"].startswith("references/repair-guides/base/")
            )
            self.assertTrue((self.run_dir / record["path"]).is_file())

    def test_rejects_invalid_source_target_role_and_authority(self):
        link = self.root / "linked.png"
        link.symlink_to(self.source)
        bmp = self.root / "guide.bmp"
        Image.new("RGB", (24, 24), "blue").save(bmp)
        cases = (
            {"source": link},
            {"source": bmp},
            {"job_id": "not-a-job"},
            {"role": "api token"},
            {"authority": "composition"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                values = {
                    "job_id": "base",
                    "source": self.source,
                    "role": "pose sequence",
                    "authority": "pose-only",
                    **overrides,
                }
                with self.assertRaises(ValueError):
                    add_generation_guide(
                        self.project,
                        values["job_id"],
                        values["source"],
                        role=values["role"],
                        authority=values["authority"],
                    )

    def test_registry_rejects_absolute_traversal_symlink_stale_and_wrong_target(self):
        record = add_generation_guide(
            self.project,
            "base",
            self.source,
            role="pose sequence",
            authority="pose-only",
        )
        registry_path = self.run_dir / "qa/guides.json"
        original = json.loads(registry_path.read_text(encoding="utf-8"))
        mutations = (
            {**record, "path": str(self.source)},
            {**record, "path": "../pose-guide.png"},
            {**record, "sha256": "0" * 64},
            {**record, "target_job": "idle"},
            {**record, "extra": True},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                registry_path.write_text(
                    json.dumps({"schema_version": 1, "guides": [mutation]}),
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    load_generation_guides(self.run_dir, "base")
        registry_path.write_text(json.dumps(original), encoding="utf-8")
        guide_path = self.run_dir / record["path"]
        outside = self.root / "outside.png"
        outside.write_bytes(guide_path.read_bytes())
        guide_path.unlink()
        guide_path.symlink_to(outside)
        with self.assertRaises(ValueError):
            load_generation_guides(self.run_dir, "base")

    def test_provider_uses_snapshot_and_attempt_records_metadata_only(self):
        self._approve_base()
        original = self.source.read_bytes()
        record = add_generation_guide(
            self.project,
            "idle",
            self.source,
            role="pose sequence",
            authority="pose-only",
        )

        class MutatingGenerator(FakeGenerator):
            def _write(inner_self, request):
                if request.task == "idle":
                    guide = next(
                        image
                        for image in request.grounding_images
                        if image.role.startswith("authority=pose-only;")
                    )
                    self.assertEqual(guide.content, original)
                    (request.run_root / record["path"]).write_bytes(b"changed")
                return super()._write(request)

        generator = MutatingGenerator()
        hatch_project(self.project, generator_factory=lambda _project: generator)

        idle = next(
            job
            for job in self._manifest()["jobs"]
            if job["id"] == "idle"
        )
        self.assertEqual(idle["metadata"]["generation_guides"], [record])
        self.assertNotIn("content", json.dumps(idle["metadata"]))
        self.assertEqual(load_generation_guides(self.run_dir, "idle"), ())
        self.assertFalse((self.run_dir / record["path"]).exists())

    def test_failed_attempt_clears_one_shot_guide(self):
        self._approve_base()
        record = add_generation_guide(
            self.project,
            "idle",
            self.source,
            role="pose sequence",
            authority="pose-only",
        )
        hatch_project(
            self.project,
            generator_factory=lambda _project: FakeGenerator(fail_job="idle"),
        )

        self.assertEqual(load_generation_guides(self.run_dir, "idle"), ())
        self.assertFalse((self.run_dir / record["path"]).exists())

    def test_cleanup_rejects_replaced_guide_directory_without_touching_outside(self):
        record = add_generation_guide(
            self.project,
            "base",
            self.source,
            role="pose sequence",
            authority="pose-only",
        )
        guide_path = self.run_dir / record["path"]
        guide_dir = guide_path.parent
        outside = self.root / "outside-guides"
        outside.mkdir()
        outside_file = outside / guide_path.name
        outside_file.write_bytes(b"must remain")
        guide_path.unlink()
        guide_dir.rmdir()
        guide_dir.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(ValueError):
            clear_generation_guides(self.run_dir, "base")

        self.assertEqual(outside_file.read_bytes(), b"must remain")

    def test_provider_roles_encode_authority_and_nonidentity_cannot_claim_identity(self):
        records = [
            add_generation_guide(
                self.project,
                "base",
                self.source,
                role=role,
                authority=authority,
            )
            for authority, role in (
                ("identity", "character model"),
                ("pose-only", "pose sequence"),
                ("layout-only", "spacing grid"),
            )
        ]

        from omnipet.release import _grounding

        groundings = _grounding(self.run_dir, "base", tuple(records))
        self.assertEqual(
            [grounding.role for grounding in groundings[-3:]],
            [
                'authority=identity; identity_authoritative=true; role="character model"',
                'authority=pose-only; identity_authoritative=false; role="pose sequence"',
                'authority=layout-only; identity_authoritative=false; role="spacing grid"',
            ],
        )
        with self.assertRaises(ValueError):
            add_generation_guide(
                self.project,
                "base",
                self.source,
                role="identity master",
                authority="pose-only",
            )

    def test_checkpoint_excludes_registered_guides_and_attempt_provenance(self):
        self._approve_base()
        record = add_generation_guide(
            self.project,
            "idle",
            self.source,
            role="checkpoint exclusion marker",
            authority="layout-only",
        )
        hatch_project(
            self.project,
            generator_factory=lambda _project: FakeGenerator(),
        )
        idle = next(
            job for job in self._manifest()["jobs"] if job["id"] == "idle"
        )
        self.assertEqual(idle["metadata"]["generation_guides"], [record])

        checkpoint = export_checkpoint(
            load_pet_project(self.root, "my-pet")
        )
        exported = b"\n".join(
            path.read_bytes() for path in checkpoint.rglob("*") if path.is_file()
        )

        self.assertNotIn(b"repair-guides", exported)
        self.assertNotIn(record["role"].encode(), exported)
        self.assertFalse(
            any("repair-guides" in str(path.relative_to(checkpoint)) for path in checkpoint.rglob("*"))
        )

    def test_cli_adds_guide(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "guide",
                    "add",
                    "my-pet",
                    "--job",
                    "base",
                    "--file",
                    str(self.source),
                    "--role",
                    "pose sequence",
                    "--authority",
                    "pose-only",
                    "--repo-root",
                    str(self.root),
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["guide"]["target_job"], "base")

    def _approve_base(self):
        hatch_project(
            self.project,
            generator_factory=lambda _project: FakeGenerator(),
        )
        approve_project_stage(self.project, "base")

    def _manifest(self):
        return json.loads(
            (self.run_dir / "imagegen-jobs.json").read_text(encoding="utf-8")
        )
