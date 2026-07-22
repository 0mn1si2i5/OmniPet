from pathlib import Path
import subprocess
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class GitignoreTests(unittest.TestCase):
    def check_ignored(self, *paths: str) -> set[str]:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", *paths],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertIn(result.returncode, (0, 1), result.stderr)
        return set(result.stdout.splitlines())

    def test_local_environment_files_are_ignored_except_example(self):
        secrets = {".env", ".env.local"}
        trackable = {
            ".env.example",
            "pets/sample-pet/dist/pet.json",
            "pets/sample-pet/dist/spritesheet.webp",
        }

        self.assertTrue((REPO_ROOT / ".env.example").is_file())
        self.assertEqual((REPO_ROOT / ".env.example").read_text(), "OPENAI_API_KEY=\n")
        ignored = self.check_ignored(*(secrets | trackable))

        self.assertEqual(ignored, secrets)

    def test_pet_packages_remain_trackable_while_build_outputs_are_ignored(self):
        pet_outputs = {
            "pets/example/dist/pet.json",
            "pets/example/dist/spritesheet.webp",
        }
        build_outputs = {
            "dist/omnipet.whl",
            "build/omnipet/package.py",
            "src/omnipet.egg-info/PKG-INFO",
        }

        ignored = self.check_ignored(*(pet_outputs | build_outputs))

        self.assertTrue(pet_outputs.isdisjoint(ignored))
        self.assertEqual(ignored, build_outputs)

    def test_all_omnipet_runtime_and_archives_are_ignored(self):
        runtime = {
            ".omnipet/runs/sample-pet/imagegen-jobs.json",
            ".omnipet/archives/sample-pet-canonical-adoption/evidence.json",
        }
        pet_outputs = {
            "pets/sample-pet/dist/pet.json",
            "pets/sample-pet/dist/spritesheet.webp",
        }

        ignored = self.check_ignored(*(runtime | pet_outputs))

        self.assertEqual(ignored, runtime)


if __name__ == "__main__":
    unittest.main()
