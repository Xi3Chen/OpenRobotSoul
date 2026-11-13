from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.task_watchdog import _pid_alive, _resolve_pid


class WatchdogTests(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(_pid_alive(os.getpid()))

    def test_invalid_pid_is_not_alive(self):
        self.assertFalse(_pid_alive(-1))

    def test_pid_file_is_preferred_over_pid_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "pid"
            pid_file.write_text("%d\n" % os.getpid(), encoding="utf-8")
            self.assertEqual(_resolve_pid(1, pid_file), os.getpid())
            self.assertEqual(_resolve_pid(4242, None), 4242)
            with self.assertRaises(ValueError):
                _resolve_pid(None, None)
            pid_file.write_text("not-a-pid\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                _resolve_pid(None, pid_file)


if __name__ == "__main__":
    unittest.main()
