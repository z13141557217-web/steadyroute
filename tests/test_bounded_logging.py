"""v0.4.4: router-side logging, memory metrics, cycle count merge and snapshot patching."""

import json
import pathlib
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402
import runtime_metrics  # noqa: E402

router = cycle_harness.load_router("bounded_logging_router")
TW = router.POLICIES[0]
TW_GROUP, TW_NODES = TW["group_name"], list(TW["static_candidates"])
MODULE_DIR = cycle_harness.MODULE_DIR


def mature_state(now):
    state = router.state_contract.new_state()
    for policy in router.POLICIES:
        for index, name in enumerate(policy["static_candidates"]):
            state["nodes"][name] = cycle_harness.mature_node(80 + 10 * index, now)
        state["groups"][policy["group_name"]] = {"last_business_probe_at": int(now)}
    return state


class Base(unittest.TestCase):
    def setUp(self):
        router.RUNTIME.clear()
        router.RUNTIME.update({"last_cycle_wall": None, "last_cycle_mono": None, "last_duration_s": 0.0})
        router.TIMELINE.clear()
        self.now = time.time()
        self.net = cycle_harness.FakeNetwork(router)
        self.state = mature_state(self.now)
        for policy in router.POLICIES:
            for index, name in enumerate(policy["static_candidates"]):
                self.net.latency[name] = 80 + 10 * index
        patcher = mock.patch.object(router.logging_setup, "write_event")
        self.events = patcher.start()
        self.addCleanup(patcher.stop)
        node_patcher = mock.patch.object(router.logging_setup, "write_node_event")
        self.node_events = node_patcher.start()
        self.addCleanup(node_patcher.stop)

    def kinds(self):
        return [call.args[0] for call in self.events.call_args_list]


class EventStreamTests(Base):
    def test_failover_is_recorded_with_decision_context(self):
        self.net.run_cycle(self.state)
        self.net.down_nodes.add(TW_NODES[0])
        self.net.run_fast_tick(self.state)
        call = next(call for call in self.events.call_args_list if call.args[0] == "failover")
        self.assertEqual(call.kwargs["group"], TW_GROUP)
        self.assertEqual(call.kwargs["from"], TW_NODES[0])
        self.assertIn(call.kwargs["to"], TW_NODES, "same-region only")
        self.assertEqual(call.kwargs["lane"], "fast")

    def test_state_events_are_mirrored_once(self):
        state = {"events": [{"code": "A", "occurred_at": 1}]}
        router.sync_state_events(state)          # first call only seeds
        self.assertEqual(self.events.call_count, 0)
        state["events"].append({"code": "B", "occurred_at": 2})
        router.sync_state_events(state)
        router.sync_state_events(state)
        self.assertEqual(self.events.call_count, 1)
        self.assertEqual(self.events.call_args.kwargs["code"], "B")

    def test_node_lifecycle_events_go_to_the_node_log(self):
        state = {"events": []}
        router.sync_state_events(state)
        state["events"] = [
            {"code": "NODE_DEGRADED", "scope": "node", "occurred_at": 1},
            {"code": "group_state_changed", "scope": "group", "occurred_at": 1},
        ]
        router.sync_state_events(state)
        self.assertEqual([c.kwargs["code"] for c in self.node_events.call_args_list], ["NODE_DEGRADED"])
        self.assertEqual([c.kwargs["code"] for c in self.events.call_args_list], ["group_state_changed"])

    def test_restart_does_not_rewrite_old_events(self):
        state = {"events": [{"code": "A", "occurred_at": 1}, {"code": "B", "occurred_at": 2}]}
        router.sync_state_events(state, write=False)
        router.sync_state_events(state)
        self.assertEqual(self.events.call_count, 0)

    def test_event_keys_are_bounded(self):
        router.sync_state_events({"events": []})
        for index in range(router.EVENT_KEY_LIMIT + 200):
            router.sync_state_events({"events": [{"code": "X", "occurred_at": index}]})
        self.assertLessEqual(len(router.RUNTIME["event_keys"]), router.EVENT_KEY_LIMIT)

    def test_publish_mirrors_local_network_events(self):
        self.net.run_cycle(self.state)
        self.net.local_offline = True
        with mock.patch.object(router, "save_state"), mock.patch.object(router, "log"):
            with mock.patch.object(router, "api_request", side_effect=self.net.api), \
                    mock.patch.object(router, "probe_url", side_effect=self.net.probe), \
                    mock.patch.object(router, "load_state", return_value=self.state):
                router.run_fast_tick()
        codes = [call.kwargs.get("code") for call in self.events.call_args_list if call.args[0] == "state_event"]
        self.assertIn("LOCAL_NETWORK_OFFLINE", codes)


class RoutineLogTests(Base):
    def test_steady_state_keep_lines_are_rate_limited(self):
        lines = []
        with mock.patch.object(router.LOGGER, "info", side_effect=lines.append):
            for _ in range(10):
                with mock.patch.object(router, "load_state", return_value=self.state), \
                        mock.patch.object(router, "save_state"), \
                        mock.patch.object(router, "api_request", side_effect=self.net.api), \
                        mock.patch.object(router, "probe_url", side_effect=self.net.probe):
                    router.run_cycle(dry_run=False)
        # 10 unchanged cycles: each routine key is written once, not ten times.
        self.assertLess(len(lines), 10)

    def test_probe_failure_is_logged_every_time(self):
        self.net.run_cycle(self.state)
        self.net.node_url_failures.add((TW_NODES[0], router.TEST_URL))
        lines = []
        with mock.patch.object(router.LOGGER, "info", side_effect=lines.append):
            for _ in range(3):
                with mock.patch.object(router, "load_state", return_value=self.state), \
                        mock.patch.object(router, "save_state"), \
                        mock.patch.object(router, "api_request", side_effect=self.net.api), \
                        mock.patch.object(router, "probe_url", side_effect=self.net.probe):
                    router.run_cycle(dry_run=False)
        self.assertEqual(sum(1 for line in lines if line.startswith("probe:") and "FAIL" in line), 3)


class QuietServerTests(unittest.TestCase):
    def test_broken_pipe_is_silent(self):
        server = router.QuietHTTPServer.__new__(router.QuietHTTPServer)
        with mock.patch.object(router, "log_warning") as warn:
            try:
                raise BrokenPipeError(32, "Broken pipe")
            except BrokenPipeError:
                server.handle_error(None, ("127.0.0.1", 1))
        warn.assert_not_called()

    def test_unexpected_error_is_logged_once(self):
        server = router.QuietHTTPServer.__new__(router.QuietHTTPServer)
        with mock.patch.object(router, "log_warning") as warn:
            try:
                raise ValueError("bad")
            except ValueError:
                server.handle_error(None, ("127.0.0.1", 1))
        warn.assert_called_once()

    def test_handler_write_swallows_disconnect(self):
        handler = router.DashboardHandler.__new__(router.DashboardHandler)
        handler.wfile = mock.Mock()
        handler.wfile.write.side_effect = ConnectionResetError()
        handler._send(b"x")
        self.assertTrue(handler.close_connection)

    def test_dashboard_serves_over_real_socket(self):
        import http.client
        import threading
        router.update_dashboard_cache(router.build_status_snapshots(router.state_contract.new_state(), {}, [], now=1000))
        server = router.QuietHTTPServer(("127.0.0.1", 0), router.DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            connection.request("GET", "/api/v1/status")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("memory_current_mb", json.loads(response.read())["service"])
        finally:
            server.shutdown()
            server.server_close()


class MemoryTests(unittest.TestCase):
    def setUp(self):
        router.RUNTIME.clear()

    def test_samples_every_ten_minutes_and_bounded(self):
        state = {}
        with mock.patch.object(router.runtime_metrics, "current_footprint_mb", return_value=30.0):
            for step in range(400):
                router.record_memory_sample(state, 1000 + step * 60)
        samples = state["memory_samples"]
        self.assertEqual(len(samples), 40)
        self.assertTrue(all(b[0] - a[0] >= router.MEMORY_SAMPLE_SECONDS for a, b in zip(samples, samples[1:])))
        with mock.patch.object(router.runtime_metrics, "current_footprint_mb", return_value=30.0):
            for step in range(300):
                router.record_memory_sample(state, 100000 + step * 600)
        self.assertEqual(len(state["memory_samples"]), router.MEMORY_SAMPLE_LIMIT)

    def test_samples_restart_with_the_process(self):
        state = {"memory_samples_started_at": router.SERVICE_STARTED_AT - 1, "memory_samples": [[1, 99.0]]}
        with mock.patch.object(router.runtime_metrics, "current_footprint_mb", return_value=30.0):
            router.record_memory_sample(state, 5000)
        self.assertEqual(state["memory_samples"], [[5000, 30.0]])

    def test_unavailable_footprint_records_nothing(self):
        state = {}
        with mock.patch.object(router.runtime_metrics, "current_footprint_mb", return_value=None):
            router.record_memory_sample(state, 5000)
        self.assertNotIn("memory_samples", state)
        self.assertIsNone(router.RUNTIME["memory_current_mb"])

    def test_snapshot_exposes_current_peak_and_trend(self):
        state = router.state_contract.new_state()
        state["memory_samples_started_at"] = router.SERVICE_STARTED_AT
        state["memory_samples"] = [[index * 600, 30.0 + index * 0.1] for index in range(24)]
        router.RUNTIME["memory_current_mb"] = 31.5
        snapshots = router.build_status_snapshots(state, {}, [], now=20000, memory_mb=33.0)
        service = snapshots["v1"]["service"]
        self.assertEqual((service["memory_current_mb"], service["memory_peak_mb"], service["memory_mb"]), (31.5, 33.0, 33.0))
        self.assertAlmostEqual(service["memory_trend_mb_per_hour"], 0.6, places=2)
        self.assertEqual(snapshots["legacy"]["service"]["memory_current_mb"], 31.5)


class RuntimeMetricsTests(unittest.TestCase):
    def test_trend_needs_two_hours(self):
        self.assertIsNone(runtime_metrics.trend_mb_per_hour([[i * 600, 30.0] for i in range(11)]))

    def test_trend_flat_and_rising(self):
        self.assertEqual(runtime_metrics.trend_mb_per_hour([[i * 600, 30.0] for i in range(20)]), 0.0)
        rising = [[i * 600, 30.0 + i * 0.5] for i in range(20)]
        self.assertAlmostEqual(runtime_metrics.trend_mb_per_hour(rising), 3.0, places=3)

    def test_trend_ignores_missing_values(self):
        samples = [[i * 600, 30.0] for i in range(20)] + [[99999, None]]
        self.assertEqual(runtime_metrics.trend_mb_per_hour(samples), 0.0)

    def test_peak_and_current_are_positive_numbers(self):
        self.assertGreater(runtime_metrics.peak_rss_mb(), 1)
        current = runtime_metrics.current_footprint_mb()
        if sys.platform in ("darwin",) or sys.platform.startswith("linux"):
            self.assertIsNotNone(current)
            self.assertGreater(current, 1)

    def test_unknown_platform_returns_none(self):
        with mock.patch.object(runtime_metrics.sys, "platform", "win32"):
            self.assertIsNone(runtime_metrics.current_footprint_mb())


class CycleCountMergeTests(Base):
    def test_old_sample_count_is_carried_over_once(self):
        self.state["cycle_count"] = 500
        self.state["nodes"][TW_NODES[0]]["samples"] = 19262
        self.net.run_cycle(self.state)
        self.assertEqual(self.state["cycle_count"], 19263)
        self.assertTrue(self.state["cycle_count_merged"])
        self.net.run_cycle(self.state)
        self.assertEqual(self.state["cycle_count"], 19264)

    def test_fresh_install_counts_from_one(self):
        state = router.state_contract.new_state()
        self.net.run_cycle(state)
        self.assertEqual(state["cycle_count"], 1)

    def test_merge_never_lowers_the_count(self):
        self.state["cycle_count"] = 30000
        self.net.run_cycle(self.state)
        self.assertEqual(self.state["cycle_count"], 30001)


class SnapshotPatchTests(Base):
    def publish(self):
        with mock.patch.object(router, "save_state"):
            router.publish_state(self.state, self.net.proxy_data, [], self.now)

    def test_fast_tick_patches_live_fields_without_rebuilding(self):
        self.publish()
        before = json.loads(router.read_dashboard_cache("legacy"))
        router.RUNTIME["last_probe_at"] = int(self.now) + 5
        router.timeline_add(TW_GROUP, {"t": self.now + 5, "ms": 77, "node": TW_NODES[0], "kind": "probe"})
        with mock.patch.object(router, "build_status_snapshots", side_effect=AssertionError("rebuilt")):
            router.refresh_cached_snapshot(now=int(self.now) + 5)
        legacy = json.loads(router.read_dashboard_cache("legacy"))
        v1 = json.loads(router.read_dashboard_cache("v1"))
        self.assertNotEqual(legacy["snapshot_id"], before["snapshot_id"])
        self.assertEqual(legacy["snapshot_id"], v1["diagnostics"]["snapshot_id"])
        self.assertEqual(legacy["service"]["last_probe_at"], int(self.now) + 5)
        self.assertEqual(v1["service"]["last_probe_at"], int(self.now) + 5)
        group = next(item for item in legacy["groups"] if item["name"] == TW_GROUP)
        self.assertEqual(group["timeline"][-1][1], 77)
        v1_group = next(item for item in v1["groups"] if item["name"] == TW_GROUP)
        self.assertTrue(v1_group["recent_probes"])
        self.assertEqual(legacy["nodes"], before["nodes"])

    def test_refresh_before_first_cycle_is_a_no_op(self):
        router.refresh_cached_snapshot()


class ProbePoolTests(unittest.TestCase):
    def test_pool_is_reused_across_batches(self):
        with mock.patch.object(router, "probe_url", return_value=50):
            router.run_probe_jobs([("a", "n", "u", 1000)])
            first = router.PROBE_POOL
            router.run_probe_jobs([("a", "n", "u", 1000), ("b", "m", "u", 1000)])
        self.assertIs(router.PROBE_POOL, first)

    def test_batches_larger_than_the_pool_still_complete(self):
        jobs = [("k%d" % index, "n%d" % index, "u", 1000) for index in range(router.PROBE_WORKERS * 3)]
        with mock.patch.object(router, "probe_url", return_value=50):
            results = router.run_probe_jobs(jobs)
        self.assertEqual(len(results), len(jobs))


class TransitionBookkeepingTests(Base):
    """Regression: a recovery-mode optimisation used to abort the whole cycle (v0.4.4 stress run)."""

    def test_post_switch_states_can_hand_over(self):
        for previous in ("cooldown", "recovery_observing", "handover_grace", "degraded", "failover_now"):
            state = router.state_contract.new_state()
            with self.subTest(previous=previous), mock.patch.object(router, "log_warning") as warn:
                router.record_projected_transition(
                    state, "group", "group-tw", previous, "handover_pending", 1000,
                    explicit_steps=[("handover_pending", "confirmation_complete")])
                warn.assert_not_called()
                self.assertEqual(state["events"][0]["to_state"], "handover_pending")

    def test_unmodelled_transition_never_raises(self):
        state = router.state_contract.new_state()
        with mock.patch.object(router, "log_warning") as warn:
            router.record_projected_transition(state, "node", "node-x", "retired", "healthy", 1000)
        warn.assert_called_once()
        self.assertEqual(state["events"][-1]["reason_code"], "unmodelled_transition")

    def test_snapshot_survives_cooldown_to_handover(self):
        group_state = self.state["groups"][TW_GROUP]
        group_state.update({"decision_code": "cooldown", "pending_decision_events": [
            {"code": "handover_pending", "reason_code": "confirmation_complete", "occurred_at": int(self.now)}]})
        router.build_status_snapshots(self.state, self.net.proxy_data, [], now=int(self.now))
        self.assertEqual(self.state["groups"][TW_GROUP]["pending_decision_events"], [])


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.source = (MODULE_DIR / "dashboard.html").read_text(encoding="utf-8")

    def test_polls_every_two_seconds(self):
        self.assertIn("setInterval(refresh,2000)", self.source)
        self.assertNotIn("setInterval(refresh,5000)", self.source)

    def test_focus_filter_is_named_needs_attention(self):
        self.assertIn(">需关注<", self.source)
        self.assertNotIn(">重点<", self.source)

    def test_memory_shows_current_and_peak(self):
        self.assertIn("memory_current_mb", self.source)
        self.assertIn("内存占用", self.source)
        self.assertIn("峰值 ${peak.toFixed(1)} MB", self.source)

    def test_last_resume_note(self):
        self.assertIn("上次休眠恢复", self.source)
        self.assertIn("last_sleep_gap_seconds", self.source)


if __name__ == "__main__":
    unittest.main()
