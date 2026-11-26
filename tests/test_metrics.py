import unittest

from bbu.metrics import _percentile


class MetricsPercentileTests(unittest.TestCase):
    def test_p95_never_below_p50_on_small_samples(self):
        values = [21.0, 19.0]
        self.assertEqual(_percentile(values, 0.5), 20.0)
        self.assertEqual(_percentile(values, 0.95), 20.9)

    def test_single_and_empty(self):
        self.assertEqual(_percentile([12.0], 0.95), 12.0)
        with self.assertRaises(ValueError):
            _percentile([], 0.5)

    def test_larger_sample_matches_linear_quantile(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(_percentile(values, 0.5), 5.5)
        self.assertAlmostEqual(_percentile(values, 0.95), 9.55)


if __name__ == "__main__":
    unittest.main()
