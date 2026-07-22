import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from PIL import Image

from omnipet.hatch.prepare import PrepareRunInputs, prepare_run


REPO_ROOT = Path(__file__).resolve().parents[1]


class HatchApiTests(unittest.TestCase):
    def test_production_hatch_runtime_never_mutates_process_globals_or_uses_subprocess(self):
        forbidden = (
            "sys.argv",
            "os.chdir",
            "redirect_stdout",
            "redirect_stderr",
            "subprocess",
            "run_script",
        )
        for path in (REPO_ROOT / "src" / "omnipet" / "hatch").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, text)

    def test_prepare_uses_typed_inputs_and_never_calls_vendored_main(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            reference = root / "reference.png"
            output = root / "run"
            Image.new("RGB", (4, 4), "red").save(reference)
            inputs = PrepareRunInputs(
                pet_id="ember",
                display_name="Ember",
                description="A small test pet.",
                style_preset="pixel",
                style_notes="Warm palette.",
                output_dir=output,
                references=(reference,),
            )

            with patch(
                "omnipet._vendor.hatch.scripts.prepare_pet_run.main",
                side_effect=AssertionError("main must not execute"),
            ):
                result = prepare_run(inputs)

            self.assertEqual(result.run_dir, output)
            manifest = json.loads(result.jobs.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["jobs"]), 13)
            self.assertEqual(
                (output / "references" / "reference-01.png").read_bytes(),
                reference.read_bytes(),
            )

    def test_prepare_rejects_string_references_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            inputs = PrepareRunInputs(
                pet_id="ember",
                display_name="Ember",
                description="A small test pet.",
                style_preset="pixel",
                style_notes="",
                output_dir=root / "run",
                references="not-a-path-tuple",  # type: ignore[arg-type]
            )

            with self.assertRaises(TypeError):
                prepare_run(inputs)

            self.assertFalse((root / "run").exists())

    def test_unrelated_thread_never_observes_changed_process_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            inputs = PrepareRunInputs(
                pet_id="ember",
                display_name="Ember",
                description="A small test pet.",
                style_preset="pixel",
                style_notes="",
                output_dir=root / "run",
            )
            expected = (sys.argv, Path.cwd(), sys.stdout, sys.stderr)
            observations = []
            started = threading.Event()
            release = threading.Event()

            def pause_guides(run_dir):
                started.set()
                release.wait(2)
                return []

            def observe():
                started.wait(2)
                observations.append((sys.argv, Path.cwd(), sys.stdout, sys.stderr))
                release.set()

            observer = threading.Thread(target=observe)
            observer.start()
            with patch(
                "omnipet._vendor.hatch.scripts.prepare_pet_run.create_layout_guides",
                side_effect=pause_guides,
            ):
                prepare_run(inputs)
            observer.join()

            self.assertEqual(observations, [expected])

    def test_extended_atlas_import_ignores_malicious_top_level_sibling(self):
        malicious = ModuleType("extract_strip_frames")
        malicious.component_frame_groups = lambda *args: (_ for _ in ()).throw(AssertionError())
        malicious.component_group_image = malicious.component_frame_groups
        before = set(sys.modules)

        with patch.dict(sys.modules, {"extract_strip_frames": malicious}):
            from omnipet._vendor.hatch.scripts import assemble_extended_atlas

            self.assertEqual(assemble_extended_atlas.component_frame_groups.__module__, (
                "omnipet._vendor.hatch.scripts.extract_strip_frames"
            ))

        added_top_level = {name for name in set(sys.modules) - before if "." not in name}
        self.assertEqual(added_top_level, set())


if __name__ == "__main__":
    unittest.main()
