import importlib.util
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "build-release-notes.py"
SPEC = importlib.util.spec_from_file_location("release_notes_builder", str(SCRIPT))
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
sys.path.insert(0, str(ROOT / "src" / "steadyroute"))
ROUTER_SPEC = importlib.util.spec_from_file_location("notes_router", str(ROOT / "src" / "steadyroute" / "weighted_router.py"))
router = importlib.util.module_from_spec(ROUTER_SPEC)
ROUTER_SPEC.loader.exec_module(router)


class ReleaseNotesBuilderTests(unittest.TestCase):
    def test_real_history_has_unique_complete_descending_versions(self):
        result = builder.parse_release_history()
        versions = [item["version"] for item in result["releases"]]
        self.assertEqual(versions, ["v0.4.2", "v0.4.1", "v0.4.0", "v0.3.0", "v0.2.0", "v0.1.0"])
        self.assertIn("shadow", json.dumps(result, ensure_ascii=False))
        self.assertIn("PR #14", result["releases"][0]["references"])
        self.assertNotIn("https://", json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["generated_from"], "docs/VERSION_HISTORY.md")
        self.assertEqual(result["source_sha256"], hashlib.sha256(builder.SOURCE.read_bytes()).hexdigest())

    def test_overview_and_sections_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "history.md"
            source.write_text("| v1.0.0 | 2026-09-22 | abc | released |\n", encoding="utf-8")
            with self.assertRaisesRegex(builder.ReleaseNotesError, "must match"):
                builder.parse_release_history(source)

    def test_missing_required_detail_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "history.md"
            source.write_text("| v1.0.0 | 2026-09-22 | abc | released |\n## v1.0.0 — title\n- **关联：**issue\n",
                              encoding="utf-8")
            with self.assertRaisesRegex(builder.ReleaseNotesError, "missing fields"):
                builder.parse_release_history(source)


class ReleaseNotesEndpointTests(unittest.TestCase):
    def test_static_notes_read_never_calls_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            notes = pathlib.Path(directory) / "release_notes.json"
            notes.write_text('{"schema_version":1,"releases":[]}', encoding="utf-8")
            with mock.patch.object(router, "RELEASE_NOTES_PATH", str(notes)), mock.patch.object(
                router, "api_request", side_effect=AssertionError("read-only endpoint called controller")
            ):
                status, content_type, content = router.release_notes_response()
            self.assertEqual(status, 200)
            self.assertIn("application/json", content_type)
            self.assertEqual(json.loads(content)["schema_version"], 1)

    def test_missing_or_oversized_notes_returns_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "release_notes.json"
            with mock.patch.object(router, "RELEASE_NOTES_PATH", str(path)):
                self.assertEqual(router.release_notes_response()[0], 503)
                path.write_bytes(b"x" * (256 * 1024 + 1))
                self.assertEqual(router.release_notes_response()[0], 503)

    def test_dashboard_links_local_notes_and_page_fetches_local_only(self):
        dashboard = (ROOT / "src" / "steadyroute" / "dashboard.html").read_text(encoding="utf-8")
        page = (ROOT / "src" / "steadyroute" / "release_notes.html").read_text(encoding="utf-8")
        self.assertIn('href="/release-notes"', dashboard)
        self.assertIn("fetch('/api/v1/status'", page)
        self.assertIn("fetch('/api/v1/release-notes'", page)
        self.assertNotIn("fetch('https://", page)
        self.assertIn("无法确认运行版本", page)
        self.assertIn("releaseVersions.includes(runningVersion)", page)
        self.assertIn("service.state_stale!==false", page)
        self.assertIn("fields.every", page)


if __name__ == "__main__":
    unittest.main()
