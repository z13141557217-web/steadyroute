import pathlib
import socket
import sys
import unittest
from unittest import mock

import cycle_harness

router = cycle_harness.load_router("probe_display_router")
import probe_display


class ProbeDisplayTests(unittest.TestCase):
    def setUp(self):
        probe_display.reset()

    def test_uniform_counts_and_pending_completion_are_not_duplicated(self):
        probe_display.record("group", "a", 100, 60, "1", "success", None, 80, {})
        probe_display.record("group", "a", 101, None, "2", "pending", "transient", None, None)
        point, = probe_display.snapshot("group", 103)
        self.assertIsNone(point[1])
        self.assertEqual(point[10]["counts"]["pending"], 1)
        probe_display.complete("group", "a", 101, "2", 120, "success", 2000, {})
        probe_display.complete("group", "a", 101, "2", 120, "success", 2000, {})
        point, = probe_display.snapshot("group", 103)
        self.assertEqual(point[1], 120)
        self.assertEqual(point[10]["counts"]["success"], 2)
        self.assertEqual(point[10]["min_ms"], 60)
        self.assertEqual(point[10]["max_ms"], 120)

    def test_mixed_failure_breaks_line_and_preserves_success_and_reason_counts(self):
        probe_display.record("group", "a", 100, 60, "1", "success", None, 80, {})
        probe_display.record("group", "a", 101, None, "2", "failed", "transient", 500,
                             {"status": "timeout"})
        point, = probe_display.snapshot("group", 103)
        self.assertIsNone(point[1])
        self.assertEqual(point[6], "transient")
        self.assertEqual(point[10]["latest_success_ms"], 60)
        self.assertEqual(point[10]["failure_reasons"]["timeout"], 1)

    def test_node_identity_and_retention_are_bounded(self):
        for index in range(1000):
            probe_display.record("group", "a", index*5, 80, str(index), "success", None, 90, {})
        probe_display.record("group", "b", 4995, 90, "b", "success", None, 100, {})
        points = probe_display.snapshot("group", 5000)
        self.assertLessEqual(len(points), 368)
        self.assertEqual({p[3] for p in points}, {"a", "b"})

    def test_unavailable_measurement_is_separate_from_node_failure(self):
        probe_display.record("group", "a", 100, None, "1", "unavailable", None, 200,
                             {"status": "unavailable", "reason_code": "controller_timeout"})
        point, = probe_display.snapshot("group", 101)
        self.assertEqual(point[5], "unavailable")
        self.assertEqual(point[10]["counts"].get("failed", 0), 0)


class ProbeReasonTests(unittest.TestCase):
    def measure(self, reply=None, error=None):
        observation = {}
        with mock.patch.object(router, "api_request", return_value=reply, side_effect=error):
            delay = router.timed_probe("node", "http://fixture.invalid", 500, observation)
        return delay, observation

    def test_known_timeout_and_generic_error_remain_distinct(self):
        delay, result = self.measure(error=router.ControllerHTTPError(504, "context deadline exceeded"))
        self.assertIsNone(delay)
        self.assertEqual(result["status"], "timeout")
        _, result = self.measure(error=router.ControllerHTTPError(503, "proxy handshake failed"))
        self.assertEqual(result["status"], "error")
        self.assertNotIn("handshake", str(result))

    def test_controller_timeout_is_unavailable_not_a_node_timeout(self):
        _, result = self.measure(error=socket.timeout())
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason_code"], "controller_timeout")

    def test_success_and_invalid_response_preserve_old_delay_contract(self):
        delay, result = self.measure({"delay": 80})
        self.assertEqual(delay, 80)
        self.assertEqual(result["status"], "success")
        _, result = self.measure({"delay": 0})
        self.assertEqual(result["status"], "error")
        _, result = self.measure({"delay": "bad"})
        self.assertEqual(result["status"], "unavailable")
