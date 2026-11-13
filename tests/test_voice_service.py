from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from robotsoul.runlog import configure, current
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
        text = getattr(self, "wake_text", None)
        if text and getattr(self, "wake_once", False):
            if getattr(self, "_woke", False):
                return None
            self._woke = True
        return text

    def accept_vad(self, _samples):
        return False

    def accept_asr(self, _samples, _sample_rate):
        self.asr_called.set()
        pending = getattr(self, "asr_results", None)
        if pending:
            return pending.pop(0)
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
    def setUp(self):
        self._logs = tempfile.TemporaryDirectory()
        self._previous_log = current().directory
        configure(self._logs.name)

    def tearDown(self):
        configure(self._previous_log)
        self._logs.cleanup()

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

    def test_wake_and_transcript_are_recorded(self):
        capture = FakeCapture(available=True)
        engine = FakeEngine()
        engine.wake_text = "索拉"
        service = VoiceService(
            capture=capture,
            engine=engine,
            run_text=lambda text: text,
            idle_probe_seconds=100,
        )
        try:
            self.assertEqual(service.start(), CaptureState.PASSIVE)
            capture.callback((0.2, 0.1), 16000)
            deadline = time.monotonic() + 2
            lines = []
            while time.monotonic() < deadline:
                lines = current().recent(20)
                if any("唤醒：索拉" in line for line in lines) and any("识别：向前" in line for line in lines):
                    break
                time.sleep(0.01)
            self.assertTrue(any("-Voice-Info] 唤醒：索拉" in line for line in lines))
            self.assertTrue(any("-Voice-Info] 识别：向前（本地麦克风）" in line for line in lines))
            self.assertTrue(any("-Voice-Info] 提交运行时：向前" in line for line in lines))
            names = os.listdir(self._logs.name)
            self.assertTrue(names)
            self.assertTrue(all(name.endswith(".log") for name in names))
            self.assertFalse((Path(self._logs.name) / "voice_events.jsonl").exists())
            self.assertFalse(any(name.endswith(".jsonl.1") for name in names))
        finally:
            service.stop()

    def test_partial_recognition_is_recorded_before_endpoint(self):
        capture = FakeCapture(available=True)
        engine = FakeEngine()
        engine.wake_text = "索拉"
        engine.asr_results = [
            AsrResult("向", False),
            AsrResult("向前走", False),
            AsrResult("向前走", False),
        ]
        submitted = []
        service = VoiceService(
            capture=capture,
            engine=engine,
            run_text=lambda text: submitted.append(text) or text,
            idle_probe_seconds=100,
        )
        try:
            self.assertEqual(service.start(), CaptureState.PASSIVE)
            capture.callback((0.2, 0.1), 16000)
            capture.callback((0.2, 0.1), 16000)
            capture.callback((0.2, 0.1), 16000)
            deadline = time.monotonic() + 2
            lines = []
            while time.monotonic() < deadline:
                lines = current().recent(20)
                if any("识别中：向前走（本地麦克风）" in line for line in lines):
                    break
                time.sleep(0.01)
            partials = [line for line in lines if "识别中：" in line]
            self.assertEqual(len(partials), 2)
            self.assertTrue(any("识别中：向（本地麦克风）" in line for line in partials))
            self.assertTrue(any("识别中：向前走（本地麦克风）" in line for line in partials))
            self.assertFalse(any("] 识别：" in line for line in lines))
            self.assertEqual(submitted, [])
        finally:
            service.stop()

    def test_empty_endpoint_keeps_listening_for_the_command(self):
        capture = FakeCapture(available=True)
        engine = FakeEngine()
        engine.wake_text = "索拉"
        engine.wake_once = True
        engine.asr_results = [
            AsrResult("", True),
            AsrResult("", True),
            AsrResult("请站起来", True),
        ]
        submitted = []
        service = VoiceService(
            capture=capture,
            engine=engine,
            run_text=lambda text: submitted.append(text) or text,
            idle_probe_seconds=100,
        )
        try:
            self.assertEqual(service.start(), CaptureState.PASSIVE)
            capture.callback((0.2, 0.1), 16000)
            capture.callback((0.2, 0.1), 16000)
            capture.callback((0.2, 0.1), 16000)
            deadline = time.monotonic() + 2
            lines = []
            while time.monotonic() < deadline:
                lines = current().recent(30)
                if any("识别：请站起来（本地麦克风）" in line for line in lines):
                    break
                time.sleep(0.01)
            self.assertTrue(any("识别：请站起来（本地麦克风）" in line for line in lines))
            self.assertFalse(any("没有识别到指令" in line for line in lines))
            self.assertEqual(submitted, ["请站起来"])
        finally:
            service.stop()

    def test_runtime_result_is_recorded(self):
        capture = FakeCapture(available=True)
        engine = FakeEngine()
        engine.wake_text = "索拉"
        engine.wake_once = True

        class Result:
            def __init__(self):
                self.task = type("Task", (), {"task_id": "task-1", "status": "failed", "error": "没有对应技能"})()
                self.evaluation = None

        service = VoiceService(
            capture=capture,
            engine=engine,
            run_text=lambda text: Result(),
            idle_probe_seconds=100,
        )
        try:
            self.assertEqual(service.start(), CaptureState.PASSIVE)
            capture.callback((0.2, 0.1), 16000)
            deadline = time.monotonic() + 2
            lines = []
            while time.monotonic() < deadline:
                lines = current().recent(30)
                if (
                    any("运行时失败 id=task-1" in line for line in lines)
                    and any("回复结束，等待后续指令" in line for line in lines)
                ):
                    break
                time.sleep(0.01)
            self.assertTrue(any("提交运行时：向前" in line for line in lines))
            self.assertTrue(any("运行时失败 id=task-1 状态=failed 原因=没有对应技能" in line for line in lines))
            self.assertTrue(any("回复结束，等待后续指令" in line for line in lines))
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
