from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import os
from queue import Empty, Full, Queue
import re
from threading import Event, Lock, RLock, Thread, Timer
from time import monotonic
from typing import Any, Callable

from robotsoul.runlog import log as runtime_log
from robotsoul.voice.core import (
    AudioFrame,
    BoundedPcmBuffer,
    CaptureArbiter,
    CaptureState,
    ConversationGate,
    RuntimeTextExecutor,
)
from robotsoul.voice.engine import VoiceEngineError
from robotsoul.voice.monitor import AudioLevelMonitor
from robotsoul.voice.reply_speech import ReplySpeechPipeline
from robotsoul.voice.speech_api import SpeechApiError


def _recognition_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _toneless_pinyin(text: str) -> list[str] | None:
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return None
    return [item.lower() for item in lazy_pinyin(text)]


def _is_wake_only(text: str, wake_phrase: str) -> bool:
    """True when the transcript is the wake word itself (including homophones)."""
    cleaned = "".join(text.split())
    wake = "".join(wake_phrase.split())
    if not cleaned or not wake:
        return False
    if cleaned == wake:
        return True
    wake_syllables = _toneless_pinyin(wake)
    text_syllables = _toneless_pinyin(cleaned)
    if wake_syllables is not None and text_syllables is not None:
        return text_syllables == wake_syllables
    # Minimal environment without pypinyin: accept a same-length, same-final
    # transcription (e.g. 索拉 -> 锁拉) as the wake word.
    return len(cleaned) == len(wake) and cleaned[-1] == wake[-1]


def _shorten_spoken_reply(text: str, success: bool, limit: int) -> str:
    """Keep spoken replies short: long replies add seconds of TTS latency."""
    if len(text) <= limit:
        return text
    return "好的，已完成。" if success else "抱歉，这个操作没有成功。"


def _command_matches(expected: str, text: str) -> bool:
    """True when two command texts refer to the same utterance (rapid ASR/final)."""
    left = "".join(expected.split())
    right = "".join(text.split())
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return shorter in longer and len(longer) - len(shorter) <= 3


# A transcript that ends with one of these is very likely only the first half of
# one spoken command ("把…", "然后…"): hold the dispatch briefly so a second VAD
# segment can be merged instead of running two unrelated runtimes.
_CONTINUATION_ENDINGS = (
    "然后", "并且", "而且", "接着", "再", "把", "给", "帮", "请", "跟", "向",
    "往", "朝", "对", "将", "让", "为", "和", "并", "去", "到",
)
# The same words at the start of the next segment also signal a continuation
# ("……然后，向左转弯").
_CONTINUATION_STARTERS = (
    "然后", "并且", "而且", "接着", "再", "就", "再往", "往", "向", "朝", "把",
)


@dataclass(frozen=True)
class _OwnedSegment:
    """PCM copied off the VAD before ``reset`` reuses its buffers."""

    samples: tuple[float, ...]


def _owned_segments(segments: Any) -> list[Any]:
    owned: list[Any] = []
    for segment in segments or ():
        samples = getattr(segment, "samples", None)
        if samples is None:
            owned.append(segment)
            continue
        owned.append(_OwnedSegment(tuple(float(value) for value in samples)))
    return owned


def _owned_segment_samples(segments: Any) -> list[float]:
    samples: list[float] = []
    for segment in segments or ():
        chunk = getattr(segment, "samples", None)
        if chunk is None:
            continue
        samples.extend(float(value) for value in chunk)
    return samples


@dataclass
class _HeldTranscript:
    """A final that looks like half of one spoken command, waiting for its tail."""

    text: str
    cleaned: str
    generation: int
    deadline: float
    merges: int = 0


@dataclass(frozen=True)
class VoiceHealth:
    state: CaptureState
    available: bool
    error: str | None
    dropped_frames: int
    last_frame_at: float | None
    last_wake_at: float | None
    last_transcript_at: float | None


class VoiceService:
    """Coordinates local passive listening, bounded ASR work and TTS routing."""

    def __init__(
        self,
        *,
        capture: Any,
        engine: Any,
        run_text: Callable[[str], Any],
        robot_name: str = "机器人",
        speaker: Any | None = None,
        sample_rate: int = 16000,
        follow_up_seconds: float = 8,
        session_max_seconds: float = 120,
        frame_queue_size: int = 20,
        pre_roll_frames: int = 20,
        idle_probe_seconds: float = 2,
        call_reconnect_grace_seconds: float = 15,
        wake_callback: Callable[[str], None] | None = None,
        transcript_callback: Callable[[str], None] | None = None,
        command_matcher: Callable[[str], Any] | None = None,
        cancel_task: Callable[[str], Any] | None = None,
    ) -> None:
        self.capture = capture
        self.engine = engine
        self.robot_name = robot_name
        self.speaker = speaker
        self.sample_rate = sample_rate
        self.arbiter = CaptureArbiter(capture)
        self.gate = ConversationGate(
            follow_up_seconds=follow_up_seconds,
            session_max_seconds=session_max_seconds,
        )
        self.runtime_executor = RuntimeTextExecutor(run_text, max_pending=4, cancel_task=cancel_task)
        self._command_matcher = command_matcher
        self._asr_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-asr")
        self._asr_lock = Lock()
        self._asr_token = 0
        # Successive short VAD segments from one spoken command are concatenated
        # here so a late pause does not replace the first half with the second.
        self._utterance_samples: list[float] = []
        self._frames: Queue[AudioFrame] = Queue(maxsize=frame_queue_size)
        # Capture blocks are 100 ms, so 20 frames keep about 2 s. The keyword
        # spotter reports the wake phrase only after its chunk delay, which is
        # already inside the following command. This window has to hold the
        # wake phrase itself, or that audio never reaches VAD.
        self._pre_roll: deque[AudioFrame] = deque(maxlen=pre_roll_frames)
        self._idle_probe_seconds = idle_probe_seconds
        self._call_reconnect_grace_seconds = call_reconnect_grace_seconds
        self.call_reconnect_grace_seconds = call_reconnect_grace_seconds
        self._wake_callback = wake_callback
        self._transcript_callback = transcript_callback
        self.monitor = AudioLevelMonitor()
        self._call_audio_sink: Callable[[Any, int], None] | None = None
        self._stop = Event()
        self._state_lock = Lock()
        self._engine_lock = RLock()
        self._available = False
        self._engine_loaded = False
        self._error: str | None = None
        self._dropped_frames = 0
        self._last_frame_at: float | None = None
        self._last_wake_at: float | None = None
        self._last_transcript_at: float | None = None
        self._logged_recognition = ""
        self._last_probe_at = 0.0
        self._retry_delay_seconds = 1.0
        self._next_retry_at = 0.0
        self._worker: Thread | None = None
        self._call_pending_at: float | None = None
        self._call_disconnected_at: float | None = None
        self._asr_reset_requested = Event()
        self._reply_queue: Queue[Callable[[], None]] = Queue(maxsize=8)
        self._reply_worker: Thread | None = None
        self._reply_pipeline: ReplySpeechPipeline | None = None
        self._reply_cache_thread: Thread | None = None
        self._last_reply_timing: dict[str, Any] | None = None
        # True while the user is still talking; a fast runtime result is held
        # back so the reply never talks over the command itself.
        self._utterance_active = False
        self._deferred_reply: tuple[str, int, int | None] | None = None
        self._turn_marks: dict[str, float] = {}
        self._recent_dispatch_text = ""
        self._recent_dispatch_until = 0.0
        self._recent_dispatch_mergeable = False
        self._held_transcript: _HeldTranscript | None = None
        self._held_timer: Timer | None = None
        self._held_lock = Lock()
        self._utterance_lock = Lock()
        self._max_spoken_chars = _env_int("ROBOTSOUL_VOICE_MAX_SPOKEN_CHARS", 30)
        # How long a half-command may wait for its second VAD segment. The
        # first segment already looked incomplete, so waiting here costs less
        # than paying a longer silence wait on every utterance.
        self._merge_grace = _env_float("ROBOTSOUL_VOICE_MERGE_GRACE_SECONDS", 0.8)
        self._merge_max_merges = _env_int("ROBOTSOUL_VOICE_MERGE_MAX_SEGMENTS", 3)
        if hasattr(self.capture, "set_callback"):
            self.capture.set_callback(self._on_local_audio)

    def start(self) -> CaptureState:
        if self._worker is not None and self._worker.is_alive():
            return self.arbiter.state
        self._note("Info", "语音服务启动")
        try:
            self._available = True
            self._error = None
            if self.capture.probe():
                self._load_engine()
                self._open_local_stream()
                self._start_reply_cache()
                self._note("Info", self._capture_status("本地麦克风已打开"))
                self.arbiter.refresh_local()
                self._reset_retry()
            else:
                self._schedule_retry()
        except Exception as exc:
            self._error = f"Local voice input unavailable: {exc}"
            self.arbiter.fail_local()
            self._schedule_retry()
        self._stop.clear()
        self._worker = Thread(target=self._run, name="voice-recognition", daemon=True)
        self._worker.start()
        self._reply_worker = Thread(target=self._run_reply_queue, name="voice-reply", daemon=True)
        self._reply_worker.start()
        state = self.arbiter.state
        if self._error:
            self._note("Error", self._error)
        elif state == CaptureState.PASSIVE:
            self._note("Info", "进入被动监听")
        elif state == CaptureState.SLEEP:
            self._note("Warning", "没有可用麦克风，进入休眠")
        return state

    def stop(self, *, wait: bool = True) -> None:
        self._stop.set()
        # Abort TTS/playback first: the pipeline may be blocked in a speech API
        # call, and joining the reply thread would otherwise wait for it.
        if self._reply_pipeline is not None:
            try:
                self._reply_pipeline.stop()
            except Exception:
                pass
        worker = self._worker
        if wait and worker is not None:
            worker.join(timeout=2)
        self._worker = None
        self._cancel_held()
        reply_worker = self._reply_worker
        if wait and reply_worker is not None:
            reply_worker.join(timeout=2)
        self._reply_worker = None
        self._asr_pool.shutdown(wait=wait, cancel_futures=not wait)
        self.runtime_executor.close(wait=wait)
        try:
            self.arbiter.sleep()
        finally:
            try:
                self.capture.close()
            except Exception:
                pass
            with self._engine_lock:
                self.engine.close()
                self._engine_loaded = False
        if self.speaker is not None:
            self._reply_pipeline = None
            try:
                self.speaker.close()
            except Exception:
                pass
        self._note("Info", "语音服务停止")

    def refresh_device(self) -> CaptureState:
        if not self._available or self.arbiter.state in {
            CaptureState.CALL, CaptureState.CALL_PENDING, CaptureState.CALL_RECONNECTING
        }:
            return self.arbiter.state
        if monotonic() < self._next_retry_at:
            return self.arbiter.state
        previous = self.arbiter.state
        if previous == CaptureState.SLEEP:
            try:
                if not self.capture.probe():
                    self._schedule_retry()
                    return CaptureState.SLEEP
                self._load_engine()
                self._open_local_stream()
            except Exception as exc:
                self._fail(exc)
                return CaptureState.SLEEP
        state = self.arbiter.refresh_local()
        if state == CaptureState.PASSIVE and previous != CaptureState.PASSIVE:
            try:
                self._open_local_stream()
            except Exception as exc:
                self._fail(exc)
        if state == CaptureState.PASSIVE:
            with self._state_lock:
                self._error = None
            self._reset_retry()
            if previous != CaptureState.PASSIVE:
                self._note("Info", "进入被动监听")
        elif state == CaptureState.SLEEP and previous == CaptureState.PASSIVE:
            self._clear_audio()
            self._schedule_retry()
            self._note("Warning", "没有可用麦克风，进入休眠")
        return state

    def begin_call(self) -> int:
        if not self._available:
            raise RuntimeError(self._error or "Voice service is unavailable")
        generation = self.arbiter.begin_call()
        try:
            self._load_engine()
            self._clear_audio()
            self.gate.reset()
            with self._engine_lock:
                self.engine.reset_asr()
            if self.speaker is not None and hasattr(self.speaker, "stop"):
                self.speaker.stop()
            self._call_pending_at = monotonic()
            self._note("Info", "开始建立通话")
            return generation
        except Exception as exc:
            self.arbiter.end_call()
            self._call_pending_at = None
            self._note("Error", f"建立通话失败：{exc}")
            raise

    def call_disconnected(self) -> None:
        self.arbiter.call_disconnected()
        self._call_disconnected_at = monotonic()
        self._clear_audio(clear_sink=False)
        self.gate.reset()
        self._asr_reset_requested.set()
        self._note("Warning", "通话断开，等待重连")

    def call_connected(self) -> int:
        generation = self.arbiter.call_connected()
        self._call_pending_at = None
        self._note("Info", "通话已接通")
        return generation

    def call_reconnected(self) -> int:
        generation = self.arbiter.call_reconnected()
        self._call_disconnected_at = None
        self._note("Info", "通话已恢复")
        return generation

    def end_call(self) -> CaptureState:
        self._clear_audio()
        self.gate.reset()
        if self._engine_loaded:
            with self._engine_lock:
                self.engine.reset_asr()
                if hasattr(self.engine, "reset_vad"):
                    self.engine.reset_vad()
        state = self.arbiter.end_call()
        self._call_pending_at = None
        self._call_disconnected_at = None
        if state == CaptureState.PASSIVE and self._available:
            try:
                self._open_local_stream()
            except Exception as exc:
                self._fail(exc)
        self._note("Info", f"通话结束，状态 {state.value}")
        return state

    def accept_call_audio(self, generation: int, samples: Any, sample_rate: int) -> bool:
        frame = self._make_frame(generation, samples, sample_rate)
        if not self.arbiter.accepts(frame, source="call"):
            return False
        return self._enqueue(frame)

    def set_call_audio_sink(self, sink: Callable[[Any, int], None] | None) -> None:
        with self._state_lock:
            self._call_audio_sink = sink

    def _stop_spoken_output(self) -> None:
        if self._reply_pipeline is not None:
            # Abort the producer thread as well: otherwise it keeps synthesizing
            # the rest of a reply nobody will hear.
            try:
                self._reply_pipeline.stop()
            except Exception:
                pass
        if self.speaker is not None and hasattr(self.speaker, "stop"):
            try:
                self.speaker.stop()
            except Exception:
                pass
        with self._state_lock:
            sink = self._call_audio_sink
        clear = getattr(sink, "clear", None)
        if callable(clear):
            try:
                clear()
            except Exception:
                pass

    def health(self) -> VoiceHealth:
        with self._state_lock:
            return VoiceHealth(
                state=self.arbiter.state,
                available=self._available,
                error=self._error,
                dropped_frames=self._dropped_frames,
                last_frame_at=self._last_frame_at,
                last_wake_at=self._last_wake_at,
                last_transcript_at=self._last_transcript_at,
            )

    @property
    def state(self) -> CaptureState:
        return self.arbiter.state

    @property
    def generation(self) -> int:
        return self.arbiter.generation

    @property
    def queue_depth(self) -> int:
        """Frames waiting for the recognition worker (read-only status)."""
        return self._frames.qsize()

    @property
    def engine_loaded(self) -> bool:
        """Whether the native voice models have been loaded (read-only status)."""
        return self._engine_loaded

    def _open_local_stream(self) -> None:
        if not hasattr(self.capture, "set_callback"):
            raise RuntimeError("audio capture backend must support set_callback")
        self.capture.set_callback(self._on_local_audio)

    def _capture_status(self, prefix: str) -> str:
        rate = getattr(self.capture, "sample_rate", None)
        device = getattr(self.capture, "device", None) or "default"
        return f"{prefix} rate={rate} device={device}"

    def _load_engine(self) -> None:
        if not self._engine_loaded:
            self.engine.load()
            self._engine_loaded = True

    def _schedule_retry(self) -> None:
        self._next_retry_at = monotonic() + self._retry_delay_seconds
        self._retry_delay_seconds = min(self._retry_delay_seconds * 2.0, 30.0)

    def _reset_retry(self) -> None:
        self._next_retry_at = 0.0
        self._retry_delay_seconds = 1.0

    def _on_local_audio(self, samples: Any, sample_rate: int) -> None:
        if self.arbiter.state != CaptureState.PASSIVE:
            return
        frame = self._make_frame(self.arbiter.generation, samples, sample_rate)
        if not self.arbiter.accepts(frame, source="local"):
            return
        self._enqueue(frame)

    def _make_frame(self, generation: int, samples: Any, sample_rate: int) -> AudioFrame:
        if hasattr(samples, "tolist"):
            flattened = samples.reshape(-1).tolist() if hasattr(samples, "reshape") else samples.tolist()
        else:
            flattened = samples
        return AudioFrame(generation, tuple(float(value) for value in flattened), sample_rate, monotonic())

    def _enqueue(self, frame: AudioFrame) -> bool:
        self._push_level("input", frame.samples, frame.sample_rate)
        try:
            self._frames.put_nowait(frame)
            with self._state_lock:
                self._last_frame_at = frame.captured_at
            return True
        except Full:
            try:
                self._frames.get_nowait()
                self._frames.task_done()
            except Empty:
                pass
            with self._state_lock:
                self._dropped_frames += 1
            try:
                self._frames.put_nowait(frame)
                return True
            except Full:
                with self._state_lock:
                    self._dropped_frames += 1
                return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self._frames.get(timeout=0.1)
            except Empty:
                now = monotonic()
                if now - self._last_probe_at >= self._idle_probe_seconds:
                    self._last_probe_at = now
                    self.refresh_device()
                if self.gate.expired(now=now):
                    if self._engine_loaded:
                        with self._engine_lock:
                            self.engine.reset_asr()
                if self._asr_reset_requested.is_set() and self._engine_loaded:
                    with self._engine_lock:
                        self.engine.reset_asr()
                    self._asr_reset_requested.clear()
                if (
                    self.arbiter.state == CaptureState.CALL_PENDING
                    and self._call_pending_at is not None
                    and now - self._call_pending_at >= self._call_reconnect_grace_seconds
                ):
                    self.end_call()
                if (
                    self.arbiter.state == CaptureState.CALL_RECONNECTING
                    and self._call_disconnected_at is not None
                    and now - self._call_disconnected_at >= self._call_reconnect_grace_seconds
                ):
                    self.end_call()
                continue
            try:
                self._process(frame)
            except Exception as exc:
                self._fail(exc)
            finally:
                self._frames.task_done()

    def _process(self, frame: AudioFrame) -> None:
        with self._engine_lock:
            if self._asr_reset_requested.is_set() and self._engine_loaded:
                self.engine.reset_asr()
                self._asr_reset_requested.clear()
            self._process_locked(frame)

    def _process_locked(self, frame: AudioFrame) -> None:
        state = self.arbiter.state
        now = monotonic()
        if state == CaptureState.CALL_RECONNECTING:
            disconnected_at = getattr(self, "_call_disconnected_at", None)
            if disconnected_at is not None and now - disconnected_at >= self._call_reconnect_grace_seconds:
                self.end_call()
                return
        if (
            state == CaptureState.CALL_PENDING
            and self._call_pending_at is not None
            and now - self._call_pending_at >= self._call_reconnect_grace_seconds
        ):
            self.end_call()
            return
        if state == CaptureState.PASSIVE and self.gate.expired(now=now):
            self._note("Info", "等待指令超时，回到唤醒监听")
            self._pre_roll.clear()
            self._utterance_active = False
            self._cancel_held()
            self._flush_deferred_reply()
            self._reset_utterance()
            if self._engine_loaded:
                self.engine.reset_asr()
                if hasattr(self.engine, "reset_vad"):
                    self.engine.reset_vad()
        source = "call" if state in {CaptureState.CALL, CaptureState.CALL_RECONNECTING} else "local"
        if not self.arbiter.accepts(frame, source=source):
            return
        samples = frame.samples
        if state == CaptureState.PASSIVE and not self.gate.active:
            self._pre_roll.append(frame)
            if not self._engine_loaded:
                return
            wake = self.engine.accept_wake(samples, frame.sample_rate)
            if not wake:
                return
            self.gate.wake()
            self._logged_recognition = ""
            self._reset_utterance()
            self._cancel_held()
            self._utterance_active = False
            self._deferred_reply = None
            self._turn_marks = {}
            with self._state_lock:
                self._last_wake_at = monotonic()
            self._turn_mark("wake", self._last_wake_at)
            if self._wake_callback is not None:
                self._wake_callback(str(wake))
            self._note("Info", f"唤醒：{str(wake).strip()}")
            if hasattr(self.engine, "reset_vad"):
                self.engine.reset_vad()
            # The spotter fires after the wake phrase has already been spoken.
            # Replay that audio into VAD; accept_asr does not keep samples.
            pre_roll = tuple(self._pre_roll)
            self._pre_roll.clear()
            for buffered in pre_roll:
                self._consume_utterance(buffered)
            return
        if state not in {CaptureState.CALL, CaptureState.CALL_RECONNECTING} and not self.gate.active:
            return
        self._consume_utterance(frame)

    def _reset_utterance(self) -> None:
        with self._utterance_lock:
            self._utterance_samples.clear()

    def _consume_utterance(self, frame: AudioFrame) -> None:
        samples = frame.samples
        if frame.sample_rate != self.sample_rate:
            samples = self._resample(samples, frame.sample_rate, self.sample_rate)
        endpoint = self.engine.accept_vad(samples)
        speaking = callable(getattr(self.engine, "speech_detected", None)) and bool(self.engine.speech_detected())
        transcribe = getattr(self.engine, "transcribe_segments", None)
        if endpoint and callable(transcribe):
            segments = _owned_segments(self.engine.drain_speech_segments())
            # Do not reset the VAD here: draining already popped the finished
            # segment, and a reset would drop the first samples of the next
            # utterance when B starts in the same frame the endpoint fires.
            with self._utterance_lock:
                for value in _owned_segment_samples(segments):
                    self._utterance_samples.append(value)
                snapshot = tuple(self._utterance_samples)
            if not snapshot:
                return
            self.gate.hold("asr")
            self._announce(self.gate.disengage("speech"))
            self._submit_transcription(
                [_OwnedSegment(snapshot)],
                frame.generation,
            )
            return
        if speaking:
            self._utterance_active = True
            self.gate.engage("speech")
        else:
            self._announce(self.gate.disengage("speech"))
        result = self.engine.accept_asr(samples, self.sample_rate)
        self._observe_recognition(result)
        if endpoint or (result is not None and result.is_endpoint):
            self.engine.drain_speech_segments()
            if result is not None and not result.is_endpoint and hasattr(self.engine, "reset_asr"):
                self.engine.reset_asr()
        if result is not None and (result.is_endpoint or endpoint):
            self._handle_asr_result(result, force_endpoint=bool(endpoint), generation=frame.generation)

    @staticmethod
    def _resample(samples: tuple[float, ...], source_rate: int, target_rate: int) -> tuple[float, ...]:
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("audio sample rate must be positive")
        if not samples or source_rate == target_rate:
            return samples
        count = max(1, round(len(samples) * target_rate / source_rate))
        if count == 1:
            return (samples[0],)
        scale = (len(samples) - 1) / (count - 1)
        converted = []
        for index in range(count):
            position = index * scale
            left = int(position)
            right = min(left + 1, len(samples) - 1)
            fraction = position - left
            converted.append(samples[left] * (1.0 - fraction) + samples[right] * fraction)
        return tuple(converted)

    def _submit_transcription(self, segments: Any, generation: int) -> None:
        with self._asr_lock:
            self._asr_token += 1
            token = self._asr_token
        self._asr_pool.submit(self._recognize, segments, generation, token)

    def _current_asr_token(self) -> int:
        with self._asr_lock:
            return self._asr_token

    def _recognize(self, segments: Any, generation: int, token: int) -> None:
        fresh: str | None = None
        try:
            if self._stop.is_set() or token != self._current_asr_token():
                return
            try:
                result = self.engine.transcribe_segments(segments)
            except Exception as exc:
                if token == self._current_asr_token():
                    self._note("Warning", f"识别接口失败：{exc}")
                    fresh = self.gate.mode
                return
            if self._stop.is_set() or token != self._current_asr_token() or generation != self.arbiter.generation:
                return
            # Speech resumed while the API was busy: keep the PCM and wait for
            # the next VAD segment so the command is not cut in half.
            with self._engine_lock:
                speaking = callable(getattr(self.engine, "speech_detected", None)) and bool(
                    self.engine.speech_detected()
                )
                pending = callable(getattr(self.engine, "has_speech_segment", None)) and bool(
                    self.engine.has_speech_segment()
                )
            if (speaking or pending) and token == self._current_asr_token():
                fresh = self.gate.mode
                return
            submitted = self._handle_asr_result(result, force_endpoint=True, generation=generation, reset_endpoint=False)
            if not submitted and token == self._current_asr_token():
                fresh = self.gate.mode
        finally:
            latest = token == self._current_asr_token()
            kind = self.gate.release("asr", fresh=fresh if latest else None)
            if latest:
                self._announce(kind)

    def _handle_asr_result(
        self,
        result: Any,
        *,
        force_endpoint: bool = False,
        generation: int | None = None,
        reset_endpoint: bool = True,
    ) -> bool:
        text = getattr(result, "text", "")
        endpoint = bool(getattr(result, "is_endpoint", False) or force_endpoint)
        if not endpoint:
            return False
        # The user stopped talking (or the turn timed out): a reply that was
        # deferred while they were still speaking may be spoken now.
        self._utterance_active = False
        self._flush_deferred_reply()
        if not isinstance(text, str) or not text.strip():
            return False
        if reset_endpoint:
            with self._engine_lock:
                self.engine.reset_asr()
                if hasattr(self.engine, "reset_vad"):
                    self.engine.reset_vad()
        with self._state_lock:
            self._last_transcript_at = monotonic()
        if self._transcript_callback is not None:
            self._transcript_callback(text)
        cleaned = _recognition_text(text)
        phrases = getattr(self.engine, "wake_phrases", None) or (
            (str(getattr(self.engine, "wake_phrase", "")).strip(),) if getattr(self.engine, "wake_phrase", "") else ()
        )
        normalized = cleaned.replace(" ", "")
        if normalized and any(
            normalized == phrase.strip().replace(" ", "") or _is_wake_only(cleaned, phrase)
            for phrase in phrases
        ):
            # Saying only the wake word (then pausing before the command) is not
            # a command; keep the gate open and wait for the follow-up audio.
            self._note("Info", f"忽略仅唤醒词的识别：{cleaned}")
            self._reset_utterance()
            return False
        self._note("Info", f"识别：{cleaned}（{self._audio_source_label()}）")
        self._logged_recognition = ""
        expected_generation = self.arbiter.generation if generation is None else generation
        if expected_generation != self.arbiter.generation:
            self._note("Warning", f"会话已更换，未提交：{cleaned}")
            return False
        return self._consider_final(text, cleaned, expected_generation)

    # --- split-transcript merge, dispatch and reply pipeline -----------------

    def _consider_final(self, text: str, cleaned: str, generation: int) -> bool:
        """Dispatch now, merge a split continuation, or briefly hold a half-command."""
        with self._held_lock:
            held = self._held_transcript
        if held is not None and held.generation == generation:
            merged = self._merge_transcript(held, cleaned)
            self._cancel_held()
            if merged is not None:
                self._turn_mark("merge")
                self._note("Info", f"合并被拆开的语音片段：{merged}")
                if (
                    self._should_hold_dispatch(merged, None)
                    and held.merges < self._merge_max_merges
                ):
                    self._hold_dispatch(merged, merged, generation, merges=held.merges + 2)
                    return True
                return self._dispatch_final(merged, merged, generation)
        merged_after_dispatch = self._continuation_of_dispatch(cleaned)
        if merged_after_dispatch is not None:
            self._turn_mark("merge_after_dispatch")
            self._note("Info", f"续接上一句识别并重新提交：{merged_after_dispatch}")
            return self._dispatch_final(merged_after_dispatch, merged_after_dispatch, generation)
        if self._recent_dispatch_matches(cleaned):
            self._recent_dispatch_text = ""
            self._note("Info", f"同一条指令已提前执行，跳过重复识别：{cleaned}")
            return False
        intent = self._command_matcher(cleaned) if self._command_matcher is not None else None
        if self._should_hold_dispatch(cleaned, intent):
            self._hold_dispatch(text, cleaned, generation)
            return True
        return self._dispatch_final(text, cleaned, generation)

    def _should_hold_dispatch(self, cleaned: str, intent: Any) -> bool:
        """True for a transcript that looks like only the first half of a command.

        A complete closed-set command is always dispatched immediately: it is
        already actionable and waiting would only add latency. A transcript
        with a dangling continuation word or clause punctuation waits briefly
        for one more VAD segment instead of running two unrelated runtimes when
        a natural pause split the sentence.
        """
        if intent is not None:
            return False
        text = cleaned.strip()
        if not text:
            return False
        if text.endswith(_CONTINUATION_ENDINGS):
            return True
        return text[-1] in "，,、：:"

    def _merge_transcript(self, held: _HeldTranscript, cleaned: str) -> str | None:
        """Join a held fragment with a following one, when they are one sentence."""
        left = held.cleaned
        right = cleaned.strip()
        if not left or not right:
            return None
        separator = "" if (left.endswith("，,、：:") or right.startswith(_CONTINUATION_STARTERS)) else "，"
        return (left.rstrip("，,、：: ") + separator + right).strip()

    def _continuation_of_dispatch(self, cleaned: str) -> str | None:
        """A later final that continues a recently dispatched open-ended command.

        The VAD can split one sentence into A and B even after A was already
        dispatched. Rather than executing A and B twice, the runtime task is
        superseded by the joined text; a closed-set command (already executed on
        its own) is never merged with a later transcript.
        """
        previous = self._recent_dispatch_text
        if not previous or not self._recent_dispatch_mergeable:
            return None
        if monotonic() >= self._recent_dispatch_until:
            return None
        right = cleaned.strip()
        if not right or right == previous:
            return None
        if right.startswith(previous):
            # The ASR already re-transcribed the accumulated PCM (A+B).
            return right
        return (previous.rstrip("，,、：: ") + "，" + right).strip()

    def _hold_dispatch(self, text: str, cleaned: str, generation: int, *, merges: int = 1) -> None:
        self._cancel_held()
        held = _HeldTranscript(
            text=text,
            cleaned=cleaned,
            generation=generation,
            deadline=monotonic() + self._merge_grace,
            merges=merges,
        )
        timer = Timer(self._merge_grace, self._flush_held, args=(held,))
        timer.daemon = True
        with self._held_lock:
            self._held_transcript = held
            self._held_timer = timer
        self._turn_mark("hold")
        self._note("Info", f"疑似未说完，短暂等待后续语音：{cleaned}")
        timer.start()

    def _flush_held(self, held: _HeldTranscript) -> None:
        with self._held_lock:
            if self._held_transcript is not held:
                return
            self._held_transcript = None
            self._held_timer = None
        if self._stop.is_set() or held.generation != self.arbiter.generation:
            return
        self._turn_mark("hold_flush")
        self._note("Info", f"等待超时，按原识别提交：{held.cleaned}")
        try:
            self._dispatch_final(held.text, held.cleaned, held.generation)
        except Exception as exc:
            self._fail(exc)

    def _cancel_held(self) -> None:
        with self._held_lock:
            timer, self._held_timer = self._held_timer, None
            self._held_transcript = None
        if timer is not None:
            timer.cancel()

    def _dispatch_final(self, text: str, cleaned: str, generation: int) -> bool:
        if self._stop.is_set():
            return False
        intent = self._command_matcher(cleaned) if self._command_matcher is not None else None
        if intent is not None:
            self._note("Info", f"闭集技能：{getattr(intent, 'skill', '')}")
        self._turn_mark("dispatch")
        self._remember_dispatch(cleaned, mergeable=intent is None)
        self.gate.hold("runtime")
        self._stop_spoken_output()
        self.runtime_executor.supersede()
        self._note("Info", f"提交运行时：{cleaned}")
        self._reset_utterance()
        try:
            context_provider = getattr(self, "runtime_conversation", None)
            conversation = context_provider() if callable(context_provider) else None
            future = self.runtime_executor.submit_final(text, intent=intent, conversation=conversation)
        except Exception:
            self._release_turn("runtime")
            raise
        epoch = getattr(future, "voice_epoch", None)
        future.add_done_callback(
            lambda completed, generation=generation, epoch=epoch: self._enqueue_reply(
                completed, generation, epoch
            )
        )
        return True

    def _enqueue_reply(self, future: Future[Any], generation: int, epoch: int | None = None) -> None:
        """Handle a runtime result on the dedicated reply thread.

        The recognition worker holds ``_engine_lock`` while processing one audio
        frame, and a completed runtime future can invoke its callback in that
        same thread. Speaking there would deadlock against synthesis, so the
        reply is always produced by the reply thread.
        """
        action = lambda: self._handle_runtime_result(future, generation, epoch)
        try:
            self._reply_queue.put_nowait(action)
        except Full:
            self._note("Warning", "回复队列已满，丢弃一条过期播报")

    def _run_reply_queue(self) -> None:
        while not self._stop.is_set():
            try:
                action = self._reply_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - reply thread must stay alive
                self._fail(exc)
            finally:
                self._reply_queue.task_done()

    def _flush_deferred_reply(self) -> None:
        deferred, self._deferred_reply = self._deferred_reply, None
        if deferred is None:
            return
        response, generation, epoch = deferred
        action = lambda: self._speak_reply_guarded(response, generation, epoch)
        try:
            self._reply_queue.put_nowait(action)
        except Full:
            pass

    def _speak_reply_guarded(self, response: str, generation: int, epoch: int | None = None) -> None:
        try:
            self._speak_reply(response, generation, epoch)
        except Exception as exc:  # noqa: BLE001 - reply thread must stay alive
            self._release_turn("playback")
            self._fail(exc)

    def _reply_pipeline_instance(self) -> ReplySpeechPipeline:
        pipeline = self._reply_pipeline
        if pipeline is None:
            # The speech API does not need the engine lock; taking it here would
            # block the VAD/ASR frame loop for the whole TTS round trip.
            pipeline = ReplySpeechPipeline(self.engine, self.speaker, logger=self._note)
            self._reply_pipeline = pipeline
        return pipeline

    def _start_reply_cache(self) -> None:
        """Warm the fixed acknowledgement PCM in the background after model load."""
        if self.speaker is None:
            return
        pipeline = self._reply_pipeline_instance()

        def warm() -> None:
            try:
                pipeline.prepare_cache()
            except Exception as exc:  # noqa: BLE001 - cache is an optimisation only
                self._note("Warning", f"回复语音缓存预热失败：{type(exc).__name__}: {exc}")

        thread = Thread(target=warm, name="tts-cache", daemon=True)
        self._reply_cache_thread = thread
        thread.start()

    @property
    def reply_cache_size(self) -> int:
        pipeline = self._reply_pipeline
        return pipeline.cache_size if pipeline is not None else 0

    @property
    def last_reply_timing(self) -> dict[str, Any] | None:
        return None if self._last_reply_timing is None else dict(self._last_reply_timing)

    def _recent_dispatch_matches(self, text: str) -> bool:
        return bool(self._recent_dispatch_text) and monotonic() < self._recent_dispatch_until \
            and _command_matches(self._recent_dispatch_text, text)

    def _remember_dispatch(self, text: str, *, mergeable: bool = False) -> None:
        self._recent_dispatch_text = text.strip()
        self._recent_dispatch_until = monotonic() + max(2.0, self._merge_grace * 2.0)
        self._recent_dispatch_mergeable = mergeable

    def _turn_mark(self, stage: str, at: float | None = None) -> None:
        with self._state_lock:
            self._turn_marks[stage] = monotonic() if at is None else at

    @property
    def last_turn_timings(self) -> dict[str, float]:
        """Per-stage latency in ms relative to the wake event (diagnostics)."""
        with self._state_lock:
            marks = dict(self._turn_marks)
            wake = self._last_wake_at
        if not marks or wake is None:
            return {}
        return {stage: round((at - wake) * 1000.0, 1) for stage, at in marks.items()}

    def _handle_runtime_result(self, future: Future[Any], generation: int, epoch: int | None = None) -> None:
        if epoch is None:
            epoch = getattr(future, "voice_epoch", None)
        try:
            if future.cancelled() or self._turn_replaced(epoch):
                self._release_turn("runtime")
                return
            result = future.result()
            task = getattr(result, "task", None)
            task_id = getattr(task, "task_id", "")
            status = getattr(getattr(task, "status", None), "value", None) or getattr(task, "status", "") or "unknown"
            evaluation = getattr(result, "evaluation", None)
            if generation != self.arbiter.generation or self._turn_replaced(epoch):
                self._release_turn("runtime")
                self._note("Warning", f"运行时结果已过期，未播报 id={task_id} 状态={status}")
                return
            reply = getattr(result, "reply", "")
            if isinstance(reply, str) and reply.strip():
                response = reply.strip()
                success = status not in {"failed", "interrupted"}
                self._note("Info", f"运行时答复 id={task_id} 状态={status} 内容={response}")
            elif evaluation is not None and evaluation.success:
                response = evaluation.conclusion
                success = True
                self._note("Info", f"运行时完成 id={task_id} 状态={status} 结论={response}")
            elif task is not None and getattr(task, "error", None):
                response = task.error
                success = False
                self._note("Warning", f"运行时失败 id={task_id} 状态={status} 原因={response}")
            else:
                response = "操作已处理。"
                success = False
                self._note("Info", f"运行时返回 id={task_id} 状态={status} 结论={response}")
            response = self._spoken_reply(response, success=success)
            # Long replies cost seconds of TTS before the first audio; a short
            # acknowledgement keeps the turn responsive.
            response = _shorten_spoken_reply(response, success, self._max_spoken_chars)
            self._turn_mark("runtime_done")
            self.gate.hold("playback")
            self._release_turn("runtime")
            if self._utterance_active:
                # A speculative command can finish before the user stops
                # talking; wait for the ASR endpoint so TTS never talks over
                # the command itself.
                self._deferred_reply = (response, generation, epoch)
                self._turn_mark("reply_deferred")
                self._note("Info", "指令已执行，等用户说完再播报")
                return
            self._speak_reply(response, generation, epoch)
        except Exception as exc:
            self._release_turn("runtime")
            self._release_turn("playback")
            self._fail(exc)

    def _speak_reply(self, response: str, generation: int, epoch: int | None = None) -> None:
        """Synthesize and play one reply, using the low-latency unit pipeline."""
        # A newer reply replaces whatever is still playing; the reply queue is
        # serial, but a direct caller (or a cancelled turn) can race here.
        self._stop_spoken_output()
        if generation != self.arbiter.generation or self._turn_replaced(epoch):
            self._release_turn("playback")
            self._note("Warning", "合成完成但会话已更换，未播放")
            return
        call_mode = self.arbiter.state in {CaptureState.CALL, CaptureState.CALL_RECONNECTING}
        with self._state_lock:
            call_sink = self._call_audio_sink
        if call_mode or self.speaker is None:
            self._speak_reply_whole(response, generation, epoch, call_sink if call_mode else None)
            return
        pipeline = self._reply_pipeline_instance()
        try:
            timing = pipeline.speak(
                response,
                on_first_audio=lambda: self._turn_mark("first_audio"),
                on_chunk=lambda chunk, rate: self._push_level("output", chunk, rate),
            )
        except (SpeechApiError, VoiceEngineError) as exc:
            self._note("Warning", f"合成接口失败：{exc}")
            self._release_turn("playback")
            if not self._turn_replaced(epoch):
                self._finish_reply()
            return
        self._last_reply_timing = timing.as_dict()
        self._turn_mark("reply_done")
        if timing.aborted:
            self._note("Info", "上一句播报已打断")
        else:
            # Without acoustic echo cancellation the microphone hears the reply
            # that was just played. Drop that audio only after this utterance
            # finishes; a replaced utterance must leave the next sentence alone.
            self._clear_audio()
            with self._engine_lock:
                self.engine.reset_asr()
                if hasattr(self.engine, "reset_vad"):
                    self.engine.reset_vad()
            self._turn_mark("clear_audio")
        self._release_turn("playback")
        if not self._turn_replaced(epoch):
            self._finish_reply()

    def _speak_reply_whole(self, response: str, generation: int, epoch: int | None,
                           call_sink: Callable[[Any, int], None] | None) -> None:
        """Non-local or call path: one synthesis, then play the whole reply."""
        try:
            audio = self.engine.synthesize(response)
        except (SpeechApiError, VoiceEngineError) as exc:
            self._note("Warning", f"合成接口失败：{exc}")
            self._release_turn("playback")
            if not self._turn_replaced(epoch):
                self._finish_reply()
            return
        if generation != self.arbiter.generation or self._turn_replaced(epoch):
            self._release_turn("playback")
            self._note("Warning", f"合成完成但会话已更换，未播放")
            return
        samples = getattr(audio, "samples", audio)
        sample_rate = int(getattr(audio, "sample_rate", self.sample_rate))
        self._push_level("output", samples, sample_rate)
        cancelled = False
        if call_sink is not None:
            call_sink(samples, sample_rate)
        elif self.speaker is not None:
            cancelled = self.speaker.play(samples, sample_rate) is True
            if not cancelled:
                self._clear_audio()
                with self._engine_lock:
                    self.engine.reset_asr()
                    if hasattr(self.engine, "reset_vad"):
                        self.engine.reset_vad()
        if cancelled:
            self._note("Info", "上一句播报已打断")
        self._release_turn("playback")
        if not self._turn_replaced(epoch):
            self._finish_reply()

    def _turn_replaced(self, epoch: int | None) -> bool:
        return epoch is not None and epoch != self.runtime_executor.epoch

    def _finish_reply(self) -> None:
        if self.gate.reply_finished():
            self._note("Info", "回复结束，等待后续指令")

    def _release_turn(self, reason: str, *, fresh: str | None = None) -> None:
        self._announce(self.gate.release(reason, fresh=fresh))

    def _announce(self, kind: str) -> None:
        if kind == "follow_up":
            self._note("Info", "回复结束，等待后续指令")

    _CJK = re.compile(r"[\u4e00-\u9fff]")

    def _spoken_reply(self, text: Any, *, success: bool) -> str:
        """Keep spoken output in Chinese.

        Runtime and adapter messages are English (for logs and the dashboard).
        Speak a short Chinese acknowledgement instead of mixed-language replies.
        """
        if isinstance(text, str) and self._CJK.search(text):
            return text
        return "好的，已完成。" if success else "抱歉，这个操作没有成功。"

    def _clear_audio(self, *, clear_sink: bool = True) -> None:
        while True:
            try:
                self._frames.get_nowait()
                self._frames.task_done()
            except Empty:
                break
        self._pre_roll.clear()
        self._reset_utterance()
        self._call_disconnected_at = None if clear_sink else self._call_disconnected_at
        if clear_sink:
            with self._state_lock:
                self._call_audio_sink = None

    def _push_level(self, side: str, samples: Any, sample_rate: int) -> None:
        try:
            if side == "input":
                self.monitor.push_input(samples, sample_rate)
            else:
                self.monitor.push_output(samples, sample_rate)
        except Exception:
            return

    def _audio_source_label(self) -> str:
        if self.arbiter.state in {CaptureState.CALL, CaptureState.CALL_RECONNECTING}:
            return "网页通话"
        return "本地麦克风"

    def _observe_recognition(self, result: Any) -> None:
        if getattr(result, "is_endpoint", False):
            return
        cleaned = _recognition_text(getattr(result, "text", ""))
        if not cleaned or cleaned == self._logged_recognition:
            return
        self._logged_recognition = cleaned
        self._note("Info", f"识别中：{cleaned}（{self._audio_source_label()}）")

    def _note(self, level: str, message: str) -> None:
        runtime_log("Voice", level, message)

    def _fail(self, error: Exception) -> None:
        with self._state_lock:
            self._error = f"{type(error).__name__}: {error}"
        self._note("Error", self._error or "语音服务失败")
        self._cancel_held()
        self._deferred_reply = None
        self._utterance_active = False
        self._clear_audio()
        try:
            self.arbiter.sleep()
        except Exception:
            pass
        self._call_pending_at = None
        self._call_disconnected_at = None
        if self._engine_loaded:
            with self._engine_lock:
                self.engine.close()
                self._engine_loaded = False
        self.gate.reset()
        self._schedule_retry()

