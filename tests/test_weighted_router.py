import importlib.util
import os
import pathlib
import stat
import tempfile
import time
import unittest
from unittest import mock


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


class ControllerSocketTests(unittest.TestCase):
    def test_resolver_supports_new_service_socket_and_legacy_fallback(self):
        missing = "/var/run/example/missing.sock"
        available = "/var/run/example/service.sock"
        original_paths = router.CONTROLLER_SOCKET_PATHS
        original_override = os.environ.pop("STEADYROUTE_CONTROLLER_SOCKET", None)

        def fake_lstat(path):
            if path == available:
                return os.stat_result((stat.S_IFSOCK | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            raise FileNotFoundError(path)

        try:
            router.CONTROLLER_SOCKET_PATHS = (missing, available, "/tmp/verge/verge-mihomo.sock")
            with mock.patch.object(router.os, "lstat", side_effect=fake_lstat):
                self.assertEqual(router.resolve_controller_socket(), available)
        finally:
            router.CONTROLLER_SOCKET_PATHS = original_paths
            if original_override is not None:
                os.environ["STEADYROUTE_CONTROLLER_SOCKET"] = original_override

    def test_resolver_rejects_regular_files_and_relative_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            regular = pathlib.Path(directory) / "not-a-socket"
            regular.write_text("no", encoding="utf-8")
            original_paths = router.CONTROLLER_SOCKET_PATHS
            original_override = os.environ.get("STEADYROUTE_CONTROLLER_SOCKET")
            try:
                router.CONTROLLER_SOCKET_PATHS = (str(regular),)
                os.environ["STEADYROUTE_CONTROLLER_SOCKET"] = "relative.sock"
                with self.assertRaises(RuntimeError):
                    router.resolve_controller_socket()
            finally:
                router.CONTROLLER_SOCKET_PATHS = original_paths
                if original_override is None:
                    os.environ.pop("STEADYROUTE_CONTROLLER_SOCKET", None)
                else:
                    os.environ["STEADYROUTE_CONTROLLER_SOCKET"] = original_override


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

    def test_ai_line_switches_only_on_failure(self):
        group = "AI line"
        state = {
            "nodes": {"current": healthy(500), "better": healthy(100)},
            "groups": {group: {"last_seen": "current", "last_switch_at": 0, "better_candidate": "better",
                               "better_streak": router.PERFORMANCE_CONFIRMATIONS - 1}},
        }
        policy = {"group_name": group, "failover_only": True, "include_pattern": ".", "exclude_pattern": "(?!)"}
        with mock.patch.dict(router.POLICY_BY_GROUP, {group: policy}):
            router.evaluate_group(group, ["current", "better"], {group: {"now": "current"}}, [], state, False)
            self.assertEqual(self.selected, [], "a faster node is not a reason to change the AI exit IP")
            failed = healthy(500)
            failed["last_success"] = False
            failed["failure_streak"] = router.FAILURES_BEFORE_SWITCH
            state["nodes"]["current"] = failed
            router.evaluate_group(group, ["current", "better"], {group: {"now": "current"}}, [], state, False)
        self.assertEqual(self.selected, [(group, "better")])

    def _slow_line(self, **group_state):
        group = "AI line"
        state = {"nodes": {"current": healthy(500), "better": healthy(100)},
                 "groups": {group: dict({"last_seen": "current", "last_switch_at": 0}, **group_state)}}
        policy = {"group_name": group, "failover_only": True, "slow_exit": True,
                  "include_pattern": ".", "exclude_pattern": "(?!)"}
        return group, state, policy

    def _cycles(self, group, state, count, connections=()):
        for _ in range(count):
            router.evaluate_group(group, ["current", "better"], {group: {"now": "current"}},
                                  list(connections), state, False)

    def test_ai_line_leaves_a_node_that_stays_slow(self):
        group, state, policy = self._slow_line()
        live = [{"id": "live", "chains": ["current", group]}]
        with mock.patch.dict(router.POLICY_BY_GROUP, {group: policy}), \
                mock.patch.object(router.logging_setup, "write_event") as event:
            self._cycles(group, state, router.SLOW_EXIT_CYCLES - 1, live)
            self.assertEqual(self.selected, [], "three cycles are enough elsewhere; the AI line waits ten minutes")
            self.assertEqual(state["groups"][group]["slow_streak"], router.SLOW_EXIT_CYCLES - 1)
            self._cycles(group, state, 1, live)
        self.assertEqual(self.selected, [(group, "better")])
        self.assertEqual(self.closed, [], "leaving a slow node must not cut existing connections")
        self.assertEqual(state["groups"][group]["slow_streak"], 0)
        self.assertEqual(len(state["groups"][group]["performance_switch_times"]), 1)
        name, fields = event.call_args[0][0], event.call_args[1]
        self.assertEqual((name, fields["reason"], fields["held_cycles"], fields["preserved_connections"]),
                         ("optimize", "sustained_slow", router.SLOW_EXIT_CYCLES, 1))

    def test_ai_line_slow_count_needs_the_gap_to_hold(self):
        group, state, policy = self._slow_line()
        with mock.patch.dict(router.POLICY_BY_GROUP, {group: policy}):
            self._cycles(group, state, 20)
            state["nodes"]["current"] = healthy(150)     # back to normal for one cycle
            self._cycles(group, state, 1)
            self.assertEqual(state["groups"][group]["slow_streak"], 20 - router.SLOW_EXIT_MISS_PENALTY)
            self._cycles(group, state, 10)
            self.assertEqual(state["groups"][group]["slow_streak"], 0, "a node that recovered is kept")
            # a gap below the ordinary performance threshold never counts
            state["nodes"]["current"] = healthy(100 + router.MIN_ABSOLUTE_GAIN_MS - 20)
            self._cycles(group, state, router.SLOW_EXIT_CYCLES + 5)
        self.assertEqual(self.selected, [])

    def test_ai_line_slow_exit_respects_manual_choice_cooldown_and_daily_cap(self):
        now = time.time()
        for held, expected in (
                ({"manual_hold_until": now + 600}, 0),
                ({"last_switch_at": now - 60}, 0),
                ({"performance_switch_times": [now - 3600 * hours for hours in (20, 12, 2)]}, router.SLOW_EXIT_CYCLES),
        ):
            group, state, policy = self._slow_line(slow_streak=router.SLOW_EXIT_CYCLES - 1, **held)
            with mock.patch.dict(router.POLICY_BY_GROUP, {group: policy}):
                self._cycles(group, state, 3)
            self.assertEqual(self.selected, [], held)
            self.assertEqual(state["groups"][group]["slow_streak"], expected, held)
        # the oldest of the three has left the 24-hour window: allowed again
        group, state, policy = self._slow_line(
            slow_streak=router.SLOW_EXIT_CYCLES - 1,
            performance_switch_times=[now - 3600 * hours for hours in (25, 12, 2)])
        with mock.patch.dict(router.POLICY_BY_GROUP, {group: policy}):
            self._cycles(group, state, 1)
        self.assertEqual(self.selected, [(group, "better")])

    def test_ai_line_slow_exit_can_be_turned_off_and_never_delays_failover(self):
        group, state, policy = self._slow_line(slow_streak=router.SLOW_EXIT_CYCLES - 1)
        with mock.patch.dict(router.POLICY_BY_GROUP, {group: dict(policy, slow_exit=False)}):
            self._cycles(group, state, 3)
            self.assertEqual(self.selected, [])
            self.assertEqual(state["groups"][group]["slow_streak"], 0)
        group, state, policy = self._slow_line(slow_streak=4, manual_hold_until=0)
        failed = healthy(500)
        failed["last_success"] = False
        failed["failure_streak"] = router.FAILURES_BEFORE_SWITCH
        state["nodes"]["current"] = failed
        with mock.patch.dict(router.POLICY_BY_GROUP, {group: policy}):
            self._cycles(group, state, 1)
        self.assertEqual(self.selected, [(group, "better")])
        self.assertEqual(self.closed, [(group, "current")])
        self.assertEqual(state["groups"][group]["slow_streak"], 0)

    def test_ai_line_slow_exit_waits_for_a_mature_standby_and_its_preflight(self):
        group, state, policy = self._slow_line(slow_streak=router.SLOW_EXIT_CYCLES - 1)
        state["nodes"]["better"]["samples"] = router.MIN_SAMPLES_FOR_OPTIMIZATION - 1
        with mock.patch.dict(router.POLICY_BY_GROUP, {group: policy}):
            self._cycles(group, state, 1)
            self.assertEqual((self.selected, state["groups"][group]["slow_streak"]), ([], 0))
            state["nodes"]["better"] = healthy(100)
            state["groups"][group]["slow_streak"] = router.SLOW_EXIT_CYCLES - 1
            with mock.patch.object(router, "business_preflight", return_value=False):
                self._cycles(group, state, 1)
            self.assertEqual(self.selected, [], "a standby that cannot reach the AI services is not a target")
            self.assertEqual(state["groups"][group]["slow_streak"], router.SLOW_EXIT_CYCLES - 5)

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

    def test_manual_preference_pauses_optimization_but_never_failure_safety(self):
        group = "test-group"
        now = 1800000000
        current = healthy(500)
        failed = dict(current, last_success=False, failure_streak=router.FAILURES_BEFORE_SWITCH)
        state = {
            "nodes": {"manual": current, "backup": healthy(100)},
            "groups": {group: {
                "last_seen": "old", "last_router_selection": "old",
                "manual_hold_until": now + 5000,
            }},
        }
        with mock.patch.object(router.time, "time", return_value=now):
            router.evaluate_group(group, ["manual", "backup"], {group: {"now": "manual"}}, [], state, False)
        self.assertEqual(self.selected, [])
        self.assertLessEqual(state["groups"][group]["manual_hold_until"], now + router.MANUAL_HOLD_SECONDS)

        state["nodes"]["manual"] = failed
        with mock.patch.object(router.time, "time", return_value=now + 1):
            router.evaluate_group(group, ["manual", "backup"], {group: {"now": "manual"}}, [], state, False)
        self.assertEqual(self.selected, [(group, "backup")])
        self.assertEqual(state["groups"][group]["manual_hold_until"], 0)
        self.assertEqual(
            [event["code"] for event in state["events"]],
            ["MANUAL_PREFERENCE_STARTED", "MANUAL_PREFERENCE_INTERRUPTED"],
        )

    def test_quarantined_manual_node_requires_mature_backup_or_fails_closed(self):
        group = "test-group"
        now = 1800000000
        quarantined = healthy(500)
        quarantined["quarantine_until"] = now + 600
        immature = healthy(100)
        immature["samples"] = 2
        state = {
            "nodes": {"manual": quarantined, "immature": immature},
            "groups": {group: {
                "last_seen": "old", "last_router_selection": "old",
                "manual_hold_until": now + 500,
            }},
            "events": [],
        }
        with mock.patch.object(router.time, "time", return_value=now):
            router.evaluate_group(
                group, ["manual", "immature"], {group: {"now": "manual"}}, [], state, False
            )
            router.evaluate_group(
                group, ["manual", "immature"], {group: {"now": "manual"}}, [], state, False
            )
        self.assertEqual(self.selected, [])
        self.assertEqual(state["groups"][group]["manual_hold_until"], 0)
        self.assertTrue(state["groups"][group]["dynamic_no_candidate"])
        self.assertEqual(
            [event["code"] for event in state["events"]],
            ["MANUAL_PREFERENCE_STARTED", "MANUAL_PREFERENCE_INTERRUPTED"],
        )

    def test_manual_preference_expiry_event_is_emitted_once(self):
        group = "test-group"
        now = 1800000000
        state = {
            "nodes": {"current": healthy(100), "backup": healthy(200)},
            "groups": {group: {"last_seen": "current", "manual_hold_until": now - 1}},
            "events": [],
        }
        with mock.patch.object(router.time, "time", return_value=now):
            router.evaluate_group(group, ["current", "backup"], {group: {"now": "current"}}, [], state, False)
            router.evaluate_group(group, ["current", "backup"], {group: {"now": "current"}}, [], state, False)
        self.assertEqual(
            [event["code"] for event in state["events"]], ["MANUAL_PREFERENCE_EXPIRED"]
        )

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
