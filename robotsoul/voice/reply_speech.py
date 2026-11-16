"""Low-latency reply speech: short TTS units, warm phrase cache and chunked playback.

The speech API synthesizes a whole sentence per request, so a long reply pays the
full network + synthesis latency before the first audio sample can be played.
Two cheap tricks remove most of that latency:

* split a reply into short units (first unit <= 6 chars) and synthesize the
  next unit while the current one is playing; the producer runs in its own
  thread so playback never waits for the whole reply;
* pre-synthesize the fixed acknowledgements the voice service repeats all the
  time, so the first audio chunk is a cache hit.

When the configured speaker exposes ``start``/``write``/``finish`` (the local
PulseAudio adapter does), the PCM of every unit is written chunk by chunk into a
single persistent ``pacat`` process instead of restarting the player per unit.
Speakers without a streaming path fall back to one ``play`` call per unit; the
TTS pipeline still overlaps synthesis with playback.

The pipeline is abortable: ``stop`` cancels the producer and stops the speaker,
which is what barge-in and a replaced utterance need.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from queue import Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Callable, Sequence

DEFAULT_CACHE_PHRASES = ("好的，", "已完成。", "抱歉，", "这个操作没有成功。")
DEFAULT_MAX_FIRST_CHARS = 6
DEFAULT_MAX_UNIT_CHARS = 12
_SENTENCE_ENDINGS = "。！？!?；;…"
_CLAUSE_ENDINGS = "，,、：:"
# Playback errors (a killed pacat process, a closed speaker) are normal on
# barge-in; they abort the reply instead of failing the voice service.
_PLAYBACK_ERRORS = (OSError, RuntimeError)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def cache_phrases_from_env(default: Sequence[str] = DEFAULT_CACHE_PHRASES) -> list[str]:
    """Read ROBOTSOUL_VOICE_TTS_CACHE_PHRASES (semicolon separated); empty disables the cache."""
    raw = os.environ.get("ROBOTSOUL_VOICE_TTS_CACHE_PHRASES")
    if raw is None:
        return list(default)
    if not raw.strip():
        return []
    return [item.strip() for item in raw.split(";") if item.strip()]


def split_reply_units(text: str, *, max_first: int = DEFAULT_MAX_FIRST_CHARS,
                      max_unit: int = DEFAULT_MAX_UNIT_CHARS) -> list[str]:
    """Split a reply into TTS units, keeping the first unit short for low TTFA."""
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return []
    clauses: list[str] = []
    current = ""
    for char in cleaned:
        current += char
        if char in _SENTENCE_ENDINGS or char in _CLAUSE_ENDINGS:
            clauses.append(current)
            current = ""
    if current:
        clauses.append(current)
    units: list[str] = []
    first = True
    for clause in clauses:
        limit = max_first if first else max_unit
        if len(clause) <= limit:
            units.append(clause)
            first = False
            continue
        start = 0
        while start < len(clause):
            units.append(clause[start:start + limit])
            start += limit
            first = False
    return units


@dataclass
class SpeechTiming:
    text: str
    units: int = 0
    cache_hits: int = 0
    first_audio_ms: float | None = None
    total_ms: float = 0.0
    aborted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "units": self.units,
            "cache_hits": self.cache_hits,
            "first_audio_ms": None if self.first_audio_ms is None else round(self.first_audio_ms, 1),
            "total_ms": round(self.total_ms, 1),
            "aborted": self.aborted,
        }


class ReplySpeechPipeline:
    """Synthesize a reply unit by unit while the player consumes chunks."""

    def __init__(self, engine: Any, speaker: Any, *, engine_lock: Lock | None = None,
                 logger: Callable[[str, str], None] | None = None,
                 max_first: int | None = None, max_unit: int | None = None) -> None:
        self.engine = engine
        self.speaker = speaker
        self.sample_rate = int(getattr(engine, "sample_rate", 16000))
        self._engine_lock = engine_lock
        self._log = logger or (lambda level, message: None)
        self.max_first = max_first if max_first is not None else _env_int(
            "ROBOTSOUL_VOICE_TTS_MAX_FIRST_CHARS", DEFAULT_MAX_FIRST_CHARS)
        self.max_unit = max_unit if max_unit is not None else _env_int(
            "ROBOTSOUL_VOICE_TTS_MAX_UNIT_CHARS", DEFAULT_MAX_UNIT_CHARS)
        self._cache: dict[str, Any] = {}
        self._cache_lock = Lock()
        self._stop = Event()
        self._speak_lock = Lock()

    @property
    def cache_size(self) -> int:
        with self._cache_lock:
            return len(self._cache)

    def prepare_cache(self, phrases: Sequence[str] | None = None) -> int:
        """Synthesize the fixed acknowledgement phrases once (call from a background thread)."""
        targets = list(phrases) if phrases is not None else cache_phrases_from_env()
        built = 0
        for phrase in targets:
            with self._cache_lock:
                if phrase in self._cache:
                    continue
            try:
                audio = self._synthesize(phrase)
            except Exception as exc:  # noqa: BLE001 - cache is an optimisation only
                self._log("Warning", f"回复语音缓存预热失败：{type(exc).__name__}: {exc}")
                return built
            with self._cache_lock:
                self._cache[phrase] = audio
            built += 1
        if built:
            self._log("Info", f"回复语音缓存已预热 数量={built} 总缓存={self.cache_size}")
        return built

    def _synthesize(self, text: str) -> Any:
        if self._engine_lock is not None:
            with self._engine_lock:
                return self.engine.synthesize(text)
        return self.engine.synthesize(text)

    def _audio_for(self, unit: str) -> tuple[Any, bool]:
        with self._cache_lock:
            cached = self._cache.get(unit)
        if cached is not None:
            return cached, True
        return self._synthesize(unit), False

    def _streaming_speaker(self) -> bool:
        supports = getattr(self.speaker, "supports_streaming", None)
        if supports is not None:
            return bool(supports)
        return hasattr(self.speaker, "start") and hasattr(self.speaker, "write")

    def speak(self, text: str, *, on_first_audio: Callable[[], None] | None = None,
              on_chunk: Callable[[Any, int], None] | None = None) -> SpeechTiming:
        """Speak ``text``; returns timing details (first audio, cache hits, abort)."""
        timing = SpeechTiming(text=str(text))
        units = split_reply_units(text, max_first=self.max_first, max_unit=self.max_unit)
        timing.units = len(units)
        if not units:
            return timing
        with self._speak_lock:
            self._stop.clear()
            started = monotonic()
            audio_queue: Queue = Queue()
            cache_hits = 0

            def produce() -> None:
                nonlocal cache_hits
                try:
                    for unit in units:
                        if self._stop.is_set():
                            return
                        audio, hit = self._audio_for(unit)
                        if hit:
                            cache_hits += 1
                        if self._stop.is_set():
                            return
                        if audio is None:
                            continue
                        audio_queue.put(audio)
                except Exception as exc:  # noqa: BLE001 - surfaced through the queue
                    audio_queue.put(exc)
                finally:
                    audio_queue.put(None)

            producer = Thread(target=produce, daemon=True, name="tts-reply")
            producer.start()
            chunked = self._streaming_speaker()
            started_player = False
            try:
                item = audio_queue.get()
                if item is None:
                    return timing
                if isinstance(item, Exception):
                    raise item
                audio = item
                sample_rate = int(getattr(audio, "sample_rate", self.sample_rate))
                if chunked:
                    self.speaker.start(sample_rate)
                    started_player = True
                played = 0
                while audio is not None:
                    if self._stop.is_set():
                        timing.aborted = True
                        break
                    if isinstance(audio, Exception):
                        raise audio
                    samples = getattr(audio, "samples", audio)
                    chunk_rate = int(getattr(audio, "sample_rate", sample_rate))
                    if on_chunk is not None:
                        on_chunk(samples, chunk_rate)
                    try:
                        if chunked:
                            self.speaker.write(samples)
                        else:
                            replaced = self.speaker.play(samples, chunk_rate)
                            if replaced is True:
                                timing.aborted = True
                                break
                    except _PLAYBACK_ERRORS:
                        # The backend was stopped (barge-in) or the sink died:
                        # the reply is aborted, not a service failure.
                        timing.aborted = True
                        break
                    played += 1
                    if played == 1:
                        timing.first_audio_ms = (monotonic() - started) * 1000.0
                        if on_first_audio is not None:
                            on_first_audio()
                    audio = audio_queue.get()
                timing.cache_hits = cache_hits
            finally:
                if not timing.aborted:
                    timing.aborted = self._stop.is_set()
                self._stop.set()
                if chunked and started_player:
                    try:
                        self.speaker.finish()
                    except _PLAYBACK_ERRORS:
                        pass
                producer.join(timeout=1.0)
                timing.total_ms = (monotonic() - started) * 1000.0
                self._log(
                    "Info",
                    f"回复语音完成 单元={timing.units} 缓存命中={timing.cache_hits} "
                    f"首音={timing.first_audio_ms and round(timing.first_audio_ms, 1)}ms "
                    f"总耗时={round(timing.total_ms, 1)}ms",
                )
        return timing

    def stop(self) -> None:
        """Abort playback (used by barge-in); safe to call from another thread."""
        self._stop.set()
        stop = getattr(self.speaker, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001 - stopping a dead backend is fine
                pass
