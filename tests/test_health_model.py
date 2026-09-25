import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src" / "steadyroute"))
import health_model  # noqa: E402


class HealthModelTests(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        self.assertEqual(health_model.percentile([10, 20, 30, 40], 0.5), 20)
        self.assertEqual(health_model.percentile([10, 20, 30, 40], 0.9), 40)
        self.assertEqual(health_model.percentile([7], 0.95), 7)

    def test_optional_percentile_needs_five_samples(self):
        self.assertIsNone(health_model.optional_percentile([1, 2, 3, 4], 0.5))
        self.assertEqual(health_model.optional_percentile([1, 2, 3, 4, 5], 0.5), 3)

    def test_next_deadline_is_fixed_rate(self):
        self.assertEqual(health_model.next_deadline(100, 20, 105), 120)

    def test_next_deadline_never_bursts(self):
        self.assertEqual(health_model.next_deadline(100, 20, 500), 500)

    def test_detect_resume_by_clock_divergence(self):
        self.assertEqual(health_model.detect_resume(0, 0, 600, 20, 20, 1), (True, 600))

    def test_detect_resume_ignores_normal_cycle(self):
        self.assertEqual(health_model.detect_resume(0, 0, 21, 21, 20, 1), (False, 21))

    def test_detect_resume_gap_fallback(self):
        self.assertTrue(health_model.detect_resume(0, 0, 200, 200, 20, 5)[0])

    def test_detect_resume_first_cycle(self):
        self.assertEqual(health_model.detect_resume(None, None, 5, 5, 20, 1), (False, 0))

    def test_count_recent_and_bounded_append(self):
        self.assertEqual(health_model.count_recent([0, 10, 95], 100, 86400), 3)
        self.assertEqual(health_model.count_recent([100 - 90000, 90], 100, 86400), 1)
        self.assertEqual(health_model.bounded_append(list(range(10)), 10, 10), list(range(1, 11)))


if __name__ == "__main__":
    unittest.main()
