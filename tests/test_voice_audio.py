from __future__ import annotations

import unittest
from unittest.mock import patch

from robotsoul.voice.audio import InterruptibleOutput, PulseAudioOutput, VoiceAudioError


class FakeProcess:
    def __init__(self, command):
        self.command = command
        self.killed = False
        self.waited = False
        self.stdin = self
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    def flush(self):
        return

    def close(self):
        self.closed = True

    def kill(self):
        self.killed = True

    def poll(self):
        return 0 if self.killed else None

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def communicate(self, data, timeout=None):
        self.data = data
        return (b"", b"")


class PulseAudioStreamingTests(unittest.TestCase):
    def test_streaming_writes_all_units_into_one_process(self):
        processes = []

        def fake_popen(command, **kwargs):
            process = FakeProcess(command)
            processes.append(process)
            return process

        output = PulseAudioOutput(device="speaker")
        with patch("robotsoul.voice.audio.shutil.which", return_value="/usr/bin/pacat"), \
                patch("robotsoul.voice.audio.subprocess.Popen", side_effect=fake_popen):
            output.start(16000)
            output.write((0.0, 1.0))
            output.write((-1.0,))
            output.finish()

        self.assertEqual(len(processes), 1)
        command = processes[0].command
        self.assertIn("--rate=16000", command)
        self.assertIn("--channels=1", command)
        self.assertIn("--device=speaker", command)
        # 0.0 -> 0000, 1.0 -> ff7f, -1.0 -> 0180 (little endian s16le)
        self.assertEqual(processes[0].data, bytes([0x00, 0x00, 0xFF, 0x7F, 0x01, 0x80]))
        self.assertTrue(processes[0].closed)
        self.assertIsNone(output._process)

    def test_write_before_start_is_refused(self):
        with self.assertRaises(VoiceAudioError):
            PulseAudioOutput().write((0.0,))

    def test_stop_kills_the_streaming_process(self):
        processes = []

        def fake_popen(command, **kwargs):
            process = FakeProcess(command)
            processes.append(process)
            return process

        output = PulseAudioOutput()
        with patch("robotsoul.voice.audio.shutil.which", return_value="/usr/bin/pacat"), \
                patch("robotsoul.voice.audio.subprocess.Popen", side_effect=fake_popen):
            output.start(16000)
            output.stop()
        self.assertTrue(processes[0].killed)
        self.assertIsNone(output._process)

    def test_interruptible_output_reports_streaming_support(self):
        self.assertFalse(InterruptibleOutput(object()).supports_streaming)
        self.assertTrue(InterruptibleOutput(PulseAudioOutput()).supports_streaming)


if __name__ == "__main__":
    unittest.main()
