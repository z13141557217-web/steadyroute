import pathlib
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

router = cycle_harness.load_router("fast_failover_router")
TW = router.POLICIES[0]
TW_GROUP = TW["group_name"]
TW_NODES = list(TW["static_candidates"])


class FastFailoverCycleTests(unittest.TestCase):
    def setUp(self):
        router.RUNTIME.update({"last_cycle_wall": None, "last_cycle_mono": None, "last_duration_s": 0.0})
        self.now = time.time()
        self.net = cycle_harness.FakeNetwork(router)
        self.state = router.state_contract.new_state()
        for policy in router.POLICIES:
            for index, name in enumerate(policy["static_candidates"]):
                latency = 80 + 10 * index
                self.state["nodes"][name] = cycle_harness.mature_node(latency, self.now)
                self.net.latency[name] = latency
        self.current = TW_NODES[0]
        self.backup = TW_NODES[1]   # lowest score among the standbys
        self.net.connections = [{"id": "live-1", "chains": [self.current, TW_GROUP]}]
        # Business probes are not due unless a test asks for them.
        for policy in router.POLICIES:
            self.state["groups"][policy["group_name"]] = {"last_business_probe_at": int(self.now)}

    def make_business_due(self):
        self.state["groups"][TW_GROUP].update({"last_business_probe_at": 0, "business_probe_cursor": 0})

    def assert_never_selected_builtin(self):
        for _group, name in self.net.puts:
            self.assertNotIn(name, {"DIRECT", "REJECT", "COMPATIBLE"})

    def test_confirmed_failure_switches_in_same_cycle(self):
        self.net.down_nodes.add(self.current)
        self.net.run_cycle(self.state)
        self.assertIn((TW_GROUP, self.backup), self.net.puts)
        self.assertEqual(len(self.net.deletes), 1, "only the failed node's connection is closed")
        self.assertEqual(self.state["groups"][TW_GROUP]["last_confirm"]["verdict"], "confirmed")
        self.assert_never_selected_builtin()

    def test_confirmation_uses_three_consecutive_failures(self):
        self.net.down_nodes.add(self.current)
        self.net.run_cycle(self.state)
        base_calls = [call for call in self.net.calls_to(self.current) if call[1] == router.TEST_URL]
        light_calls = [call for call in self.net.calls_to(self.current) if call[1] == router.FAST_PROBE_URL]
        self.assertEqual(len(base_calls), 1)
        self.assertEqual(len(light_calls), 1 + router.CONFIRM_PROBES, "one live-strip probe plus two confirmations")

    def test_business_success_skips_confirmation(self):
        self.net.fail_once[self.current] = 0
        url = router.BUSINESS_TEST_URLS[TW_GROUP][0]
        self.make_business_due()
        original = self.net.probe

        def probe(name, url_, timeout_ms):
            if name == self.current and url_ == router.TEST_URL:
                return None
            return original(name, url_, timeout_ms)

        self.net.probe = probe
        self.net.run_cycle(self.state)
        self.assertEqual(self.net.puts, [], "the node carried real AI traffic, so the base failure is not proof")
        self.assertNotIn("last_confirm", self.state["groups"][TW_GROUP])
        self.assertTrue(url)

    def test_transient_failure_does_not_switch(self):
        original = self.net.probe
        light_failures = {"left": 2}   # the live-strip probe and the first confirmation fail

        def probe(name, url, timeout_ms):
            if name == self.current and url == router.TEST_URL:
                return None
            if name == self.current and url == router.FAST_PROBE_URL and light_failures["left"] > 0:
                light_failures["left"] -= 1
                return None
            return original(name, url, timeout_ms)

        self.net.probe = probe
        self.net.run_cycle(self.state)
        self.assertEqual(self.net.puts, [])
        self.assertEqual(self.state["nodes"][self.current]["failure_streak"], 1)
        self.assertEqual(self.state["groups"][TW_GROUP]["last_confirm"]["verdict"], "transient")

    def test_confirm_probes_do_not_inflate_samples(self):
        before = self.state["nodes"][self.current]["samples"]
        self.net.down_nodes.add(self.current)
        self.net.run_cycle(self.state)
        self.assertEqual(self.state["nodes"][self.current]["samples"], before + 1)

    def test_local_offline_freezes_decisions_and_samples(self):
        self.net.local_offline = True
        before = {name: len(item["short_results"]) for name, item in self.state["nodes"].items()}
        self.net.run_cycle(self.state)
        self.assertEqual(self.net.puts, [])
        self.assertIs(self.state["local_network_ok"], False)
        for name, length in before.items():
            self.assertEqual(len(self.state["nodes"][name]["short_results"]), length, name)
            self.assertFalse(int(self.state["nodes"][name].get("quarantine_until", 0)))
        codes = [event["code"] for event in self.state["events"]]
        self.assertIn("LOCAL_NETWORK_OFFLINE", codes)
        snapshot = router.build_status_snapshots(self.state, self.net.proxy_data, [], now=int(time.time()))
        self.assertEqual(snapshot["v1"]["service"]["state"]["code"], "local_network_offline")

        self.net.local_offline = False
        self.net.run_cycle(self.state)
        self.assertIs(self.state["local_network_ok"], True)
        self.assertIn("LOCAL_NETWORK_RECOVERED", [event["code"] for event in self.state["events"]])
        self.assertEqual(self.net.puts, [])

    def test_network_back_before_local_check_still_not_charged(self):
        """Everything failed, but the network recovered before the DIRECT check ran."""
        original = self.net.probe

        def probe(name, url, timeout_ms):
            if name == "DIRECT":
                return 20
            return None

        self.net.probe = probe
        before = {name: len(item["short_results"]) for name, item in self.state["nodes"].items()}
        self.net.run_cycle(self.state)
        self.net.probe = original
        self.assertEqual(self.net.puts, [])
        for name, length in before.items():
            self.assertEqual(len(self.state["nodes"][name]["short_results"]), length, name)
        self.assertIs(self.state["local_network_ok"], True)

    def test_single_node_failure_is_not_a_blackout(self):
        self.net.down_nodes.add(self.current)
        self.net.run_cycle(self.state)
        self.assertEqual(self.state["groups"][TW_GROUP]["last_confirm"]["verdict"], "confirmed")

    def test_local_check_runs_once_per_cycle(self):
        self.net.local_offline = True
        self.net.run_cycle(self.state)
        self.assertEqual(len(self.net.calls_to("DIRECT")), len(router.LOCAL_CHECK_URLS))

    def test_local_check_never_selects_direct(self):
        self.net.down_nodes.add(self.current)
        self.net.run_cycle(self.state)
        self.assertTrue(self.net.calls_to("DIRECT"))
        self.assert_never_selected_builtin()

    def test_resume_cycle_records_only_successes(self):
        router.RUNTIME.update({
            "last_cycle_wall": time.time() - 600,
            "last_cycle_mono": time.monotonic() - 20,
            "last_duration_s": 1.0,
        })
        self.net.down_nodes.add(self.current)
        before = len(self.state["nodes"][self.current]["short_results"])
        self.net.run_cycle(self.state)
        self.assertEqual(self.net.puts, [])
        self.assertEqual(len(self.state["nodes"][self.current]["short_results"]), before)
        self.assertTrue(self.state.get("last_resume_at"))

    def test_resume_is_detected_before_probes_are_recorded(self):
        router.RUNTIME.update({
            "last_cycle_wall": time.time() - 600,
            "last_cycle_mono": time.monotonic() - 20,
            "last_duration_s": 1.0,
        })
        seen = []
        original = self.net.probe

        def probe(name, url, timeout_ms):
            seen.append(bool(self.state.get("last_resume_at")))
            return original(name, url, timeout_ms)

        self.net.probe = probe
        self.net.run_cycle(self.state)
        self.assertTrue(seen and all(seen))

    def test_target_side_failure_does_not_count_against_node(self):
        url = router.BUSINESS_TEST_URLS[TW_GROUP][0]
        self.net.site_down.add(url)
        self.make_business_due()
        self.net.run_cycle(self.state)
        self.assertEqual(self.net.puts, [])
        self.assertEqual(int(self.state["nodes"][self.current].get("effective_failure_streak", 0)), 0)
        self.assertIn(url, self.state["groups"][TW_GROUP]["business_target_down"])

    def test_target_down_event_is_deduplicated_and_has_no_url(self):
        url = router.BUSINESS_TEST_URLS[TW_GROUP][0]
        self.net.site_down.add(url)
        for _ in range(3):
            self.make_business_due()
            self.net.run_cycle(self.state)
        events = [event for event in self.state["events"] if event["code"] == "BUSINESS_TARGET_UNREACHABLE"]
        self.assertEqual(len(events), 1)
        self.assertNotIn("http", repr(events))

    def test_node_side_business_failure_triggers_failover(self):
        url = router.BUSINESS_TEST_URLS[TW_GROUP][0]
        self.net.node_url_failures.add((self.current, url))
        self.make_business_due()
        self.net.run_cycle(self.state)
        self.assertIn((TW_GROUP, self.backup), self.net.puts)

    def test_failover_records_detection_seconds_and_history(self):
        self.state["nodes"][self.current]["first_failure_at"] = int(time.time()) - 37
        self.state["nodes"][self.current]["failure_streak"] = 1
        self.net.down_nodes.add(self.current)
        self.net.run_cycle(self.state)
        group_state = self.state["groups"][TW_GROUP]
        self.assertGreaterEqual(group_state["last_failover_detect_seconds"], 37)
        self.assertEqual(len(group_state["failover_times"]), 1)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.state = {"nodes": {"n": {}}, "groups": {TW_GROUP: {}}}
        self.urls = router.BUSINESS_TEST_URLS[TW_GROUP]

    def test_preflight_probes_urls_in_parallel(self):
        def slow(name, url, timeout_ms):
            time.sleep(0.5)
            return 100
        with mock.patch.object(router, "probe_url", side_effect=slow):
            started = time.perf_counter()
            self.assertTrue(router.business_preflight(TW_GROUP, "n", self.state, now=self.now))
            elapsed = time.perf_counter() - started
        self.assertGreater(len(self.urls), 1)
        self.assertLess(elapsed, 0.85, "serial probing would take at least 1.0 s")

    def test_preflight_retries_only_failed_urls_once(self):
        calls = []

        def probe(name, url, timeout_ms):
            calls.append(url)
            if url == self.urls[0] and calls.count(url) == 1:
                return None
            return 100
        with mock.patch.object(router, "probe_url", side_effect=probe):
            self.assertTrue(router.business_preflight(TW_GROUP, "n", self.state, now=self.now))
        self.assertEqual(len(calls), len(self.urls) + 1)

    def test_preflight_ignores_target_down_urls(self):
        self.state["groups"][TW_GROUP]["business_target_down"] = {self.urls[1]: self.now + 100}
        calls = []
        with mock.patch.object(router, "probe_url", side_effect=lambda n, u, t: calls.append(u) or 100):
            self.assertTrue(router.business_preflight(TW_GROUP, "n", self.state, now=self.now))
        self.assertEqual(calls, [self.urls[0]])

    def test_preflight_fresh_skip(self):
        self.state["nodes"]["n"] = {
            "business_last_success": True, "business_checked_at": self.now - 60,
            "last_success": True, "last_probe_at": self.now,
        }
        with mock.patch.object(router, "probe_url", side_effect=AssertionError("must not probe")):
            self.assertTrue(router.business_preflight(TW_GROUP, "n", self.state, now=self.now))
        self.assertEqual(self.state["nodes"]["n"]["preflight_skipped_at"], self.now)

    def test_preflight_fresh_skip_can_be_disabled(self):
        self.state["nodes"]["n"] = {
            "business_last_success": True, "business_checked_at": self.now - 60,
            "last_success": True, "last_probe_at": self.now,
        }
        calls = []
        with mock.patch.object(router, "probe_url", side_effect=lambda n, u, t: calls.append(u) or 100):
            router.business_preflight(TW_GROUP, "n", self.state, now=self.now, allow_fresh_skip=False)
        self.assertEqual(len(calls), len(self.urls))


class HotStandbyTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.state = {"groups": {}, "nodes": {}}
        for index, name in enumerate(TW_NODES):
            self.state["nodes"][name] = cycle_harness.mature_node(80 + 10 * index, self.now)
        self.proxy_data = {policy["group_name"]: {"now": policy["static_candidates"][0]} for policy in router.POLICIES}

    def test_hot_standby_is_probed_every_cycle(self):
        for _ in range(3):
            targets = router.choose_probe_targets(self.state, self.proxy_data)
            self.assertIn(TW_NODES[1], targets)
        self.assertEqual(self.state["groups"][TW_GROUP]["hot_standby"], TW_NODES[1])

    def test_hot_standby_excludes_quarantined_and_immature(self):
        self.state["nodes"][TW_NODES[1]]["quarantine_until"] = int(self.now) + 600
        self.state["nodes"][TW_NODES[2]]["samples"] = 2
        router.choose_probe_targets(self.state, self.proxy_data)
        self.assertEqual(self.state["groups"][TW_GROUP]["hot_standby"], TW_NODES[3])

    def test_round_robin_skips_hot_standby_and_quarantined(self):
        self.state["nodes"][TW_NODES[2]]["quarantine_until"] = int(self.now) + 600
        rotated = set()
        for _ in range(len(TW_NODES) * 2):
            rotated.update(router.choose_probe_targets(self.state, self.proxy_data))
        self.assertNotIn(TW_NODES[2], rotated)

    def test_failover_storm_requires_full_preflight_and_logs_once(self):
        group = "storm-group"
        now = 1_800_000_000
        current = cycle_harness.mature_node(80, now)
        current.update({"last_success": False, "failure_streak": 2, "effective_failure_streak": 2})
        backup = cycle_harness.mature_node(60, now)
        backup.update({"last_probe_at": now, "business_checked_at": now - 10})
        state = {
            "nodes": {"current": current, "backup": backup},
            "groups": {group: {"last_seen": "current", "failover_times": [now - 100, now - 200, now - 300]}},
            "events": [],
        }
        calls = []
        with mock.patch.object(router.time, "time", return_value=now), \
                mock.patch.object(router, "select_node"), \
                mock.patch.object(router, "close_old_connections", return_value=0), \
                mock.patch.object(router, "log"), \
                mock.patch.dict(router.BUSINESS_TEST_URLS, {group: ["https://example.invalid/trace"]}), \
                mock.patch.object(router, "probe_url", side_effect=lambda n, u, t: calls.append(n) or 90):
            router.evaluate_group(group, ["current", "backup"], {group: {"now": "current"}}, [], state, False)
            state["nodes"]["current"] = dict(current)
            state["groups"][group]["last_seen"] = "current"
            router.evaluate_group(group, ["current", "backup"], {group: {"now": "current"}}, [], state, False)
        self.assertIn("backup", calls, "storm disables the fresh-result shortcut")
        storms = [event for event in state["events"] if event["code"] == "FAILOVER_STORM"]
        self.assertEqual(len(storms), 1)


if __name__ == "__main__":
    unittest.main()
