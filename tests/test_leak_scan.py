"""v0.4.6: the secret scan cannot be skipped by a missing tool and finds planted secrets."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

PROJECT_DIR = pathlib.Path(__file__).parents[1]
SCANNER = PROJECT_DIR / "scripts" / "leak-scan.py"
# Built at runtime so this file itself never matches the scanner's pattern.
KEY = "to" + "ken"


def run(root, path=None):
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    return subprocess.run([sys.executable, str(SCANNER), str(root)], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, universal_newlines=True)


class ScanSecretsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (self.root / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_tree_passes(self):
        result = run(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_planted_secret_fails_even_without_ripgrep(self):
        (self.root / "config.py").write_text("%s = abc123def\n" % KEY, encoding="utf-8")
        # PATH holds git only, the way a runner without ripgrep would look.
        git_dir = os.path.dirname(subprocess.run(["which", "git"], stdout=subprocess.PIPE,
                                                 universal_newlines=True).stdout.strip())
        result = run(self.root, path=git_dir)
        self.assertEqual(result.returncode, 1)
        self.assertIn("config.py:1", result.stderr)
        self.assertNotIn("abc123def", result.stderr)      # the value itself is never printed

    def test_password_and_subscription_urls_are_caught(self):
        (self.root / "a.yaml").write_text("pass" + "word: hunter2\n", encoding="utf-8")
        (self.root / "b.txt").write_text("subscription" + "-url here\n", encoding="utf-8")
        result = run(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("a.yaml:1", result.stderr)
        self.assertIn("b.txt:1", result.stderr)

    def test_markdown_ignored_and_binary_files_are_skipped(self):
        (self.root / "notes.md").write_text("%s = example\n" % KEY, encoding="utf-8")
        (self.root / "ignored.txt").write_text("%s = example\n" % KEY, encoding="utf-8")
        (self.root / "blob.bin").write_bytes(b"\0\1%s = example" % KEY.encode())
        self.assertEqual(run(self.root).returncode, 0)

    def test_not_a_git_checkout_is_an_error_not_a_pass(self):
        with tempfile.TemporaryDirectory() as bare:
            result = run(bare)
        self.assertEqual(result.returncode, 2)

    def test_no_source_file_is_hidden_by_gitignore(self):
        # v0.4.6 lesson: "*secret*" in .gitignore silently kept the first version of this
        # scanner out of the commit. Anything ignored under these folders must be a cache.
        listed = subprocess.run(
            ["git", "-C", str(PROJECT_DIR), "ls-files", "--others", "--ignored", "--exclude-standard",
             "src", "scripts", "tests", "docs", "config", "deploy", "sim", ".github"],
            stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout.split()
        hidden = [name for name in listed if "__pycache__" not in name and not name.endswith((".pyc", ".DS_Store"))]
        self.assertEqual(hidden, [])

    def test_repository_is_clean(self):
        self.assertEqual(run(PROJECT_DIR).returncode, 0)


if __name__ == "__main__":
    unittest.main()
