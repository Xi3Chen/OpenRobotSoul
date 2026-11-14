from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from robotsoul.voice.audio import resolve_pulse_source
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

    def test_go2_output_is_allowed_and_cannot_be_an_input(self):
        import json
        from tempfile import TemporaryDirectory

        from robotsoul.voice.factory import resolve_playback_output
        from robotsoul.voice.outputs import GO2_OUTPUT, unregister_output

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "voice.json"
            value = json.loads((Path(__file__).parents[1] / "config" / "voice.json").read_text(encoding="utf-8"))
            value["output_device"] = "unitree-go2"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(load_voice_config(path).output_device, "unitree-go2")
            value["input_device"] = "unitree-go2"
            value["output_device"] = "default"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "input_device"):
                load_voice_config(path)
        unregister_output(GO2_OUTPUT)
        system = object()
        self.assertIs(resolve_playback_output(None, system), system)
        with self.assertRaisesRegex(RuntimeError, "OPENROBOTSOUL_GO2_WEBRTC_IP"):
            resolve_playback_output("unitree-go2", system)
    def test_configured_usb_devices_reach_audio_adapter_with_environment_override(self):
        self.assertEqual(_device_id("usb-microphone", "ROBOTSOUL_VOICE_INPUT_DEVICE"), "usb-microphone")
        with patch.dict("os.environ", {"ROBOTSOUL_VOICE_INPUT_DEVICE": "2"}):
            self.assertEqual(_device_id("usb-microphone", "ROBOTSOUL_VOICE_INPUT_DEVICE"), 2)
        with patch.dict("os.environ", {"ROBOTSOUL_VOICE_INPUT_DEVICE": "USB Audio Device"}):
            self.assertEqual(_device_id("usb-microphone", "ROBOTSOUL_VOICE_INPUT_DEVICE"), "USB Audio Device")
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(_device_id("default", "ROBOTSOUL_VOICE_INPUT_DEVICE"))

    def test_usb_microphone_prefers_the_wireless_receiver_source(self):
        listing = (
            "0\talsa_input.usb-Generic_USB_Audio-00.analog-stereo\tmodule-alsa-card.c\ts16le 2ch 48000Hz\tSUSPENDED\n"
            "1\talsa_input.usb-DJI_Technology_Co.__Ltd._Wireless_Mic_Rx_XSP12345678B-01.analog-stereo\tmodule-alsa-card.c\ts24le 2ch 48000Hz\tSUSPENDED\n"
            "2\talsa_output.platform-sound.analog-stereo.monitor\tmodule-alsa-card.c\ts16le 2ch 44100Hz\tSUSPENDED\n"
        )
        completed = type("Completed", (), {"returncode": 0, "stdout": listing})()
        with patch("robotsoul.voice.audio.shutil.which", return_value="pactl"), patch(
            "robotsoul.voice.audio.subprocess.run", return_value=completed
        ):
            source = resolve_pulse_source("usb-microphone")
        self.assertIn("Wireless_Mic_Rx", source or "")

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

    def test_newer_transcript_skips_queued_text_and_cancels_the_active_task(self):
        started = threading.Event()
        release = threading.Event()
        seen = []
        cancelled = []

        def run_text(text, task_id=None):
            seen.append((text, task_id))
            if text == "第一句":
                started.set()
                release.wait(2)
            return text

        executor = RuntimeTextExecutor(run_text, cancel_task=lambda task_id: cancelled.append(task_id))
        try:
            executor.submit_final("第一句")
            self.assertTrue(started.wait(2))
            executor.submit_final("第二句")
            executor.supersede()
            third = executor.submit_final("第三句")
            release.set()
            self.assertEqual(third.result(timeout=2), "第三句")
        finally:
            executor.close()
        self.assertEqual([item[0] for item in seen], ["第一句", "第三句"])
        self.assertEqual(cancelled, [seen[0][1]])

    def test_conversation_gate_expires_after_reply_or_hard_session_limit(self):
        gate = ConversationGate(follow_up_seconds=5, session_max_seconds=20)
        gate.wake(now=10)
        gate.reply_finished(now=12)
        self.assertFalse(gate.expired(now=16))
        self.assertTrue(gate.expired(now=17))
        gate.wake(now=30)
        gate.reply_finished(now=31)
        self.assertTrue(gate.expired(now=50))

    def test_wake_without_command_returns_to_listening_before_the_session_limit(self):
        gate = ConversationGate(follow_up_seconds=5, session_max_seconds=30)
        gate.wake(now=10)
        self.assertFalse(gate.expired(now=14))
        self.assertTrue(gate.expired(now=15))
        self.assertFalse(gate.active)

    def test_command_audio_keeps_the_session_until_the_hard_limit(self):
        gate = ConversationGate(follow_up_seconds=5, session_max_seconds=30)
        gate.wake(now=10)
        gate.note_command_audio()
        self.assertFalse(gate.expired(now=20))
        self.assertTrue(gate.expired(now=40))

    def test_follow_up_restarts_after_another_idle_reply(self):
        gate = ConversationGate(follow_up_seconds=5, session_max_seconds=30)
        gate.wake(now=10)
        self.assertTrue(gate.reply_finished(now=12))
        self.assertTrue(gate.reply_finished(now=15))
        self.assertFalse(gate.expired(now=19))
        self.assertTrue(gate.expired(now=20))

    def test_follow_up_waits_until_runtime_work_finishes(self):
        gate = ConversationGate(follow_up_seconds=5, session_max_seconds=30)
        gate.wake(now=10)
        gate.hold("runtime")
        self.assertFalse(gate.reply_finished(now=12))
        self.assertFalse(gate.expired(now=20))
        self.assertEqual(gate.release("runtime", now=21), "follow_up")
        self.assertFalse(gate.expired(now=25))
        self.assertTrue(gate.expired(now=26))

    def test_empty_transcript_rearms_the_command_window(self):
        gate = ConversationGate(follow_up_seconds=5, session_max_seconds=30)
        gate.wake(now=10)
        gate.hold("asr")
        self.assertFalse(gate.expired(now=16))
        self.assertEqual(gate.release("asr", now=18, fresh="command"), "command")
        self.assertFalse(gate.expired(now=22))
        self.assertTrue(gate.expired(now=23))

    def test_sleep_releases_local_capture_without_reopening(self):
        device = FakeCapture()
        arbiter = CaptureArbiter(device)
        arbiter.refresh_local()
        self.assertEqual(arbiter.sleep(), CaptureState.SLEEP)
        self.assertFalse(device.opened)
        self.assertEqual(device.opens, 1)


if __name__ == "__main__":
    unittest.main()
