from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from robotsoul.voice.config import load_voice_config
from robotsoul.voice.core import AudioFrame, BoundedPcmBuffer, CaptureArbiter, CaptureState, ConversationGate, RuntimeTextExecutor
from robotsoul.voice.factory import _device_id


class FakeCapture:
    def __init__(self, available=True, fail_close=False):
        self.available = available
        self.fail_close = fail_close
        self.opened = False
        self.opens = 0
        self.closes = 0

    def probe(self):
        return self.available

    def open(self):
        self.opened = True
        self.opens += 1

    def close(self):
        self.closes += 1
        if self.fail_close:
            raise OSError("device busy")
        self.opened = False


class VoiceCoreTests(unittest.TestCase):
    def test_voice_config_is_validated_without_opening_audio(self):
        root = Path(__file__).parents[1]
        config = load_voice_config(root / "config" / "voice.json")
        self.assertEqual(config.wake_phrase, "索拉")
        self.assertEqual(
            (root / "config" / "voice.json").read_bytes(),
            (root / "robotsoul" / "data" / "voice.json").read_bytes(),
        )
        self.assertEqual(
            (root / "config" / "voice-resources.json").read_bytes(),
            (root / "robotsoul" / "data" / "voice-resources.json").read_bytes(),
        )

    def test_voice_config_rejects_unsupported_schema_version(self):
        import json
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "voice.json"
            value = json.loads((Path(__file__).parents[1] / "config" / "voice.json").read_text(encoding="utf-8"))
            value["schema_version"] = 2
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema version"):
                load_voice_config(path)
    def test_configured_usb_devices_reach_audio_adapter_with_environment_override(self):
        self.assertEqual(_device_id("usb-microphone", "ROBOTSOUL_VOICE_INPUT_DEVICE"), "usb-microphone")
        with patch.dict("os.environ", {"ROBOTSOUL_VOICE_INPUT_DEVICE": "2"}):
            self.assertEqual(_device_id("usb-microphone", "ROBOTSOUL_VOICE_INPUT_DEVICE"), 2)
        with patch.dict("os.environ", {"ROBOTSOUL_VOICE_INPUT_DEVICE": "USB Audio Device"}):
            self.assertEqual(_device_id("usb-microphone", "ROBOTSOUL_VOICE_INPUT_DEVICE"), "USB Audio Device")
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(_device_id("default", "ROBOTSOUL_VOICE_INPUT_DEVICE"))

    def test_local_capture_is_exclusive_with_call_and_old_frames_are_rejected(self):
        device = FakeCapture()
        arbiter = CaptureArbiter(device)
        self.assertEqual(arbiter.state, CaptureState.SLEEP)
        self.assertEqual(arbiter.refresh_local(), CaptureState.PASSIVE)
        old = AudioFrame(arbiter.generation, (0.1,), 16000, 0.0)
        self.assertTrue(arbiter.accepts(old, source="local"))

        call_generation = arbiter.begin_call()
        self.assertFalse(device.opened)
        self.assertFalse(arbiter.accepts(old, source="local"))
        self.assertEqual(arbiter.state, CaptureState.CALL_PENDING)
        pending_frame = AudioFrame(call_generation, (0.2,), 16000, 1.0)
        self.assertFalse(arbiter.accepts(pending_frame, source="call"))
        call_generation = arbiter.call_connected()
        call_frame = AudioFrame(call_generation, (0.2,), 16000, 1.0)
        self.assertTrue(arbiter.accepts(call_frame, source="call"))
        with self.assertRaises(RuntimeError):
            arbiter.begin_call()
        arbiter.call_disconnected()
        self.assertFalse(arbiter.accepts(call_frame, source="call"))
        self.assertEqual(arbiter.state, CaptureState.CALL_RECONNECTING)
        arbiter.call_reconnected()
        self.assertEqual(arbiter.end_call(), CaptureState.PASSIVE)
        self.assertTrue(device.opened)

    def test_failed_release_rejects_call(self):
        device = FakeCapture(fail_close=True)
        arbiter = CaptureArbiter(device)
        arbiter.refresh_local()
        with self.assertRaisesRegex(RuntimeError, "release failed"):
            arbiter.begin_call()
        self.assertEqual(arbiter.state, CaptureState.SLEEP)
        self.assertFalse(arbiter.accepts(AudioFrame(arbiter.generation, (), 16000, 0), source="call"))
        self.assertEqual(arbiter.refresh_local(), CaptureState.SLEEP)
        device.fail_close = False
        self.assertEqual(arbiter.refresh_local(), CaptureState.PASSIVE)

    def test_missing_device_enters_sleep_and_hotplug_recovers(self):
        device = FakeCapture(available=False)
        arbiter = CaptureArbiter(device)
        self.assertEqual(arbiter.refresh_local(), CaptureState.SLEEP)
        sleeping_generation = arbiter.generation
        self.assertEqual(arbiter.refresh_local(), CaptureState.SLEEP)
        self.assertEqual(arbiter.generation, sleeping_generation)
        device.available = True
        self.assertEqual(arbiter.refresh_local(), CaptureState.PASSIVE)
        active_generation = arbiter.generation
        self.assertEqual(arbiter.refresh_local(), CaptureState.PASSIVE)
        self.assertEqual(arbiter.generation, active_generation)

    def test_pcm_buffer_drops_oldest_and_runtime_work_is_off_caller_thread(self):
        buffer = BoundedPcmBuffer[int](2)
        buffer.put(1); buffer.put(2); buffer.put(3)
        self.assertEqual(buffer.drain(), (2, 3))
        caller_id = threading.get_ident()
        worker_ids = []
        executor = RuntimeTextExecutor(lambda text: worker_ids.append(threading.get_ident()) or text)
        try:
            future = executor.submit_final("向前")
            self.assertEqual(future.result(timeout=2), "向前")
        finally:
            executor.close()
        self.assertNotEqual(worker_ids[0], caller_id)

    def test_conversation_gate_expires_after_reply_or_hard_session_limit(self):
        gate = ConversationGate(follow_up_seconds=5, session_max_seconds=20)
        gate.wake(now=10)
        gate.reply_finished(now=12)
        self.assertFalse(gate.expired(now=16))
        self.assertTrue(gate.expired(now=17))
        gate.wake(now=30)
        gate.reply_finished(now=31)
        self.assertTrue(gate.expired(now=50))

    def test_follow_up_deadline_expires_without_a_new_interaction(self):
        gate = ConversationGate(follow_up_seconds=5, session_max_seconds=30)
        gate.wake(now=10)
        gate.reply_finished(now=12)
        gate.reply_finished(now=15)
        self.assertTrue(gate.expired(now=17))

    def test_sleep_releases_local_capture_without_reopening(self):
        device = FakeCapture()
        arbiter = CaptureArbiter(device)
        arbiter.refresh_local()
        self.assertEqual(arbiter.sleep(), CaptureState.SLEEP)
        self.assertFalse(device.opened)
        self.assertEqual(device.opens, 1)


if __name__ == "__main__":
    unittest.main()
