import pathlib
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

router = cycle_harness.load_router("runtime_metrics_router")
MODULE_DIR = cycle_harness.MODULE_DIR


class CycleMetricsTests(unittest.TestCase):
    def setUp(self):
        router.RUNTIME.update({"last_cycle_wall": None, "last_cycle_mono": None, "last_duration_s": 0.0})
        self.net = cycle_harness.FakeNetwork(router)
        self.state = router.state_contract.new_state()

    def test_cycle_durations_are_bounded_to_60(self):
        self.state["cycle_durations_ms"] = list(range(70))
        self.net.run_cycle(self.state)
        self.assertEqual(len(self.state["cycle_durations_ms"]), router.CYCLE_DURATION_SAMPLES)
        self.assertEqual(self.state["cycle_count"], 1)

    def test_percentiles_null_when_fewer_than_5_samples(self):
        self.state.update({"cycle_durations_ms": [100, 200], "updated_at": 1000})
        service = router.build_status_snapshots(self.state, {}, [], now=1001)["v1"]["service"]
        self.assertIsNone(service["cycle_duration_p50_ms"])
        self.state["cycle_durations_ms"] = [100, 200, 300, 400, 5000]
        service = router.build_status_snapshots(self.state, {}, [], now=1001)["v1"]["service"]
        self.assertEqual(service["cycle_duration_p50_ms"], 300)
        self.assertEqual(service["cycle_duration_p95_ms"], 5000)

    def test_switch_counters_only_count_last_24h(self):
        group = router.POLICIES[0]["group_name"]
        now = 1_800_000_000
        self.state.update({"updated_at": now})
        self.state["groups"][group] = {"failover_times": [now - 90000, now - 100]}
        payload = router.build_status_snapshots(self.state, {}, [], now=now)["v1"]
        item = next(entry for entry in payload["groups"] if entry["name"] == group)
        self.assertEqual(item["metrics"]["failovers_24h"], 1)
        self.assertEqual(item["metrics"]["performance_switches_24h"], 0)


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        router.RUNTIME.update({"last_cycle_wall": None, "last_cycle_mono": None, "last_duration_s": 0.0})
        self.net = cycle_harness.FakeNetwork(router)
        self.state = router.state_contract.new_state()

    def test_updated_at_is_cycle_completion_time(self):
        clock = iter([1000.0, 1000.0, 1000.0])

        def fake_time():
            return next(clock, 1007.0)

        with mock.patch.object(router.time, "time", side_effect=fake_time):
            self.net.run_cycle(self.state)
        self.assertEqual(self.state["last_cycle_started_at"], 1000)
        self.assertEqual(self.state["updated_at"], 1007)

    def test_stale_at_exposed_in_both_apis(self):
        self.state["updated_at"] = 5000
        snapshots = router.build_status_snapshots(self.state, {}, [], now=5010)
        self.assertEqual(snapshots["v1"]["service"]["stale_at"], 5000 + 50)
        self.assertEqual(snapshots["legacy"]["service"]["stale_at"], 5000 + 50)
        self.assertEqual(snapshots["legacy"]["service"]["stale_title"], "检测延迟")

    def test_state_staleness_threshold_is_two_intervals_plus_budget(self):
        self.state["updated_at"] = 5000
        self.assertTrue(router.build_status_snapshots(self.state, {}, [], now=5051)["v1"]["service"]["state_stale"])
        self.assertFalse(router.build_status_snapshots(self.state, {}, [], now=5049)["v1"]["service"]["state_stale"])

    def test_fixed_rate_main_loop_does_not_add_cycle_time(self):
        starts = []

        def fake_cycle(dry_run=False):
            starts.append(time.monotonic())
            time.sleep(0.3)
            if len(starts) == 3:
                raise KeyboardInterrupt

        with mock.patch.object(router, "run_cycle", side_effect=fake_cycle), \
                mock.patch.object(router, "PROBE_INTERVAL_SECONDS", 0.6), \
                mock.patch.object(router, "start_dashboard", return_value=None), \
                mock.patch.object(router, "log"), \
                mock.patch.object(router.fcntl, "flock"), \
                mock.patch.object(router.os, "makedirs"), \
                mock.patch("builtins.open", mock.mock_open()), \
                mock.patch.object(sys, "argv", ["weighted_router.py", "--daemon"]):
            with self.assertRaises(KeyboardInterrupt):
                router.main()
        gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
        # Fixed rate: ~0.6 s between starts. The old sleep-after-cycle loop gave ~0.9 s.
        for gap in gaps:
            self.assertGreater(gap, 0.5)
            self.assertLess(gap, 0.8)


class DashboardFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.source = (MODULE_DIR / "dashboard.html").read_text(encoding="utf-8")

    def test_dashboard_uses_backend_stale_deadline(self):
        self.assertIn("stale_at", self.source)
        self.assertIn("stale_title", self.source)
        self.assertIn("visibilitychange", self.source)
        self.assertNotIn("a>d.service.probe_interval_seconds", self.source)

    def test_policy_shows_in_cycle_confirmation(self):
        self.assertIn("confirm_probes", self.source)
        legacy = router.build_status_snapshots(router.state_contract.new_state(), {}, [], now=1000)["legacy"]
        self.assertEqual(legacy["policy"]["confirm_probes"], 1 + router.CONFIRM_PROBES)

    def test_dashboard_counts_cycles_not_samples(self):
        self.assertIn("cycle_count", self.source)

    def test_dashboard_shows_seconds_up_to_two_minutes(self):
        self.assertIn("s<120?`${s}秒前`", self.source)


if __name__ == "__main__":
    unittest.main()
