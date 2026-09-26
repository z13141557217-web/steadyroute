"""v0.4.6: one bookkeeping path for every switch; 24-hour counts are complete."""

import pathlib
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

router = cycle_harness.load_router("switch_bookkeeping_router")
TW = router.POLICIES[0]
GROUP, NODES = TW["group_name"], list(TW["static_candidates"])
NOW = 1790409600


def metrics(state, now):
    snapshots = router.build_status_snapshots(state, {}, [], now=now)
    legacy = next(g for g in snapshots["legacy"]["groups"] if g["name"] == GROUP)
    return legacy["metrics"]


class ApplySwitchTests(unittest.TestCase):
    def setUp(self):
        router.TIMELINE.clear()

    def test_common_fields_for_every_kind(self):
        for kind in router.SWITCH_KINDS:
            with self.subTest(kind=kind):
                group = {"better_candidate": "x", "better_streak": 2, "dynamic_no_candidate": True}
                router.apply_switch(GROUP, group, kind, "old", "new", NOW)
                self.assertEqual((group["last_router_selection"], group["last_seen"], group["last_switch_at"]),
                                 ("new", "new", NOW))
                self.assertEqual((group["better_candidate"], group["better_streak"], group["dynamic_no_candidate"]),
                                 (None, 0, False))
                self.assertEqual(group["handover_new_node"], "new")
                self.assertEqual(group[router.SWITCH_KINDS[kind][0]], [NOW])
                point = router.TIMELINE[GROUP][-1]
                self.assertEqual((point["kind"], point["node"], point["reason"]), ("switch", "new", kind))

    def test_failover_drops_old_connections_and_enters_recovery(self):
        group = {"manual_hold_until": NOW + 999}
        router.apply_switch(GROUP, group, "failover", "old", "new", NOW)
        self.assertIsNone(group["handover_old_node"])
        self.assertEqual(group["handover_grace_until"], 0)
        self.assertTrue(group["recovery_mode"])
        self.assertEqual(group["manual_hold_until"], 0)
        self.assertEqual(group["recovery_observe_until"], NOW + 60)

    def test_optimize_keeps_old_connections(self):
        group = {"recovery_mode": True}
        router.apply_switch(GROUP, group, "optimize", "old", "new", NOW)
        self.assertEqual(group["handover_old_node"], "old")
        self.assertEqual(group["handover_grace_until"], NOW + 300)
        self.assertFalse(group["recovery_mode"])

    def test_removal_keeps_connections_and_clears_a_stale_manual_hold(self):
        group = {"recovery_mode": True, "manual_hold_until": NOW + 999}
        router.apply_switch(GROUP, group, "removed", "gone", "new", NOW)
        self.assertEqual(group["handover_old_node"], "gone")
        self.assertTrue(group["recovery_mode"], "removal leaves the recovery flag as it was")
        self.assertEqual(group["manual_hold_until"], 0)


class SwitchCountTests(unittest.TestCase):
    def test_more_than_ten_failovers_a_day_are_all_counted(self):
        state = router.state_contract.new_state()
        group = state["groups"].setdefault(GROUP, {})
        for index in range(30):
            router.apply_switch(GROUP, group, "failover", NODES[0], NODES[1], NOW - 80000 + index * 2000)
        self.assertEqual(metrics(state, NOW)["failovers_24h"], 30)

    def test_history_keeps_only_the_last_day(self):
        times = router.recent_switch_times([NOW - 30 * 3600, NOW - 26 * 3600, NOW - 3600], NOW)
        self.assertEqual(times, [NOW - 3600, NOW])

    def test_history_is_capped(self):
        times = []
        for index in range(router.SWITCH_TIMES_LIMIT + 50):
            times = router.recent_switch_times(times, NOW + index)
        self.assertEqual(len(times), router.SWITCH_TIMES_LIMIT)

    def test_current_removed_switch_is_counted(self):
        now = time.time()
        state = router.state_contract.new_state()
        state["nodes"][NODES[1]] = cycle_harness.mature_node(90, now)
        state["groups"][GROUP] = {"last_seen": "gone-node"}
        with mock.patch.object(router, "select_node"), \
                mock.patch.object(router, "business_preflight", return_value=True), \
                mock.patch.object(router, "close_old_connections") as close:
            router.evaluate_group(GROUP, [NODES[1]], {GROUP: {"now": "gone-node"}}, [], state, False)
        close.assert_not_called()
        self.assertEqual(state["groups"][GROUP]["last_seen"], NODES[1])
        counted = metrics(state, int(now))
        self.assertEqual((counted["removal_switches_24h"], counted["failovers_24h"]), (1, 0))


if __name__ == "__main__":
    unittest.main()
