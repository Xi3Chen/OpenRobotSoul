from __future__ import annotations

import threading
import time
import unittest

from robotsoul.voice.core import CaptureState
from robotsoul.voice.engine import AsrResult, SynthesizedAudio
from robotsoul.voice.service import VoiceService


class FakeCapture:
    def __init__(self, available=False):
        self.available = available
        self.opened = False
        self.callback = None
        self.probes = 0

    def probe(self):
        self.probes += 1
        return self.available

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    def set_callback(self, callback):
        self.callback = callback


class FakeEngine:
    def __init__(self):
        self.loads = 0
        self.resets = 0
        self.closed = 0
        self.asr_called = threading.Event()
        self.tts_called = threading.Event()
        self.synthesized = []

    def load(self):
        self.loads += 1

    def reset_asr(self):
        self.resets += 1

    def accept_wake(self, _samples, _sample_rate):
        return None

    def accept_vad(self, _samples):
        return False

    def accept_asr(self, _samples, _sample_rate):
        self.asr_called.set()
        return AsrResult("向前", True)

    def drain_speech_segments(self):
        return []

    def synthesize(self, _text):
        self.synthesized.append(_text)
        self.tts_called.set()
        return SynthesizedAudio((0.1, -0.1), 16000)

    def close(self):
        self.closed += 1


class VoiceServiceTests(unittest.TestCase):
    def test_no_microphone_sleeps_without_loading_models_until_call(self):
        capture = FakeCapture(available=False)
        engine = FakeEngine()
        runtime_called = threading.Event()
        outputs = []
        service = VoiceService(
            capture=capture,
            engine=engine,
            run_text=lambda text: runtime_called.set() or text,
            idle_probe_seconds=100,
        )
        try:
            self.assertEqual(service.start(), CaptureState.SLEEP)
            self.assertEqual(engine.loads, 0)
            generation = service.begin_call()
            self.assertEqual(service.state, CaptureState.CALL_PENDING)
            generation = service.call_connected()
            self.assertEqual(engine.loads, 1)
            self.assertFalse(capture.opened)
            service.set_call_audio_sink(lambda samples, rate: outputs.append((samples, rate)))
            self.assertTrue(service.accept_call_audio(generation, (0.1, 0.2), 16000))
            self.assertTrue(engine.asr_called.wait(2))
            self.assertTrue(runtime_called.wait(2))
            self.assertTrue(engine.tts_called.wait(2))
            deadline = time.monotonic() + 2
            while not outputs and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(outputs, [((0.1, -0.1), 16000)])
            self.assertEqual(service.end_call(), CaptureState.SLEEP)
        finally:
            service.stop()
        self.assertFalse(capture.opened)
        self.assertGreaterEqual(engine.closed, 1)

    def test_old_generation_audio_is_rejected_after_hangup(self):
        capture = FakeCapture(available=False)
        engine = FakeEngine()
        service = VoiceService(capture=capture, engine=engine, run_text=lambda text: text, idle_probe_seconds=100)
        try:
            service.start()
            generation = service.begin_call()
            generation = service.call_connected()
            service.end_call()
            self.assertFalse(service.accept_call_audio(generation, (0.1,), 16000))
        finally:
            service.stop()

    def test_unanswered_call_releases_lease_after_pending_grace(self):
        capture = FakeCapture(available=False)
        service = VoiceService(
            capture=capture,
            engine=FakeEngine(),
            run_text=lambda text: text,
            idle_probe_seconds=100,
            call_reconnect_grace_seconds=0.05,
        )
        try:
            service.start()
            service.begin_call()
            deadline = time.monotonic() + 2
            while service.state != CaptureState.SLEEP and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(service.state, CaptureState.SLEEP)
            self.assertFalse(capture.opened)
        finally:
            service.stop()

    def test_unavailable_device_probe_uses_bounded_backoff(self):
        capture = FakeCapture(available=False)
        service = VoiceService(capture=capture, engine=FakeEngine(), run_text=lambda text: text)
        try:
            self.assertEqual(service.start(), CaptureState.SLEEP)
            probes_after_start = capture.probes
            self.assertEqual(service.refresh_device(), CaptureState.SLEEP)
            self.assertEqual(capture.probes, probes_after_start)
        finally:
            service.stop()

    def test_spoken_reply_stays_chinese_for_english_runtime_messages(self):
        class Task:
            error = "No supported skill matched the request"

        class Result:
            task = Task()
            evaluation = None

        capture = FakeCapture(available=False)
        engine = FakeEngine()
        service = VoiceService(capture=capture, engine=engine, run_text=lambda text: Result(),
                               idle_probe_seconds=100)
        try:
            service.start()
            service.begin_call()
            generation = service.call_connected()
            service.set_call_audio_sink(lambda samples, rate: None)
            service.accept_call_audio(generation, (0.1, 0.2), 16000)
            self.assertTrue(engine.tts_called.wait(2))
            self.assertEqual(engine.synthesized[-1], "抱歉，这个操作没有成功。")
        finally:
            service.stop()



if __name__ == "__main__":
    unittest.main()
