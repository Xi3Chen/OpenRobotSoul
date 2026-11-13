from __future__ import annotations

import unittest

from robotsoul.voice.monitor import AudioLevelMonitor


class AudioLevelMonitorTests(unittest.TestCase):
    def test_input_and_output_keep_separate_envelopes(self):
        monitor = AudioLevelMonitor(buckets=4, window_seconds=3)
        monitor.push_input((0.0, 0.5, -0.25), 1)
        monitor.push_output((0.8,), 1)
        snapshot = monitor.snapshot()

        self.assertEqual(snapshot["input"], [{"min": -0.25, "max": 0.5}])
        self.assertEqual(snapshot["output"], [{"min": 0.8, "max": 0.8}])
        self.assertTrue(snapshot["input_active"])
        self.assertTrue(snapshot["output_active"])

    def test_non_finite_samples_are_ignored(self):
        monitor = AudioLevelMonitor(window_seconds=1)
        monitor.push_input((float("nan"), float("inf"), 0.2), 1)
        self.assertEqual(monitor.snapshot()["input"], [{"min": 0.2, "max": 0.2}])


if __name__ == "__main__":
    unittest.main()
