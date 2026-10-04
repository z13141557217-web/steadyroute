"""v0.4.6: production folders follow the user's home and can be overridden."""

import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402


class PathTests(unittest.TestCase):
    def test_defaults_follow_home(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("STEADYROUTE_")}
        env["HOME"] = "/Users/someone"
        with mock.patch.dict(os.environ, env, clear=True):
            router = cycle_harness.load_router("paths_default_router")
        self.assertEqual(router.BASE_DIR, "/Users/someone/Library/Application Support/SteadyRoute")
        self.assertEqual(router.LOG_DIR, "/Users/someone/Library/Logs/SteadyRoute")
        self.assertEqual(router.STATE_PATH, os.path.join(router.BASE_DIR, "state.json"))

    def test_environment_overrides(self):
        with mock.patch.dict(os.environ, {"STEADYROUTE_BASE_DIR": "/tmp/sr-base", "STEADYROUTE_LOG_DIR": "/tmp/sr-logs"}):
            router = cycle_harness.load_router("paths_override_router")
        self.assertEqual((router.BASE_DIR, router.LOG_DIR), ("/tmp/sr-base", "/tmp/sr-logs"))
        self.assertEqual(router.DASHBOARD_PATH, "/tmp/sr-base/dashboard.html")

    def test_version_is_read_once(self):
        router = cycle_harness.load_router("paths_version_router")
        with mock.patch.object(router, "_read_version_file", return_value="9.9.9") as read:
            self.assertEqual(router.read_service_version(), "9.9.9")
            self.assertEqual(router.read_service_version(), "9.9.9")
        self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
