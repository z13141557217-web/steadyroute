"""v0.6.1, from the 10-21 detection record: waits follow each node's own answer time, a line in
a failover storm moves only after about 5 s of silence, a failover target with a clean last
minute comes first, and the business check run while waiting is reused at the switch."""

import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

router = cycle_harness.load_router("detection_v061_router")
TW = router.POLICIES[0]
TW_GROUP = TW["group_name"]
TW_NODES = list(TW["static_candidates"])


def below_target(node_state):
    """Answers, but 80% over 24 hours: a fallback target, not a qualified one."""
    for bucket in node_state["long_buckets"]:
        bucket["success"] = 80
    return node_state


class RealTimeTests(unittest.TestCase):
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

    def seed(self):
        self.net.run_cycle(self.state)
        self.net.realtime = True

    def tick(self):
        started = time.monotonic()
        outcome = self.net.run_fast_tick(self.state)
        return outcome, time.monotonic() - started

    def until_switch(self, limit=12.0):
        """Fast-lane ticks one second apart (as the loop runs them) until the line moves."""
        started = time.monotonic()
        outcomes = []
        while time.monotonic() - started < limit:
            tick_at = time.monotonic()
            outcome, _seconds = self.tick()
            outcomes.append(outcome)
            if outcome == "failover":
                return outcomes, time.monotonic() - started
            time.sleep(max(0.0, 1.0 - (time.monotonic() - tick_at)))
        return outcomes, None

    # 1) Waits follow the node's own answer time.
    def test_a_relay_node_is_not_suspected_once_its_answer_time_is_known(self):
        self.seed()
        self.net.setup[TW_NODES[0]] = 0.6           # reports 60 ms, answers after 0.66 s
        with mock.patch.object(router, "probe_url", side_effect=self.net.probe):
            for _ in range(router.ELAPSED_MIN_FAST):
                router.timed_probe(TW_NODES[0], router.FAST_PROBE_URL, router.FAST_PROBE_TIMEOUT_MS)
        self.assertGreater(router.suspect_after(TW_NODES[0]), 0.9)
        self.assertGreater(router.confirm_timeout_ms(TW_NODES[0]), 1100)
        outcome, _seconds = self.tick()
        self.assertEqual(outcome, "ok")
        self.assertEqual(self.net.puts, [])

    def test_an_unmeasured_relay_node_is_suspected_but_kept(self):
        self.seed()
        self.net.setup[TW_NODES[0]] = 0.6
        router.reset_detection_memory()
        self.assertEqual(router.suspect_after(TW_NODES[0]), router.SUSPECT_AFTER_SECONDS)
        outcome, _seconds = self.tick()
        self.assertIn(outcome, ("transient", "ok"), "the late first probe still answers the confirmation")
        self.assertEqual(self.net.puts, [])

    def test_the_wait_is_capped(self):
        self.seed()
        with router.ELAPSED_LOCK:
            router.ELAPSED[(TW_NODES[0], router.FAST_PROBE_URL)] = [5.0] * 20
        self.assertEqual(router.suspect_after(TW_NODES[0]), router.SUSPECT_MAX_SECONDS)

    def test_late_answers_in_a_stall_do_not_stretch_the_wait(self):
        self.seed()
        with router.ELAPSED_LOCK:
            router.ELAPSED[(TW_NODES[0], router.FAST_PROBE_URL)] = [0.07] * 50 + [1.2] * 10
        self.assertEqual(router.suspect_after(TW_NODES[0]), router.SUSPECT_AFTER_SECONDS)

    # 2) No backup meets the targets, or a storm: about 5 s of silence first.
    def test_with_a_qualified_backup_the_line_moves_in_about_two_seconds(self):
        self.seed()
        self.net.down_nodes.add(TW_NODES[0])
        outcomes, seconds = self.until_switch()
        self.assertEqual(outcomes, ["failover"])
        self.assertLess(seconds, 2.5)

    def test_without_a_qualified_backup_the_line_still_moves_in_about_two_seconds(self):
        for name in TW_NODES[1:]:
            below_target(self.state["nodes"][name])
        self.seed()
        self.net.down_nodes.add(TW_NODES[0])
        outcomes, seconds = self.until_switch()
        self.assertEqual(outcomes, ["failover"])
        self.assertLess(seconds, 2.5)

    def test_in_a_storm_the_line_waits_about_five_seconds(self):
        self.seed()
        now = int(time.time())
        self.state["groups"][TW_GROUP]["failover_times"] = [now - 300, now - 200, now - 100]
        self.net.down_nodes.add(TW_NODES[0])
        outcomes, seconds = self.until_switch()
        self.assertEqual(outcomes[0], "hold")
        self.assertEqual(outcomes[-1], "failover")
        self.assertGreaterEqual(seconds, router.LONG_OUTAGE_SECONDS)
        self.assertLess(seconds, router.LONG_OUTAGE_SECONDS + 2.5)
        self.assertEqual(len(self.net.puts), 1)

    def test_a_node_that_answers_again_while_waiting_is_kept(self):
        self.seed()
        now = int(time.time())
        self.state["groups"][TW_GROUP]["failover_times"] = [now - 300, now - 200, now - 100]
        self.net.stall[TW_NODES[0]] = time.monotonic() + 3.0
        outcomes, seconds = self.until_switch(limit=8.0)
        self.assertIsNone(seconds, outcomes)
        self.assertIn("hold", outcomes)
        self.assertEqual(self.net.puts, [])
        self.assertNotIn(TW_GROUP, router.OUTAGES)

    def test_two_failovers_are_not_a_storm(self):
        self.seed()
        now = int(time.time())
        self.state["groups"][TW_GROUP]["failover_times"] = [now - 200, now - 100]
        self.net.down_nodes.add(TW_NODES[0])
        outcomes, seconds = self.until_switch()
        self.assertEqual(outcomes, ["failover"])

    def test_the_business_check_runs_while_waiting_and_is_reused(self):
        for name in TW_NODES[1:]:
            below_target(self.state["nodes"][name])
        self.seed()
        self.net.down_nodes.add(TW_NODES[0])
        now = int(time.time())
        self.state["groups"][TW_GROUP]["failover_times"] = [now - 300, now - 200, now - 100]   # storm
        with mock.patch.object(router, "probe_url", side_effect=self.net.probe):   # for the background check
            outcome, _seconds = self.tick()
            self.assertEqual(outcome, "hold")
            likely = router.best_failover(TW_NODES, self.state["nodes"], TW_NODES[0])
            deadline = time.monotonic() + 3
            while (TW_GROUP, likely) not in router.PRECHECKED and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(router.PRECHECKED[(TW_GROUP, likely)][1])
            calls = len(self.net.probe_calls)
            self.assertTrue(router.business_preflight(TW_GROUP, likely, self.state, allow_fresh_skip=False))
        self.assertEqual(len(self.net.probe_calls), calls, "no second business check at the switch")


class TargetChoiceTests(unittest.TestCase):
    def test_a_clean_last_minute_beats_a_better_score(self):
        now = time.time()
        nodes = {name: cycle_harness.mature_node(100, now) for name in TW_NODES[:3]}
        nodes[TW_NODES[1]]["score"] = 90.0
        nodes[TW_NODES[1]]["window"] = [[int(now) - 5 * i, 80] for i in range(11, 0, -1)] + [[int(now), -1]]
        nodes[TW_NODES[2]]["score"] = 300.0
        nodes[TW_NODES[2]]["window"] = [[int(now) - 5 * i, 250] for i in range(12, 0, -1)]
        self.assertEqual(router.best_failover(TW_NODES[:3], nodes, TW_NODES[0]), TW_NODES[2])

    def test_score_still_orders_clean_nodes(self):
        now = time.time()
        nodes = {name: cycle_harness.mature_node(100, now) for name in TW_NODES[:3]}
        for name, score in ((TW_NODES[1], 200.0), (TW_NODES[2], 120.0)):
            nodes[name]["score"] = score
            nodes[name]["window"] = [[int(now) - 5 * i, 100] for i in range(12, 0, -1)]
        self.assertEqual(router.best_failover(TW_NODES[:3], nodes, TW_NODES[0]), TW_NODES[2])

    def test_too_few_checks_are_not_clean(self):
        self.assertFalse(router.recently_clean({"window": [[1, 80]] * 3}))
        self.assertTrue(router.recently_clean({"window": [[1, 80]] * 6}))


class TraceTests(unittest.TestCase):
    def test_fast_lines_carry_the_real_time_and_holds_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = router.probe_trace
            trace.configure(directory)
            try:
                trace.record("fast", "node-a", 60, elapsed=640.4)
                trace.record("confirm:hold", "node-a", None)
                trace.flush()
                name = sorted(n for n in os.listdir(directory) if n.endswith(".tsv"))[0]
                with open(os.path.join(directory, name), encoding="utf-8") as handle:
                    lines = handle.read().splitlines()
            finally:
                trace._dir = None
        self.assertTrue(lines[0].endswith("\tf\t1\t60\t640"), lines)
        self.assertTrue(lines[1].endswith("\tk\t1\th"), lines)


if __name__ == "__main__":
    unittest.main()
