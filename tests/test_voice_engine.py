from __future__ import annotations

import builtins
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from robotsoul.runlog import configure, current
from robotsoul.voice.audio import SoundDeviceCapture, SoundDeviceOutput, VoiceAudioError
from robotsoul.voice.engine import SherpaVoiceEngine, VoiceEngineError


class VoiceAdapterTests(unittest.TestCase):
    def setUp(self):
        self._logs = tempfile.TemporaryDirectory()
        self._previous_log = current().directory
        configure(self._logs.name)

    def tearDown(self):
        configure(self._previous_log)
        self._logs.cleanup()

    def test_sherpa_models_use_cpu_and_streaming_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            specs = {
                "kws-zh-en-3m": ("tokens.txt", "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
                                    "decoder-epoch-13-avg-2-chunk-8-left-64.onnx",
                                    "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx", "en.phone"),
                "asr-zh-streaming-int8": ("tokens.txt", "encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx"),
                "vad-silero-int8": ("silero_vad.int8.onnx",),
                "tts-zh-xiao-ya-int8": ("tokens.txt", "voice.onnx", "lexicon.txt", "phone.fst", "date.fst", "number.fst"),
            }
            for directory, files in specs.items():
                target = root / directory / "archive-root"
                target.mkdir(parents=True)
                for filename in files:
                    (target / filename).touch()

            calls = {}

            class Stream:
                def accept_waveform(self, rate, samples): calls["waveform"] = (rate, tuple(samples))
            class Kws:
                def __init__(self, **kw): calls["kws_args"] = kw
                def create_stream(self, keyword=None): calls["keyword"] = keyword; return Stream()
                def is_ready(self, stream): return calls.pop("kws_ready", False)
                def decode_stream(self, stream): calls["kws_decoded"] = True
                def get_result(self, stream): return "索拉"
                def reset_stream(self, stream): calls["kws_reset"] = True
            class Recognizer:
                @classmethod
                def from_transducer(cls, **kw): calls["asr_args"] = kw; return cls()
                def create_stream(self): return Stream()
                def is_ready(self, stream): return calls.pop("asr_ready", False)
                def decode_stream(self, stream): calls["asr_decoded"] = True
                def get_result(self, stream): return "向前"
                def is_endpoint(self, stream): return True
                def reset(self, stream): calls["asr_reset"] = True
            class VadConfig:
                def __init__(self):
                    self.silero_vad = types.SimpleNamespace()
                def validate(self): return True
            class Vad:
                def __init__(self, config, **kw): calls["vad_config"] = config
                def accept_waveform(self, samples): calls["vad_samples"] = tuple(samples)
                def empty(self): return True
                def is_speech_detected(self): return False
                def front(self): return None
                def pop(self): pass
            class TtsConfig:
                def __init__(self, **kw): pass
                def validate(self): return True
            class Tts:
                def __init__(self, config): pass
                def generate(self, text, generation): return types.SimpleNamespace(samples=[0.1, -0.1], sample_rate=22050)
            def text2token(*args, **kwargs):
                calls["text2token"] = (args, kwargs)
                return [["suǒ", "lā"]]
            fake = types.SimpleNamespace(
                text2token=text2token,
                KeywordSpotter=Kws, OnlineRecognizer=Recognizer, VadModelConfig=VadConfig,
                VoiceActivityDetector=Vad, OfflineTtsConfig=TtsConfig,
                OfflineTtsModelConfig=lambda **kw: kw, OfflineTtsVitsModelConfig=lambda **kw: kw,
                OfflineTts=Tts, GenerationConfig=lambda: types.SimpleNamespace(),
            )
            fake_np = types.SimpleNamespace(float32="float32", asarray=lambda values, dtype: list(values))
            with patch.dict(sys.modules, {"sherpa_onnx": fake, "numpy": fake_np}):
                engine = SherpaVoiceEngine(tmp, "索拉")
                engine.load()
                self.assertEqual(calls["kws_args"]["num_threads"], 1)
                self.assertEqual(calls["kws_args"]["provider"], "cpu")
                self.assertIn("chunk-8-left-64.int8.onnx", calls["kws_args"]["encoder"])
                self.assertEqual(calls["text2token"][1]["tokens_type"], "phone+ppinyin")
                self.assertEqual(calls["text2token"][0], (["索拉"],))
                self.assertIsNone(calls["keyword"])
                self.assertEqual(engine.accept_wake([0.1], 16000), "索拉")
                self.assertTrue(calls["kws_reset"])
                calls["asr_ready"] = True
                result = engine.accept_asr([0.2], 16000)
                self.assertEqual(calls["asr_args"]["rule3_min_utterance_length"], 20.0)
                self.assertEqual((result.text, result.is_endpoint), ("向前", True))
                self.assertTrue(calls["asr_decoded"])
                self.assertTrue(calls["asr_reset"])
                self.assertEqual(calls["vad_config"].num_threads, 1)
                self.assertEqual(calls["vad_config"].provider, "cpu")
                engine.accept_vad([0.3])
                self.assertEqual(engine.drain_speech_segments(), [])
                self.assertEqual(engine.synthesize("你好").sample_rate, 22050)
                keyword_text = Path(calls["kws_args"]["keywords_file"]).read_text(encoding="utf-8")
                self.assertIn("suǒ lā :3.0 #0.1 @索拉", keyword_text)
                lines = current().recent(20)
                self.assertTrue(any("语音模型加载完成" in line for line in lines))
                self.assertTrue(any("唤醒模型已加载" in line and "门槛=0.1" in line and "加权=3.0" in line for line in lines))
                self.assertTrue(any("识别模型已加载" in line for line in lines))
                self.assertTrue(any("端点模型已加载" in line for line in lines))
                self.assertTrue(any("合成模型已加载" in line for line in lines))
                engine.close()

    def test_missing_sherpa_is_reported_lazily(self):
        engine = SherpaVoiceEngine(tempfile.gettempdir(), "索拉")
        original = builtins.__import__

        def no_sherpa(name, *args, **kwargs):
            if name == "sherpa_onnx":
                raise ImportError("not installed")
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=no_sherpa):
            with self.assertRaisesRegex(VoiceEngineError, "sherpa-onnx is not installed"):
                engine.load()

    def test_missing_model_file_names_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "models" / "kws-zh-en-3m").mkdir(parents=True)
            with patch.dict(sys.modules, {"sherpa_onnx": types.ModuleType("sherpa_onnx")}):
                with self.assertRaisesRegex(VoiceEngineError, "kws-zh-en-3m model file tokens.txt"):
                    SherpaVoiceEngine(tmp, "索拉").load()

    def test_vad_front_property_is_drained_without_calling_the_segment(self):
        class Segment:
            def __call__(self):
                raise AssertionError("SpeechSegment must not be called")

        class PropertyVad:
            def __init__(self):
                self.items = [Segment()]

            @property
            def front(self):
                return self.items[0]

            def empty(self):
                return not self.items

            def pop(self):
                self.items.pop(0)

        engine = SherpaVoiceEngine(tempfile.gettempdir(), "索拉")
        engine._sherpa = object()
        engine._vad = PropertyVad()
        segments = engine.drain_speech_segments()
        self.assertEqual(len(segments), 1)
        self.assertIsInstance(segments[0], Segment)

        class MethodVad:
            def __init__(self):
                self.items = ["method-segment"]

            def front(self):
                return self.items[0]

            def empty(self):
                return not self.items

            def pop(self):
                self.items.pop(0)

        engine._vad = MethodVad()
        self.assertEqual(engine.drain_speech_segments(), ["method-segment"])

    def test_audio_devices_are_lazy_and_capture_copies_mono_data(self):
        events = []
        class FakeStream:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def start(self): events.append("start")
            def stop(self): events.append("stop")
            def close(self): events.append("close")
        fake = types.SimpleNamespace(
            query_devices=lambda device, kind: {"max_input_channels": 1},
            InputStream=FakeStream, play=lambda *a, **k: events.append(("play", k)),
            stop=lambda: events.append("output-stop"),
        )
        received = []
        capture = SoundDeviceCapture(lambda samples, rate: received.append((samples, rate)))
        output = SoundDeviceOutput()
        with patch.dict(sys.modules, {"sounddevice": fake}):
            self.assertIsNone(capture._stream)
            self.assertTrue(capture.probe())
            capture.open()
            matrix = [[0.1, 0.9], [0.2, 0.8]]
            class Matrix(list):
                ndim = 2
                def copy(self): return Matrix(self)
                def __getitem__(self, key):
                    if isinstance(key, tuple):
                        rows, col = key
                        return [row[col] for row in list.__getitem__(self, rows)]
                    return list.__getitem__(self, key)
            capture._stream.kwargs["callback"](Matrix(matrix), 2, None, None)
            self.assertEqual(received, [((0.1, 0.2), 16000)])
            capture.close(); capture.close()
            output.play((0.1,), 16000); output.stop()
        self.assertEqual(events[:1], ["start"])
        self.assertIn("close", events)
        self.assertIn(("play", {"samplerate": 16000, "device": None, "blocking": True}), events)

    def test_missing_sounddevice_has_actionable_error(self):
        original = builtins.__import__
        def no_audio(name, *args, **kwargs):
            if name == "sounddevice":
                raise ImportError("not installed")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=no_audio):
            with self.assertRaisesRegex(VoiceAudioError, "sounddevice is required"):
                SoundDeviceOutput().play((0.2,), 16000)

    def test_empty_tts_text_rejected_without_loading_models(self):
        with self.assertRaisesRegex(ValueError, "text must not be empty"):
            SherpaVoiceEngine(tempfile.gettempdir(), "索拉").synthesize(" 	")


if __name__ == "__main__":
    unittest.main()
