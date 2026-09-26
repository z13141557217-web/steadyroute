"""Every released version ships GitHub Release notes (scripts/publish-release.sh reads them)."""

import pathlib
import re
import unittest

PROJECT_DIR = pathlib.Path(__file__).parents[1]


class ReleaseNotesTests(unittest.TestCase):
    def test_current_version_has_release_notes(self):
        version = (PROJECT_DIR / "VERSION").read_text(encoding="utf-8").strip()
        notes = PROJECT_DIR / "docs" / "releases" / ("v%s.md" % version)
        self.assertTrue(notes.is_file(), "missing %s" % notes.relative_to(PROJECT_DIR))
        text = notes.read_text(encoding="utf-8")
        self.assertIn("steadyroute-%s.zip" % version, text)
        self.assertIn("/blob/v%s/CHANGELOG.md" % version, text)

    def test_notes_only_for_released_versions(self):
        released = set(re.findall(r"^## \[(\d+\.\d+\.\d+)\]", (PROJECT_DIR / "CHANGELOG.md").read_text(encoding="utf-8"), re.M))
        for notes in (PROJECT_DIR / "docs" / "releases").glob("v*.md"):
            with self.subTest(notes=notes.name):
                self.assertIn(notes.stem[1:], released)

    def test_publish_script_is_executable(self):
        script = PROJECT_DIR / "scripts" / "publish-release.sh"
        self.assertTrue(script.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
