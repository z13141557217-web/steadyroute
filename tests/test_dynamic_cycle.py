import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


PROJECT_DIR = pathlib.Path(__file__).parents[1]
MODULE_DIR = PROJECT_DIR / "src" / "steadyroute"
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

router = cycle_harness.load_router("dynamic_cycle_router")


class DynamicCycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.state = router.state_contract.new_state()
        self.puts = []
        self.proxy_data = {}
        for policy in router.POLICIES:
            self.proxy_data[policy["group_name"]] = {
                "now": policy["static_candidates"][0],
                "all": list(policy["static_candidates"]),
            }
            self.proxy_data[policy["discovery_group_name"]] = {
                "now": policy["static_candidates"][0],
                "all": list(policy["static_candidates"]),
            }

    def api(self, method, path, payload=None, timeout=10):
        if method == "GET" and path == "/proxies":
            return {"proxies": self.proxy_data}
        if method == "GET" and path == "/connections":
            return {"connections": []}
        if method == "PUT":
            self.puts.append((path, payload))
            return None
        raise AssertionError((method, path))

    def cycle(self):
        with mock.patch.object(router, "load_state", return_value=self.state), \
                mock.patch.object(router, "save_state"), \
                mock.patch.object(router, "update_dashboard_cache"), \
                mock.patch.object(router, "api_request", side_effect=self.api), \
                mock.patch.object(router, "probe_url", return_value=80):
            router.run_cycle(dry_run=False)

    def test_fake_proxies_cycle_confirms_candidates_without_shadow_put(self):
        self.cycle()
        self.cycle()
        self.assertEqual(self.state["candidate_registry"]["mode"], "shadow")
        self.assertEqual(self.state["subscription"]["generation"], 1)
        for policy in router.POLICIES:
            registered = self.state["candidate_registry"]["policies"][policy["id"]]["candidates"]
            self.assertEqual(registered, policy["static_candidates"])
        self.assertEqual(self.puts, [], "shadow discovery must never select through PUT")

    def test_controller_failure_after_valid_snapshot_preserves_candidates(self):
        self.cycle()
        self.cycle()
        before = {
            policy["id"]: list(self.state["candidate_registry"]["policies"][policy["id"]]["candidates"])
            for policy in router.POLICIES
        }
        with mock.patch.object(router, "load_state", return_value=self.state), \
                mock.patch.object(router, "save_state"), \
                mock.patch.object(router, "update_dashboard_cache"), \
                mock.patch.object(router, "api_request", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                router.run_cycle(dry_run=False)
        after = {
            policy["id"]: list(self.state["candidate_registry"]["policies"][policy["id"]]["candidates"])
            for policy in router.POLICIES
        }
        self.assertEqual(after, before)
        self.assertEqual(self.state["subscription"]["generation"], 1)


if __name__ == "__main__":
    unittest.main()
