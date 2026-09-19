import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock


PROJECT_DIR = pathlib.Path(__file__).parents[1]
MODULE_DIR = PROJECT_DIR / "src" / "steadyroute"
sys.path.insert(0, str(MODULE_DIR))
SPEC = importlib.util.spec_from_file_location("status_router", str(MODULE_DIR / "weighted_router.py"))
router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)


def contains_forbidden_key(value):
    forbidden = {"subscription_url", "url", "token", "credential", "credentials", "password", "server", "server_address"}
    if isinstance(value, dict):
        return any(str(key).lower() in forbidden or contains_forbidden_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_forbidden_key(item) for item in value)
    return False


class StatusApiContractTests(unittest.TestCase):
    def setUp(self):
        self.now = 1789762600
        group = "AI 台湾家宽线路"
        current = "台湾 Seednet HY2"
        target = "台湾 HINET 家宽02"
        self.state = {
            "schema_version": 2,
            "nodes": {
                current: {
                    "last_success": True, "samples": 20, "success_streak": 18, "failure_streak": 0,
                    "availability_ewma": 1.0, "latency_ewma": 369.0, "jitter_ewma": 8.0,
                    "score": 381.0, "short_results": [1] * 20,
                    "long_buckets": [{"hour": 1, "success": 998, "total": 1000, "latency_sum": 369000}],
                    "last_probe_at": self.now - 10,
                },
                target: {
                    "last_success": True, "samples": 20, "success_streak": 18, "failure_streak": 0,
                    "availability_ewma": 1.0, "latency_ewma": 82.0, "jitter_ewma": 6.0,
                    "score": 166.0, "short_results": [1] * 20,
                    "long_buckets": [{"hour": 1, "success": 998, "total": 1000, "latency_sum": 82000}],
                    "last_probe_at": self.now - 10,
                },
            },
            "groups": {group: {"last_seen": current, "better_candidate": target, "better_streak": 2}},
            "events": [],
            "updated_at": self.now - 5,
            "last_cycle_duration_ms": 830,
            "controller_connected": True,
            "subscription": {"generation": 17, "candidate_count": 2, "added_count": 0, "removed_count": 0},
        }
        self.proxy_data = {group: {"now": current}}
        self.connections = [{"id": "connection-id", "chains": [current, group]}]

    def test_v1_status_contract_has_versioned_backend_owned_fields(self):
        snapshots = router.build_status_snapshots(
            self.state, self.proxy_data, self.connections, now=self.now, memory_mb=24.1
        )
        payload = snapshots["v1"]
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(
            list(payload),
            ["schema_version", "generated_at", "generated_at_iso", "service", "subscription",
             "policies", "groups", "nodes", "events", "history_summary", "diagnostics"],
        )
        self.assertEqual(payload["generated_at_iso"], "2026-09-18T20:16:40Z")
        self.assertFalse(payload["service"]["state_stale"])
        group = next(item for item in payload["groups"] if item["name"] == "AI 台湾家宽线路")
        self.assertEqual(group["decision"]["code"], "candidate_confirming")
        self.assertEqual(group["decision"]["next_action_code"], "continue_confirmation")
        self.assertTrue(payload["nodes"][0]["lower_score_is_better"])
        self.assertFalse(contains_forbidden_key(payload))

    def test_null_and_zero_are_not_conflated_and_times_have_iso_companions(self):
        self.state["groups"]["AI 台湾家宽线路"].update({"better_candidate": None, "better_streak": 0})
        snapshots = router.build_status_snapshots(self.state, self.proxy_data, [], now=self.now, memory_mb=24.1)
        group = next(item for item in snapshots["v1"]["groups"] if item["name"] == "AI 台湾家宽线路")
        self.assertIsNone(group["target"])
        self.assertEqual(group["timers"]["cooldown_remaining_seconds"], 0)
        self.assertEqual(snapshots["v1"]["service"]["last_cycle_at_iso"], "2026-09-18T20:16:35Z")

    def test_state_staleness_is_calculated_by_backend(self):
        self.state["updated_at"] = self.now - (router.PROBE_INTERVAL_SECONDS * 3 + 1)
        payload = router.build_status_snapshots(
            self.state, self.proxy_data, [], now=self.now, memory_mb=24.1
        )["v1"]
        self.assertTrue(payload["service"]["state_stale"])

    def test_api_whitelists_events_and_subscription_changes(self):
        self.state["events"] = [{
            "code": "group_state_changed", "severity": "info", "scope": "group",
            "subject_id": "group-safe", "group_id": "group-safe", "node_id": None,
            "from_state": "stable", "to_state": "degraded", "reason_code": "quality_drop",
            "occurred_at": self.now - 1, "token": "must-not-leak", "url": "must-not-leak",
        }]
        self.state["subscription"]["changes"] = [{
            "code": "candidate_added", "node_id": "node-safe", "occurred_at": self.now - 2,
            "password": "must-not-leak",
        }]
        payload = router.build_status_snapshots(
            self.state, self.proxy_data, self.connections, now=self.now, memory_mb=24.1
        )["v1"]
        self.assertFalse(contains_forbidden_key(payload))
        self.assertNotIn("must-not-leak", json.dumps(payload, ensure_ascii=False))

    def test_old_and_new_api_are_encoded_from_one_cached_snapshot(self):
        snapshots = router.build_status_snapshots(
            self.state, self.proxy_data, self.connections, now=self.now, memory_mb=24.1
        )
        router.update_dashboard_cache(snapshots)
        legacy = json.loads(router.cached_api_response("/api/status")[2].decode("utf-8"))
        versioned = json.loads(router.cached_api_response("/api/v1/status")[2].decode("utf-8"))
        self.assertEqual(legacy["snapshot_id"], versioned["diagnostics"]["snapshot_id"])
        self.assertEqual(legacy["service"]["updated_at"], versioned["service"]["last_cycle_at"])
        self.assertEqual(legacy["groups"][0]["decision_code"], versioned["groups"][0]["decision"]["code"])

    def test_cached_api_reads_never_probe_or_scan_connections(self):
        snapshots = router.build_status_snapshots(
            self.state, self.proxy_data, self.connections, now=self.now, memory_mb=24.1
        )
        router.update_dashboard_cache(snapshots)
        with mock.patch.object(router, "api_request", side_effect=AssertionError("HTTP reads must not call controller")):
            for _ in range(100):
                self.assertEqual(router.cached_api_response("/api/v1/status")[0], 200)
                self.assertEqual(router.cached_api_response("/api/status")[0], 200)

    def test_snapshot_build_emits_events_only_for_real_state_changes(self):
        router.build_status_snapshots(self.state, self.proxy_data, self.connections, now=self.now, memory_mb=24.1)
        initial_count = len(self.state["events"])
        router.build_status_snapshots(self.state, self.proxy_data, self.connections, now=self.now + 1, memory_mb=24.1)
        self.assertEqual(len(self.state["events"]), initial_count)
        self.state["nodes"]["台湾 Seednet HY2"]["failure_streak"] = 1
        router.build_status_snapshots(self.state, self.proxy_data, self.connections, now=self.now + 2, memory_mb=24.1)
        changed_count = len(self.state["events"])
        self.assertGreater(changed_count, initial_count)
        router.build_status_snapshots(self.state, self.proxy_data, self.connections, now=self.now + 3, memory_mb=24.1)
        self.assertEqual(len(self.state["events"]), changed_count)

    def test_empty_cache_is_503_instead_of_triggering_live_work(self):
        router.DASHBOARD_CACHE = None
        with mock.patch.object(router, "api_request", side_effect=AssertionError("must not probe")):
            status, _, body = router.cached_api_response("/api/v1/status")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "snapshot_unavailable")

    def test_acceptance_dashboard_and_fixture_are_separate_read_only_routes(self):
        self.assertTrue(router.ACCEPTANCE_DASHBOARD_PATH.endswith("acceptance_dashboard.html"))
        status, content_type, body = router.static_acceptance_response("/acceptance/fixtures")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        fixture = json.loads(body)
        self.assertGreaterEqual(len(fixture["group_scenarios"]), 14)
        self.assertGreaterEqual(len(fixture["node_scenarios"]), 7)

    def test_production_dashboard_displays_backend_status_without_state_formulas(self):
        source = (MODULE_DIR / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("lifecycle_title", source)
        self.assertIn("decision_detail", source)
        self.assertIn("state_title", source)
        self.assertNotIn("includes('故障')", source)
        self.assertNotIn("a>d.service.probe_interval_seconds", source)
        self.assertNotIn("name.startsWith", source)


if __name__ == "__main__":
    unittest.main()
