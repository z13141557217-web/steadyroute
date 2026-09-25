import pathlib
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

router = cycle_harness.load_router("fast_lane_router")
TW, HK = router.POLICIES[0], router.POLICIES[1]
TW_GROUP, HK_GROUP = TW["group_name"], HK["group_name"]
TW_NODES, HK_NODES = list(TW["static_candidates"]), list(HK["static_candidates"])


class FastLaneTests(unittest.TestCase):
    def setUp(self):
        router.RUNTIME.clear()
        router.RUNTIME.update({"last_cycle_wall": None, "last_cycle_mono": None, "last_duration_s": 0.0})
        router.TIMELINE.clear()
        self.now = time.time()
        self.net = cycle_harness.FakeNetwork(router)
        self.state = router.state_contract.new_state()
        for policy in router.POLICIES:
            for index, name in enumerate(policy["static_candidates"]):
                self.state["nodes"][name] = cycle_harness.mature_node(80 + 10 * index, self.now)
                self.net.latency[name] = 80 + 10 * index
            self.state["groups"][policy["group_name"]] = {"last_business_probe_at": int(self.now)}
        self.net.connections = [{"id": "stream", "chains": [TW_NODES[0], TW_GROUP]}]

    def test_fast_tick_before_first_cycle_skips(self):
        self.assertEqual(self.net.run_fast_tick(self.state), "skip")

    def test_fast_tick_fails_over_within_one_tick(self):
        self.net.run_cycle(self.state)
        self.net.down_nodes.add(TW_NODES[0])
        started = time.perf_counter()
        outcome = self.net.run_fast_tick(self.state)
        elapsed = time.perf_counter() - started
        self.assertEqual(outcome, "failover")
        self.assertEqual(self.net.puts, [(TW_GROUP, TW_NODES[1])])
        self.assertEqual(len(self.net.deletes), 1)
        self.assertEqual(self.state["groups"][TW_GROUP]["last_confirm"]["lane"], "fast")
        self.assertLess(elapsed, 3.0)

    def test_fast_tick_transient_failure_changes_nothing(self):
        self.net.run_cycle(self.state)
        samples = self.state["nodes"][TW_NODES[0]]["samples"]
        self.net.fail_once[TW_NODES[0]] = 1
        self.assertEqual(self.net.run_fast_tick(self.state), "transient")
        self.assertEqual(self.net.puts, [])
        self.assertEqual(self.state["nodes"][TW_NODES[0]]["samples"], samples)

    def test_fast_tick_local_offline_never_switches(self):
        self.net.run_cycle(self.state)
        self.net.local_offline = True
        self.assertEqual(self.net.run_fast_tick(self.state), "local_offline")
        self.assertEqual(self.net.puts, [])
        self.assertIs(self.state["local_network_ok"], False)

    def test_fast_tick_hands_resume_to_full_cycle(self):
        self.net.run_cycle(self.state)
        router.RUNTIME["last_tick_wall"] = time.time() - 600
        router.RUNTIME["last_tick_mono"] = time.monotonic() - 5
        self.assertEqual(self.net.run_fast_tick(self.state), "resume")

    def test_timeline_records_probes_and_switches_for_the_dashboard(self):
        self.net.run_cycle(self.state)
        self.net.run_fast_tick(self.state)
        self.net.down_nodes.add(TW_NODES[0])
        self.net.run_fast_tick(self.state)
        legacy = router.build_status_snapshots(self.state, self.net.proxy_data, [], now=int(time.time()))["legacy"]
        group = next(item for item in legacy["groups"] if item["name"] == TW_GROUP)
        kinds = [point[2] for point in group["timeline"]]
        self.assertGreaterEqual(kinds.count("probe"), 3)
        self.assertIn("switch", kinds)
        self.assertIsNone(group["timeline"][-2][1], "the failed probe is kept as a gap")
        self.assertEqual(legacy["service"]["fast_probe_interval_seconds"], router.FAST_PROBE_INTERVAL_SECONDS)

    def test_timeline_is_bounded(self):
        for index in range(400):
            router.timeline_add(TW_GROUP, {"t": self.now + index, "ms": 50, "node": "n", "kind": "probe"})
        self.assertLessEqual(len(router.TIMELINE[TW_GROUP]), router.TIMELINE_LIMIT)


class SameRegionRuleTests(unittest.TestCase):
    def test_selection_allowed_only_for_own_region_residential(self):
        self.assertTrue(router.selection_allowed(TW_GROUP, TW_NODES[0]))
        self.assertFalse(router.selection_allowed(TW_GROUP, HK_NODES[0]))
        self.assertFalse(router.selection_allowed(HK_GROUP, TW_NODES[0]))
        self.assertFalse(router.selection_allowed(TW_GROUP, "DIRECT"))
        self.assertFalse(router.selection_allowed(TW_GROUP, "【3x】中转|香港BGP🇭🇰"))
        self.assertFalse(router.selection_allowed(TW_GROUP, "【3x】中转|高速新加坡🇸🇬"))

    def test_select_node_refuses_cross_region(self):
        with self.assertRaises(router.RegionGuardError):
            router.select_node(TW_GROUP, HK_NODES[0], dry_run=True)
        with self.assertRaises(router.RegionGuardError):
            router.select_node(HK_GROUP, "DIRECT", dry_run=True)

    def test_failover_ignores_a_foreign_node_in_the_candidate_list(self):
        now = time.time()
        net = cycle_harness.FakeNetwork(router)
        state = {"nodes": {}, "groups": {TW_GROUP: {"last_seen": TW_NODES[0]}}, "events": []}
        state["nodes"][TW_NODES[0]] = dict(cycle_harness.mature_node(80, now), last_success=False,
                                           failure_streak=2, effective_failure_streak=2)
        state["nodes"][TW_NODES[1]] = cycle_harness.mature_node(120, now)
        state["nodes"][HK_NODES[0]] = cycle_harness.mature_node(20, now)   # best score, wrong region
        with cycle_harness.mock.patch.object(router, "api_request", side_effect=net.api), \
                cycle_harness.mock.patch.object(router, "probe_url", side_effect=net.probe), \
                cycle_harness.mock.patch.object(router, "log"):
            router.evaluate_group(TW_GROUP, [TW_NODES[0], TW_NODES[1], HK_NODES[0]],
                                  net.proxy_data, [], state, False)
        self.assertEqual(net.puts, [(TW_GROUP, TW_NODES[1])])

    def test_policy_rejects_static_candidate_from_another_region(self):
        config = router.route_policy.load_policy_config(router.POLICY_CONFIG_PATHS[1])
        config["policies"][0]["static_candidates"].append(HK_NODES[0])
        with self.assertRaises(router.route_policy.PolicyConfigError):
            router.route_policy.validate_policy_config(config)


if __name__ == "__main__":
    unittest.main()
