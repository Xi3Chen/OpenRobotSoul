from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.task_watchdog import _pid_alive


class WatchdogTests(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(_pid_alive(os.getpid()))

    def test_invalid_pid_is_not_alive(self):
        self.assertFalse(_pid_alive(-1))


if __name__ == "__main__":
    unittest.main()
