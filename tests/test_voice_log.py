from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from robotsoul.voice.log import JsonlVoiceEventLog, VoiceEventLogError


class VoiceEventLogTests(unittest.TestCase):
    def test_jsonl_log_appends_wake_and_transcript_for_the_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice_events.jsonl"
            log = JsonlVoiceEventLog(path)
            log.record(kind="wake", text="索拉", source="local", state="PASSIVE")
            log.record(kind="transcript", text="抬头", source="call", state="CALL")

            mode = stat.S_IMODE(os.stat(path).st_mode)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(mode & 0o077, 0)
            self.assertEqual([json.loads(line)["text"] for line in lines], ["索拉", "抬头"])
            self.assertEqual([item["kind"] for item in log.recent(1)], ["transcript"])

    def test_empty_text_is_rejected(self):
        log = JsonlVoiceEventLog(Path("/tmp/unused-voice-events.jsonl"))
        with self.assertRaises(VoiceEventLogError):
            log.record(kind="wake", text="  ", source="local", state="PASSIVE")


if __name__ == "__main__":
    unittest.main()
