import unittest
from pathlib import Path

VENDOR_ROOT = Path(__file__).resolve().parents[2] / "src" / "omnipet" / "_vendor" / "hatch"


class SingleFinalChromaPassTest(unittest.TestCase):
    def test_instruction_only_contract_is_outside_vendored_engine_scope(self) -> None:
        self.assertFalse((VENDOR_ROOT / "SKILL.md").exists())
        self.assertTrue((VENDOR_ROOT / "scripts" / "assemble_extended_atlas.py").is_file())
        self.assertTrue((VENDOR_ROOT / "scripts" / "despill_chroma_edges.py").is_file())


if __name__ == "__main__":
    unittest.main()
