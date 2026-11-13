from __future__ import annotations

import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from robotsoul.runlog import RunLog, configure, current, log


class RunLogTests(unittest.TestCase):
    def test_daily_log_uses_time_module_and_level_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            written = RunLog(directory).write("Voice", "Info", "唤醒：索拉")
            today = datetime.now()
            path = Path(directory) / f"{today.year:04d}-{today.month:02d}-{today.day:02d}.log"
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode) & 0o077, 0)
            self.assertFalse((Path(directory) / "runtime.jsonl.1").exists())
            self.assertRegex(written, r"^\[\d{4}-\d{2}-\d{2}-\d{1,2}:\d{2}:\d{2}\.\d{3}-Voice-Info\] 唤醒：索拉$")
            self.assertEqual(path.read_text(encoding="utf-8").splitlines(), [written])

    def test_long_and_secret_text_is_collapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            line = RunLog(directory).write(
                "Dashboard",
                "Error",
                "失败\npassword=hunter2\n" + ("甲" * 1200),
            )
            self.assertNotIn("\n", line)
            self.assertNotIn("hunter2", line)
            self.assertIn("password=[redacted]", line)
            message = line.split("] ", 1)[1]
            self.assertLessEqual(len(message), 1000)

    def test_recent_filters_level_without_creating_rotation_files(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = RunLog(directory)
            sink.write("Runtime", "Info", "已连接")
            sink.write("Runtime", "Warning", "未连接")
            self.assertEqual([item.split("] ", 1)[1] for item in sink.recent(level="Warning")], ["未连接"])
            names = {path.name for path in Path(directory).iterdir()}
            self.assertTrue(all(name.endswith(".log") for name in names))
            self.assertNotIn("runtime.jsonl.1", names)

    def test_failed_write_does_not_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            previous = current().directory
            configure(blocker / "logs")
            try:
                log("Voice", "Info", "still running")
            finally:
                configure(previous)


if __name__ == "__main__":
    unittest.main()
