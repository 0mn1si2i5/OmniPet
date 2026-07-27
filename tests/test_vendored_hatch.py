from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = REPO_ROOT / "src" / "omnipet" / "_vendor" / "hatch"
PACKAGE_ROOT = PurePosixPath("omnipet/_vendor/hatch")
UPSTREAM_IDENTIFIER = "Codex hatch-pet skill distribution"
UPSTREAM_HASHES = {
    "LICENSE.txt": "4dd13869245e356246a5b770723247bbb80a8f07a181d1d3d873a1734297cdb9",
    "references/animation-rows.md": "d27b08d599e73cf6a65a03d2b8ad49e9ad408e18292636164e7b2d2f1815615f",
    "references/codex-pet-contract.md": "8f9c271e0a8269e57cd27cedeff1317975ea5f772f2549572aeefa580d9d58d3",
    "references/qa-rubric.md": "46bd447611d101cca0d89692ee566cfd58abfea30d4cfb7749b2c930e0b7deaf",
    "scripts/assemble_extended_atlas.py": "ca50b13d62858a660e1ae2d15649a1ccb565d03ee185539d22df0e121075eadc",
    "scripts/combine_direction_blind_verdicts.py": "4dad56adaad032a4e6d070494b0ab2ca316429cf69363450f9fbf7135d1c2d42",
    "scripts/compose_atlas.py": "a76d7c8b81033353004f5cafab8fa174c314f1d79158b69bc83a2f0bd6047fdf",
    "scripts/compose_cardinal_anchor_strip.py": "e67ac9816a909cad136189bf23eddf8b16e0fcb430ade2262556bbd12db2bca5",
    "scripts/derive_running_left_from_running_right.py": "ae42859720220fe8a407fc0dfff06e8344e535c6c8019220b1f63db7714baa77",
    "scripts/despill_chroma_edges.py": "dc93a5e752f4100e55010205462a8e7272bac451ee7e29b525abd6157a7fe309",
    "scripts/extract_cardinal_anchors.py": "55434d06bc328b1879d98c821f2586963910f89926f4dcbe92682b25555a8cde",
    "scripts/extract_strip_frames.py": "86301292945550c4b2c318815c9bfd97a1f9b3fa4e83aea7b31bcfa2ca20e1f2",
    "scripts/inspect_frames.py": "9ef930151ae845a3c9eb3c10dfc8da95947535d18c72992cff5f65081af4c30a",
    "scripts/make_contact_sheet.py": "51e2085b8acb172dcdd5fff9993bdee413f3851b714229ca095dc99cd551aa96",
    "scripts/make_direction_blind_qa_sheet.py": "52f2a29251872449fed51c7744c3f9f503274ee288eb23efc29a2c568b0d52bd",
    "scripts/make_direction_qa_sheet.py": "823e81e0aece24d1d6537889c9daaa2660208ff52604509b24fd5e24e7302acb",
    "scripts/measure_direction_continuity.py": "e24b7065af82eab5638f1fcdeb627d497391a2f1e9ba19801827d1db3a6d8c2d",
    "scripts/prepare_pet_run.py": "ca9809003e95248339db3c9fe0625e4e871732146bf06e19efc091191bd9da1f",
    "scripts/render_animation_previews.py": "911e8813e1b79b7f9da44fae8a667c044818e8c71f41eaa4b280e91c78cde61e",
    "scripts/validate_atlas.py": "ebbbc77cfbd27ef8476ac6fda716e864cf372a2ed4c2beb27ebdb2487e972194",
    "scripts/validate_direction_blind_verdicts.py": "7871667432918e0ffcdbb9beaf88a01c0af4b9e2809c5000f7b533a9ddc6e13d",
}
UPSTREAM_TESTS = {
    "test_assemble_extended_atlas.py",
    "test_chroma_matte_decontamination.py",
    "test_direction_acceptance_policy.py",
    "test_direction_blind_consensus.py",
    "test_look_row_safe_box_prompt.py",
    "test_single_final_chroma_pass.py",
}
VENDOR_METADATA = {"__init__.py", "MANIFEST.json", "NOTICE", "VENDORING.md"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VendoredHatchTests(unittest.TestCase):
    def test_inventory_and_hash_manifest_preserve_upstream_bytes(self) -> None:
        actual_files = {
            path.relative_to(VENDOR_ROOT).as_posix()
            for path in VENDOR_ROOT.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(actual_files, set(UPSTREAM_HASHES) | VENDOR_METADATA)

        manifest = json.loads((VENDOR_ROOT / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["upstream_source"], UPSTREAM_IDENTIFIER)
        self.assertNotIn("/Users/", json.dumps(manifest))
        entries = {entry["path"]: entry for entry in manifest["files"]}
        self.assertEqual(set(entries), set(UPSTREAM_HASHES))
        for relative_path, expected_hash in UPSTREAM_HASHES.items():
            with self.subTest(path=relative_path):
                entry = entries[relative_path]
                self.assertEqual(entry["sha256_original"], expected_hash)
                if relative_path in {
                    "scripts/assemble_extended_atlas.py",
                    "scripts/prepare_pet_run.py",
                    "references/codex-pet-contract.md",
                }:
                    expected_vendored = {
                        "scripts/assemble_extended_atlas.py": "27101d97c70cc32fda8f31aeddd9179be66be2c04455b633482a25bb3672e331",
                        "scripts/prepare_pet_run.py": "957c9f3b8e6b101b99d9ff34a3626ebf7f4a0d2179d20604fd3c55dff35eac6f",
                        "references/codex-pet-contract.md": "b91df5e7738a7f64f83a3589efc12a08654821fa271556afe3e4fe065afac047",
                    }[relative_path]
                    self.assertEqual(entry["sha256_vendored"], expected_vendored)
                    self.assertIs(entry["modified"], True)
                else:
                    self.assertEqual(entry["sha256_vendored"], expected_hash)
                    self.assertIs(entry["modified"], False)
                self.assertEqual(sha256(VENDOR_ROOT / relative_path), entry["sha256_vendored"])

    def test_license_and_notices_attribute_the_apache_source(self) -> None:
        license_text = (VENDOR_ROOT / "LICENSE.txt").read_text(encoding="utf-8")
        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0", license_text)
        for notice_path in (REPO_ROOT / "NOTICE", VENDOR_ROOT / "NOTICE", VENDOR_ROOT / "VENDORING.md"):
            with self.subTest(path=notice_path):
                notice = notice_path.read_text(encoding="utf-8")
                self.assertIn("hatch-pet", notice)
                self.assertIn("Apache License 2.0", notice)
                self.assertIn(UPSTREAM_IDENTIFIER, notice)
                self.assertNotIn("/Users/", notice)
        self.assertIn("modified: true", (VENDOR_ROOT / "VENDORING.md").read_text(encoding="utf-8"))

    def test_upstream_test_inventory_is_copied(self) -> None:
        test_root = REPO_ROOT / "tests" / "vendor_hatch"
        actual = {path.name for path in test_root.glob("test_*.py")}
        self.assertEqual(actual, UPSTREAM_TESTS)

    def test_wheel_and_sdist_include_all_vendor_data(self) -> None:
        expected = set(UPSTREAM_HASHES) | VENDOR_METADATA
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            command = (
                "import setuptools.build_meta as backend; "
                f"out={str(output)!r}; backend.build_wheel(out); backend.build_sdist(out)"
            )
            subprocess.run(
                [sys.executable, "-c", command],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            wheel, = output.glob("*.whl")
            sdist, = output.glob("*.tar.gz")

            with zipfile.ZipFile(wheel) as archive:
                wheel_vendor = {
                    PurePosixPath(name).relative_to(PACKAGE_ROOT).as_posix()
                    for name in archive.namelist()
                    if PurePosixPath(name).is_relative_to(PACKAGE_ROOT)
                    and not name.endswith("/")
                }
            self.assertEqual(wheel_vendor, expected)

            with tarfile.open(sdist, "r:gz") as archive:
                sdist_vendor = {
                    PurePosixPath(*PurePosixPath(member.name).parts[1:])
                    .relative_to(PurePosixPath("src") / PACKAGE_ROOT)
                    .as_posix()
                    for member in archive.getmembers()
                    if PurePosixPath(*PurePosixPath(member.name).parts[1:]).is_relative_to(
                        PurePosixPath("src") / PACKAGE_ROOT
                    )
                    and member.isfile()
                }
            self.assertEqual(sdist_vendor, expected)


if __name__ == "__main__":
    unittest.main()
