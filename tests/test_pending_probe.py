import concurrent.futures
import pathlib
import sys
import threading
import time
import unittest
from unittest import mock

import cycle_harness

router = cycle_harness.load_router("pending_probe_router")
policy = router.POLICIES[0]
group = policy["group_name"]
node = policy["static_candidates"][0]


class PendingProbeTests(unittest.TestCase):
    def setUp(self):
        router.reset_detection_memory()
        router.TIMELINE.clear()

    def test_late_success_updates_original_node_once_without_changing_health(self):
        future = concurrent.futures.Future()
        at = time.time()
        router.record_fast_point(group, node, None, at, suspicious=True,
                                 request_id="request-a", probe_status="pending", confirmation="transient")
        future.add_done_callback(router._stamp_done)
        future.add_done_callback(lambda done: router.enqueue_probe_completion(
            done, group, node, "request-a", at, time.monotonic() - 2))
        future.set_result(200)
        with mock.patch.object(router.probe_trace, "record") as trace:
            router.drain_probe_completions()
            router.drain_probe_completions()
        trace.assert_called_once()
        point, = router.public_timeline(group, at+3)
        self.assertEqual(point[1], 200)
        self.assertEqual(point[3], node)
        self.assertEqual(point[5:7], ["success", "transient"])
        self.assertGreaterEqual(point[7], 2000)

    def test_cancelled_probe_is_not_failed(self):
        future = concurrent.futures.Future()
        future.cancel()
        delay, status, elapsed = router.completed_probe_result(future, time.monotonic())
        self.assertIsNone(delay)
        self.assertEqual(status, "cancelled")

    def test_late_failure_keeps_retry_outcome_and_original_node(self):
        future = concurrent.futures.Future()
        at = time.time()
        router.record_fast_point(group, node, None, at, suspicious=True,
                                 request_id="request-b", probe_status="pending", confirmation="transient")
        future.add_done_callback(router._stamp_done)
        future.set_result(None)
        router.enqueue_probe_completion(future, group, node, "request-b", at, time.monotonic())
        with mock.patch.object(router.probe_trace, "record") as trace:
            router.drain_probe_completions()
        point, = router.public_timeline(group, at+1)
        self.assertEqual(point[3], node)
        self.assertEqual(point[5:7], ["failed", "transient"])
        self.assertEqual(trace.call_count, 1)

    def test_normal_tick_records_pending_then_real_success(self):
        net = cycle_harness.FakeNetwork(router)
        state = router.state_contract.new_state()
        now = time.time()
        for item in router.POLICIES:
            for name in item["static_candidates"]:
                state["nodes"][name] = cycle_harness.mature_node(80, now)
                net.latency[name] = 80
            state["groups"][item["group_name"]] = {"last_business_probe_at": now}
        net.run_cycle(state)
        samples_before = state["nodes"][node]["samples"]
        calls = []
        lock = threading.Lock()
        finished = threading.Event()

        def probe(name, url, timeout):
            with lock:
                first = name == node and not calls
                if name == node:
                    calls.append(name)
            if first:
                time.sleep(2)
                finished.set()
                return 200
            time.sleep(.03)
            return 30

        with mock.patch.object(router, "load_state", return_value=state), \
                mock.patch.object(router, "save_state"), \
                mock.patch.object(router, "update_dashboard_cache"), \
                mock.patch.object(router, "api_request", side_effect=net.api), \
                mock.patch.object(router, "probe_url", side_effect=probe), \
                mock.patch.object(router.probe_trace, "record") as trace:
            self.assertEqual(router.run_fast_tick(), "transient")
            points = router.public_timeline(group, time.time())
            pending = [p for p in points if p[5] == "pending"]
            self.assertEqual(len(pending), 1)
            self.assertFalse(finished.is_set())
            self.assertTrue(finished.wait(3))
            router.probe_pool().submit(lambda: None).result()
            # Completion callback can finish immediately after the probe sets its signal.
            deadline = time.monotonic() + 1
            while not router.PROBE_COMPLETIONS and time.monotonic() < deadline:
                time.sleep(.005)
            router.drain_probe_completions()
            matching = [p for p in router.public_timeline(group, time.time()) if p[8] == pending[0][8]]
            self.assertEqual(matching[0][5], "success")
            self.assertEqual(matching[0][1], 200)
            final = [c for c in trace.call_args_list if c.args[:2] == ("fast", node)]
            self.assertEqual(len(final), 1)
            self.assertEqual(final[0].args[2], 200)
        self.assertEqual(net.puts, [])
        self.assertEqual(state["nodes"][node]["samples"], samples_before)
