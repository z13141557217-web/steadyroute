"""v0.4.7: a lid-closed hour with Power Nap / Bluetooth wake-ups is reported as one hour."""

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cycle_harness  # noqa: E402
import health_model  # noqa: E402

router = cycle_harness.load_router("sleep_episode_router")
T0 = 1790496000


class MergeTests(unittest.TestCase):
    def test_first_sleep_starts_an_episode(self):
        self.assertEqual(health_model.merge_sleep_episode(None, T0, T0 + 600),
                         {"start": T0, "last_wake": T0 + 600, "brief_wakes": 0})

    def test_short_wake_continues_the_episode(self):
        episode = health_model.merge_sleep_episode(None, T0, T0 + 1800)
        episode = health_model.merge_sleep_episode(episode, T0 + 1840, T0 + 3600)
        self.assertEqual(episode, {"start": T0, "last_wake": T0 + 3600, "brief_wakes": 1})

    def test_real_use_between_sleeps_starts_a_new_episode(self):
        episode = health_model.merge_sleep_episode(None, T0, T0 + 1800)
        episode = health_model.merge_sleep_episode(episode, T0 + 1800 + 1200, T0 + 4000)
        self.assertEqual(episode, {"start": T0 + 3000, "last_wake": T0 + 4000, "brief_wakes": 0})

    def test_clock_going_backwards_does_not_merge(self):
        episode = {"start": T0, "last_wake": T0 + 900, "brief_wakes": 0}
        self.assertEqual(health_model.merge_sleep_episode(episode, T0 + 100, T0 + 200)["start"], T0 + 100)


class CycleResumeTests(unittest.TestCase):
    """Drive detect_cycle_resume with a wall clock that runs during sleep and a monotonic
    clock that does not, exactly as on macOS."""

    def setUp(self):
        router.RUNTIME.clear()
        router.RUNTIME.update({"last_cycle_wall": None, "last_cycle_mono": None, "last_duration_s": 0.5})
        self.state = router.state_contract.new_state()
        self.mono = 1000.0

    def cycle(self, wall, awake_seconds=0.0):
        self.mono += awake_seconds
        with mock.patch.object(router.time, "time", return_value=wall), \
                mock.patch.object(router.time, "monotonic", return_value=self.mono), \
                mock.patch.object(router.logging_setup, "write_event"):
            slept = router.detect_cycle_resume(self.state, int(wall))
        self.state["updated_at"] = int(wall)
        return slept

    def test_hour_with_two_power_nap_wakes_reads_as_one_hour(self):
        self.cycle(T0)                                  # awake, then the lid closes
        self.assertTrue(self.cycle(T0 + 1500))          # Power Nap wake-up: one cycle
        self.cycle(T0 + 1520, awake_seconds=20)
        self.assertTrue(self.cycle(T0 + 2900))          # another
        self.cycle(T0 + 2920, awake_seconds=20)
        self.assertTrue(self.cycle(T0 + 3660))          # the user opens the lid
        self.assertEqual(self.state["last_resume_at"], T0 + 3660)
        self.assertEqual(self.state["last_sleep_gap_seconds"], 3660)
        self.assertEqual(self.state["last_sleep_brief_wakes"], 2)

    def test_separate_sleeps_are_not_merged(self):
        self.cycle(T0)
        self.assertTrue(self.cycle(T0 + 600))
        for step in range(1, 40):                       # 13 minutes of real use
            self.cycle(T0 + 600 + step * 20, awake_seconds=20)
        self.assertTrue(self.cycle(T0 + 600 + 39 * 20 + 900))
        self.assertEqual(self.state["last_sleep_gap_seconds"], 900)
        self.assertEqual(self.state["last_sleep_brief_wakes"], 0)

    def test_dashboard_gets_the_brief_wake_count(self):
        self.cycle(T0)
        self.cycle(T0 + 1500)
        self.cycle(T0 + 3000, awake_seconds=0)
        legacy = router.build_status_snapshots(self.state, {}, [], now=T0 + 3001)["legacy"]["service"]
        self.assertEqual((legacy["last_sleep_gap_seconds"], legacy["last_sleep_brief_wakes"]), (3000, 1))


if __name__ == "__main__":
    unittest.main()
