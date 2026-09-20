import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "src" / "steadyroute" / "weighted_router.py"
SPEC = importlib.util.spec_from_file_location("weighted_router", str(MODULE_PATH))
router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(router)


def healthy(score):
    return {
        "last_success": True,
        "samples": 20,
        "success_streak": 10,
        "failure_streak": 0,
        "availability_ewma": 1.0,
        "latency_ewma": score,
        "jitter_ewma": 0.0,
        "score": float(score),
    }


class RoutingDecisionTests(unittest.TestCase):
    def setUp(self):
        self.selected = []
        self.closed = []
        self.original_select = router.select_node
        self.original_close = router.close_old_connections
        router.select_node = lambda group, node, dry_run: self.selected.append((group, node))
        router.close_old_connections = lambda group, node, connections=None: self.closed.append((group, node)) or 1

    def tearDown(self):
        router.select_node = self.original_select
        router.close_old_connections = self.original_close

    def test_active_connections_do_not_block_lossless_recovery(self):
        group = "test-group"
        state = {
            "nodes": {"current": healthy(500), "better": healthy(100)},
            "groups": {
                group: {
                    "last_seen": "current",
                    "last_switch_at": 0,
                    "better_candidate": "better",
                    "better_streak": router.PERFORMANCE_CONFIRMATIONS - 1,
                }
            },
        }
        proxy_data = {group: {"now": "current"}}
        connections = [{"id": "live", "chains": ["current", group]}]

        router.evaluate_group(group, ["current", "better"], proxy_data, connections, state, False)

        self.assertEqual(self.selected, [(group, "better")])
        self.assertEqual(self.closed, [], "lossless recovery must preserve existing connections")

    def test_real_failure_still_closes_only_stale_connections(self):
        group = "test-group"
        current = healthy(500)
        current["last_success"] = False
        current["failure_streak"] = router.FAILURES_BEFORE_SWITCH
        state = {"nodes": {"current": current, "better": healthy(100)}, "groups": {group: {"last_seen": "current"}}}

        router.evaluate_group(
            group,
            ["current", "better"],
            {group: {"now": "current"}},
            [{"id": "stale", "chains": ["current", group]}],
            state,
            False,
        )

        self.assertEqual(self.selected, [(group, "better")])
        self.assertEqual(self.closed, [(group, "current")])

    def test_confirmed_current_removal_selects_mature_backup_without_closing_connections(self):
        group = "test-group"
        state = {
            "nodes": {"backup": healthy(100)},
            "groups": {group: {"last_seen": "removed"}},
        }
        original_preflight = router.business_preflight
        router.business_preflight = lambda *args, **kwargs: True
        try:
            router.evaluate_group(
                group, ["backup"], {group: {"now": "removed"}},
                [{"id": "old", "chains": ["removed", group]}], state, False,
            )
        finally:
            router.business_preflight = original_preflight
        self.assertEqual(self.selected, [(group, "backup")])
        self.assertEqual(self.closed, [], "removal is not proof of a failed old connection")
        self.assertEqual(state["groups"][group]["last_seen"], "backup")


class HealthModelTests(unittest.TestCase):
    def test_quarantine_requires_time_and_three_recovery_successes(self):
        node = {}
        started = 1_000_000
        router.update_node_stats(node, None, started)
        router.update_node_stats(node, None, started + 60)
        router.update_node_stats(node, None, started + 120)
        self.assertTrue(router.is_quarantined(node, started + 121))

        router.update_node_stats(node, 100, started + 180)
        router.update_node_stats(node, 100, started + 240)
        router.update_node_stats(node, 100, started + 300)
        self.assertTrue(router.is_quarantined(node, started + 301), "30-minute isolation must still apply")
        self.assertFalse(router.is_quarantined(node, started + 120 + router.QUARANTINE_SECONDS + 1))

    def test_layered_eligibility_rejects_bad_short_or_long_availability(self):
        node = healthy(100)
        node["short_results"] = [1] * 15 + [0] * 5
        node["long_buckets"] = [{"hour": 1, "success": 95, "total": 100, "latency_sum": 9500}]
        self.assertFalse(router.eligible_for_optimization(node))

        node["short_results"] = [1] * 20
        node["long_buckets"] = [{"hour": 1, "success": 85, "total": 100, "latency_sum": 8500}]
        self.assertFalse(router.eligible_for_optimization(node))

    def test_adaptive_targets_probe_current_and_one_standby_per_group(self):
        proxy_data = {
            group: {"now": candidates[0]}
            for group, candidates in router.GROUPS.items()
        }
        state = {"groups": {}, "nodes": {}}
        targets = router.choose_probe_targets(state, proxy_data)
        expected = len(router.GROUPS) * (1 + router.STANDBY_PROBES_PER_GROUP)
        self.assertEqual(len(targets), expected)

    def test_confirmed_business_failure_triggers_effective_failure(self):
        node = {}
        router.update_effective_health(node, True, True, False)
        self.assertGreaterEqual(node["effective_failure_streak"], router.FAILURES_BEFORE_SWITCH)
        router.update_effective_health(node, False, True, True)
        self.assertEqual(node["effective_failure_streak"], 0)


if __name__ == "__main__":
    unittest.main()
