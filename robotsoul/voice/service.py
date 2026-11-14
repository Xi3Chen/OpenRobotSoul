from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from queue import Empty, Full, Queue
import re
from threading import Event, Lock, RLock, Thread
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
from robotsoul.voice.speech_api import SpeechApiError


def _recognition_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


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
        worker = self._worker
        if wait and worker is not None:
            worker.join(timeout=2)
        self._worker = None
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
            with self._state_lock:
                self._last_wake_at = monotonic()
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
            if hasattr(self.engine, "reset_vad"):
                self.engine.reset_vad()
            for value in _owned_segment_samples(segments):
                self._utterance_samples.append(value)
            if not self._utterance_samples:
                return
            self.gate.hold("asr")
            self._announce(self.gate.disengage("speech"))
            self._submit_transcription(
                [_OwnedSegment(tuple(self._utterance_samples))],
                frame.generation,
            )
            return
        if speaking:
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
        if not endpoint or not isinstance(text, str) or not text.strip():
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
        if normalized and any(normalized == phrase.strip().replace(" ", "") for phrase in phrases):
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
        intent = self._command_matcher(cleaned) if self._command_matcher is not None else None
        if intent is not None:
            self._note("Info", f"闭集技能：{getattr(intent, 'skill', '')}")
        self.gate.hold("runtime")
        self._stop_spoken_output()
        self.runtime_executor.supersede()
        self._note("Info", f"提交运行时：{cleaned}")
        self._reset_utterance()
        try:
            future = self.runtime_executor.submit_final(text, intent=intent)
        except Exception:
            self._release_turn("runtime")
            raise
        epoch = getattr(future, "voice_epoch", None)
        future.add_done_callback(
            lambda completed, generation=expected_generation, epoch=epoch: self._handle_runtime_result(
                completed, generation, epoch
            )
        )
        return True

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
            if evaluation is not None and evaluation.success:
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
            self.gate.hold("playback")
            self._release_turn("runtime")
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
                self._note("Warning", f"合成完成但会话已更换，未播放 id={task_id}")
                return
            samples = getattr(audio, "samples", audio)
            sample_rate = int(getattr(audio, "sample_rate", self.sample_rate))
            self._push_level("output", samples, sample_rate)
            with self._state_lock:
                call_sink = self._call_audio_sink
            cancelled = False
            if self.arbiter.state in {CaptureState.CALL, CaptureState.CALL_RECONNECTING}:
                if call_sink is not None:
                    call_sink(samples, sample_rate)
            elif self.speaker is not None:
                cancelled = self.speaker.play(samples, sample_rate) is True
                # A replaced utterance must not clear the microphone after the
                # next sentence has already started playing.
                if not cancelled:
                    # Without acoustic echo cancellation the microphone hears the
                    # reply that was just played. Drop that buffered audio only
                    # after this utterance finishes; a replaced utterance must
                    # leave the next sentence's playback alone.
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
        except Exception as exc:
            self._release_turn("runtime")
            self._release_turn("playback")
            self._fail(exc)

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

