"""v0.6 detection in real time: a dead current node is left in about 2 s, a blip never
switches, a local outage never switches, and the fast lane works while a full cycle is busy."""

import pathlib
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

router = cycle_harness.load_router("detection_v06_router")
TW = router.POLICIES[0]
TW_GROUP = TW["group_name"]
TW_NODES = list(TW["static_candidates"])


class RealTimeDetectionTests(unittest.TestCase):
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
                self.net.latency[name] = 60 + 10 * index
            self.state["groups"][policy["group_name"]] = {"last_business_probe_at": int(self.now)}
        self.net.connections = [{"id": "chat", "chains": [TW_NODES[0], TW_GROUP]}]
        self.net.run_cycle(self.state)          # instant, to seed hot standbys and RUNTIME
        self.net.realtime = True

    def tick(self):
        started = time.monotonic()
        outcome = self.net.run_fast_tick(self.state)
        return outcome, time.monotonic() - started

    def test_dead_node_is_left_in_about_two_seconds(self):
        self.net.down_nodes.add(TW_NODES[0])
        outcome, seconds = self.tick()
        self.assertEqual(outcome, "failover")
        self.assertEqual(self.net.puts[0], (TW_GROUP, TW_NODES[1]))
        self.assertEqual(len(self.net.deletes), 1, "connections on the dead node are closed so pages reload")
        expected = router.SUSPECT_AFTER_SECONDS + router.CONFIRM_STAGGER_SECONDS * (router.CONFIRM_PROBES - 1) \
            + router.CONFIRM_TIMEOUT_MS / 1000.0
        self.assertGreater(seconds, expected - 0.1)
        self.assertLess(seconds, expected + 0.6, "about %.2f s from the probe" % expected)

    def test_a_one_second_stall_is_a_blip(self):
        self.net.stall[TW_NODES[0]] = time.monotonic() + 1.0
        outcome, seconds = self.tick()
        self.assertEqual(outcome, "transient")
        self.assertEqual(self.net.puts, [])
        self.assertEqual(self.net.deletes, [])
        self.assertLess(seconds, 1.6, "the answer ends the confirmation early")

    def test_a_three_second_stall_switches(self):
        self.net.stall[TW_NODES[0]] = time.monotonic() + 3.0
        outcome, _seconds = self.tick()
        self.assertEqual(outcome, "failover")

    def test_one_lost_probe_never_switches(self):
        self.net.fail_once[TW_NODES[0]] = 1
        outcome, _seconds = self.tick()
        self.assertEqual(outcome, "transient")
        self.assertEqual(self.net.puts, [])

    def test_local_outage_never_switches(self):
        self.net.local_offline = True
        outcome, _seconds = self.tick()
        self.assertEqual(outcome, "local_offline")
        self.assertEqual(self.net.puts, [])

    def test_standby_down_too_but_direct_fine_still_moves_to_a_third_node(self):
        hot = self.state["groups"][TW_GROUP]["hot_standby"]
        self.net.down_nodes.update({TW_NODES[0], hot})
        outcome, _seconds = self.tick()
        self.assertEqual(outcome, "failover")
        self.assertNotIn(self.net.puts[0][1], (TW_NODES[0],))

    def test_only_stuck_connections_are_closed(self):
        self.net.connections = [{"id": "stuck", "chains": [TW_NODES[0], TW_GROUP], "download": 500},
                                {"id": "streaming", "chains": [TW_NODES[0], TW_GROUP], "download": 500}]
        self.net.connection_growth = {"streaming": 4000}
        self.net.down_nodes.add(TW_NODES[0])
        outcome, _seconds = self.tick()
        self.assertEqual(outcome, "failover")
        self.assertEqual(self.net.deletes, ["/connections/stuck"], "a connection still receiving data is kept")

    def test_fast_lane_fails_over_while_a_slow_cycle_runs(self):
        urls = TW["business_test_urls"]
        for url in urls:
            self.net.slow_urls[url] = 2.5
        self.state["groups"][TW_GROUP]["last_business_probe_at"] = 0     # business probes due: a slow cycle
        patches = [
            mock.patch.object(router, "load_state", return_value=self.state),
            mock.patch.object(router, "save_state"),
            mock.patch.object(router, "update_dashboard_cache"),
            mock.patch.object(router, "log"),
            mock.patch.object(router, "api_request", side_effect=self.net.api),
            mock.patch.object(router, "probe_url", side_effect=self.net.probe),
        ]
        for patch in patches:
            patch.start()
        try:
            cycle = threading.Thread(target=router.run_cycle)
            cycle.start()
            time.sleep(0.5)                       # the cycle is now waiting on its probes
            self.net.down_nodes.add(TW_NODES[0])
            started = time.monotonic()
            outcome = router.run_fast_tick()
            seconds = time.monotonic() - started
            cycle.join(15)
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(outcome, "failover")
        self.assertLess(seconds, 2.5, "not held up by the cycle's business probes")
        self.assertEqual([put for put in self.net.puts if put[0] == TW_GROUP], [(TW_GROUP, TW_NODES[1])])
        group_state = self.state["groups"][TW_GROUP]
        self.assertEqual(group_state["last_router_selection"], TW_NODES[1])
        self.assertFalse(int(group_state.get("manual_hold_until", 0)), "the cycle must not read our switch as manual")


if __name__ == "__main__":
    unittest.main()
