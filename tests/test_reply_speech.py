from __future__ import annotations

import threading
import time
import unittest

from robotsoul.voice.engine import SynthesizedAudio
from robotsoul.voice.reply_speech import (
    ReplySpeechPipeline,
    cache_phrases_from_env,
    split_reply_units,
)


class FakeEngine:
    sample_rate = 16000

    def __init__(self, delay=0.0):
        self.calls: list[str] = []
        self.delay = delay

    def synthesize(self, text):
        self.calls.append(text)
        if self.delay:
            time.sleep(self.delay)
        return SynthesizedAudio((0.1, -0.1), 16000)


class StreamingSpeaker:
    supports_streaming = True

    def __init__(self):
        self.started: list[int] = []
        self.writes: list[tuple[float, ...]] = []
        self.finished = 0
        self.stopped = 0

    def start(self, sample_rate):
        self.started.append(sample_rate)

    def write(self, samples):
        self.writes.append(tuple(samples))

    def finish(self, timeout=5.0):
        self.finished += 1

    def stop(self):
        self.stopped += 1


class BlockingSpeaker:
    def __init__(self):
        self.played = 0

    def play(self, _samples, _rate):
        self.played += 1
        return False

    def stop(self):
        return


class SplitUnitsTests(unittest.TestCase):
    def test_short_first_unit_then_sentence(self):
        self.assertEqual(
            split_reply_units("好的，我正在打开手电筒。"),
            ["好的，", "我正在打开手电筒。"],
        )

    def test_long_first_clause_uses_the_smaller_first_limit(self):
        units = split_reply_units("abcdefghijklmnopqrstuvwxyz", max_first=6, max_unit=12)
        self.assertEqual(units, ["abcdef", "ghijkl", "mnopqr", "stuvwx", "yz"])
        self.assertTrue(all(len(unit) <= 6 for unit in units))

    def test_empty_text_has_no_units(self):
        self.assertEqual(split_reply_units("   "), [])

    def test_cache_phrases_env(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"ROBOTSOUL_VOICE_TTS_CACHE_PHRASES": "好的;收到"}, clear=False):
            self.assertEqual(cache_phrases_from_env(), ["好的", "收到"])
        with patch.dict(os.environ, {"ROBOTSOUL_VOICE_TTS_CACHE_PHRASES": ""}, clear=False):
            self.assertEqual(cache_phrases_from_env(), [])


class ReplyPipelineTests(unittest.TestCase):
    def test_streaming_speaker_gets_one_start_and_each_unit(self):
        engine = FakeEngine()
        speaker = StreamingSpeaker()
        pipeline = ReplySpeechPipeline(engine, speaker)
        timing = pipeline.speak("好的，我正在打开手电筒。")
        self.assertEqual(engine.calls, ["好的，", "我正在打开手电筒。"])
        self.assertEqual(speaker.started, [16000])
        self.assertEqual(len(speaker.writes), 2)
        self.assertEqual(speaker.finished, 1)
        self.assertEqual(timing.units, 2)
        self.assertIsNotNone(timing.first_audio_ms)
        self.assertFalse(timing.aborted)

    def test_non_streaming_speaker_falls_back_to_play(self):
        engine = FakeEngine()
        speaker = BlockingSpeaker()
        pipeline = ReplySpeechPipeline(engine, speaker)
        pipeline.speak("好的，我正在打开手电筒。")
        self.assertEqual(speaker.played, 2)

    def test_cache_avoids_a_second_synthesis(self):
        engine = FakeEngine()
        speaker = StreamingSpeaker()
        pipeline = ReplySpeechPipeline(engine, speaker)
        self.assertEqual(pipeline.prepare_cache(["好的，"]), 1)
        self.assertEqual(pipeline.cache_size, 1)
        timing = pipeline.speak("好的，")
        self.assertEqual(engine.calls, ["好的，"])
        self.assertEqual(timing.cache_hits, 1)
        self.assertEqual(len(speaker.writes), 1)

    def test_stop_aborts_a_running_reply(self):
        engine = FakeEngine(delay=0.2)
        speaker = StreamingSpeaker()
        pipeline = ReplySpeechPipeline(engine, speaker)
        result = {}

        def speak():
            result["timing"] = pipeline.speak("好的，我正在打开手电筒。")

        thread = threading.Thread(target=speak)
        thread.start()
        time.sleep(0.05)
        pipeline.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result["timing"].aborted)
        self.assertGreaterEqual(speaker.stopped, 1)


if __name__ == "__main__":
    unittest.main()
