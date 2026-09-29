"""v0.5.0: a freshly started process tells a boot, a sleep and a plain service restart apart
by asking macOS, instead of calling every gap over 60 seconds a sleep."""

import os
import pathlib
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402

import health_model  # noqa: E402
import runtime_metrics  # noqa: E402

router = cycle_harness.load_router("start_kind_router")
NOW = 1790409600


class ClassifyTests(unittest.TestCase):
    def classify(self, power, last=NOW - 65):
        return health_model.classify_start(last, NOW, power, 60)

    def test_restart_while_awake(self):
        # Deploy: stopped for 65 s, the Mac last woke hours ago.
        self.assertEqual(self.classify({"boot": NOW - 86400, "sleep": NOW - 20000, "wake": NOW - 19000}), ("restart", 65))

    def test_restart_after_never_sleeping(self):
        self.assertEqual(self.classify({"boot": NOW - 3600, "sleep": None, "wake": None}), ("restart", 65))

    def test_even_a_short_sleep_is_a_sleep(self):
        self.assertEqual(self.classify({"boot": NOW - 86400, "sleep": NOW - 40, "wake": NOW - 10}, last=NOW - 45),
                         ("sleep", 45))

    def test_boot(self):
        self.assertEqual(self.classify({"boot": NOW - 50, "sleep": None, "wake": None}, last=NOW - 7200), ("boot", 7200))

    def test_wake_long_before_this_start_is_a_restart(self):
        # Stopped before a sleep, started again long after waking: the network has settled.
        self.assertEqual(self.classify({"boot": NOW - 86400, "sleep": NOW - 9000, "wake": NOW - 3000}, last=NOW - 9100),
                         ("restart", 9100))

    def test_without_the_os_record_the_old_rule_applies(self):
        self.assertEqual(self.classify(None), ("sleep", 65))
        self.assertEqual(self.classify(None, last=NOW - 30), ("restart", 30))
        self.assertEqual(health_model.classify_start(0, NOW, None, 60), (None, 0))


class RouterStartTests(unittest.TestCase):
    def setUp(self):
        router.RUNTIME.update({"last_cycle_wall": None, "last_cycle_mono": None, "last_duration_s": 0.0})
        self.now = int(time.time())
        self.state = router.state_contract.new_state()
        self.state["updated_at"] = self.now - 65

    def start(self, power):
        with mock.patch.object(router.runtime_metrics, "power_times", return_value=power), \
                mock.patch.object(router, "log"), \
                mock.patch.object(router.logging_setup, "write_event", autospec=True) as events:
            result = router.detect_cycle_resume(self.state, self.now)
        self.events = [call.args[0] for call in events.call_args_list]
        return result

    def test_service_restart_does_not_pause_decisions_or_count_as_sleep(self):
        result = self.start({"boot": self.now - 86400, "sleep": None, "wake": None})
        self.assertFalse(result)
        self.assertNotIn("last_resume_at", self.state)
        self.assertEqual(self.state["last_start"], {"kind": "restart", "at": self.now, "gap_seconds": 65})
        self.assertEqual(self.events, ["service_start"])
        snapshots = router.build_status_snapshots(self.state, {}, [], now=self.now)
        service = snapshots["legacy"]["service"]
        self.assertEqual(service["last_start"]["kind"], "restart")
        self.assertNotEqual(service["state_code"], "resume_recovery")

    def test_boot_observes_one_cycle_without_a_sleep_record(self):
        self.state["updated_at"] = self.now - 7200
        result = self.start({"boot": self.now - 40, "sleep": None, "wake": None})
        self.assertEqual(result, "boot")
        self.assertNotIn("last_resume_at", self.state)
        self.state["updated_at"] = self.now
        self.state["last_cycle_started_at"] = self.now
        service = router.build_status_snapshots(self.state, {}, [], now=self.now)["legacy"]["service"]
        self.assertEqual((service["state_code"], service["state_title"]), ("boot_recovery", "Mac 刚开机"))

    def test_sleep_while_stopped_is_still_a_resume(self):
        result = self.start({"boot": self.now - 86400, "sleep": self.now - 60, "wake": self.now - 5})
        self.assertTrue(result)
        self.assertEqual(self.state["last_resume_at"], self.now)

    def test_running_process_keeps_the_two_clock_rule(self):
        self.start({"boot": self.now - 86400, "sleep": None, "wake": None})
        with mock.patch.object(router.runtime_metrics, "power_times", side_effect=AssertionError("not asked")):
            self.assertFalse(router.detect_cycle_resume(self.state, self.now + 20))


@unittest.skipUnless(sys.platform == "darwin", "macOS keeps the boot / sleep / wake record")
class RealMacTests(unittest.TestCase):
    def test_macos_reports_boot_sleep_and_wake(self):
        power = runtime_metrics.power_times()
        self.assertIsNotNone(power, "sysctl kern.boottime / sleeptime / waketime must be readable")
        if os.environ.get("GITHUB_ACTIONS"):
            # Visible as a CI annotation, so the real macOS values can be checked without logs.
            print("::notice title=macOS power record::boot %.0fs ago, sleep=%s, wake=%s" % (
                time.time() - power["boot"], power["sleep"], power["wake"]))
        now = time.time()
        self.assertLess(power["boot"], now)
        self.assertGreater(power["boot"], now - 400 * 86400)
        for key in ("sleep", "wake"):
            if power[key] is not None:
                self.assertGreaterEqual(power[key], power["boot"] - 1)
                self.assertLessEqual(power[key], now + 5)


class EventFieldTests(unittest.TestCase):
    def test_an_event_field_may_be_called_kind(self):
        # write_event's own first parameter is positional-only, so a field named "kind"
        # can never collide with it again (it did twice in this release).
        lines = []
        with mock.patch.object(router.logging_setup.logging.getLogger(router.logging_setup.EVENTS_LOGGER_NAME),
                               "info", side_effect=lines.append):
            router.logging_setup.write_event("service_start", kind="restart", gap_seconds=1)
        record = __import__("json").loads(lines[0])
        self.assertEqual((record["kind"], record["kind_field"], record["gap_seconds"]), ("service_start", "restart", 1))


class OtherPlatformTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "darwin", "macOS has the record")
    def test_no_record_elsewhere(self):
        self.assertIsNone(runtime_metrics.power_times())


if __name__ == "__main__":
    unittest.main()
