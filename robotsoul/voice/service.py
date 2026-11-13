from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from queue import Empty, Full, Queue
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
from robotsoul.voice.monitor import AudioLevelMonitor


def _recognition_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


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
        pre_roll_frames: int = 8,
        idle_probe_seconds: float = 2,
        call_reconnect_grace_seconds: float = 15,
        wake_callback: Callable[[str], None] | None = None,
        transcript_callback: Callable[[str], None] | None = None,
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
        self.runtime_executor = RuntimeTextExecutor(run_text, max_pending=4)
        self._frames: Queue[AudioFrame] = Queue(maxsize=frame_queue_size)
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
            with self._state_lock:
                self._last_wake_at = monotonic()
            if self._wake_callback is not None:
                self._wake_callback(str(wake))
            self._note("Info", f"唤醒：{str(wake).strip()}")
            pre_roll = tuple(self._pre_roll)
            self._pre_roll.clear()
            for buffered in pre_roll:
                buffered_samples = buffered.samples
                if buffered.sample_rate != self.sample_rate:
                    buffered_samples = self._resample(buffered_samples, buffered.sample_rate, self.sample_rate)
                result = self.engine.accept_asr(buffered_samples, self.sample_rate)
                self._observe_recognition(result)
                self._handle_asr_result(result, generation=frame.generation)
            return
        if state not in {CaptureState.CALL, CaptureState.CALL_RECONNECTING} and not self.gate.active:
            return
        if frame.sample_rate != self.sample_rate:
            samples = self._resample(samples, frame.sample_rate, self.sample_rate)
        endpoint = self.engine.accept_vad(samples)
        if callable(getattr(self.engine, "speech_detected", None)) and self.engine.speech_detected():
            self.gate.note_command_audio()
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

    def _handle_asr_result(
        self,
        result: Any,
        *,
        force_endpoint: bool = False,
        generation: int | None = None,
    ) -> None:
        text = getattr(result, "text", "")
        endpoint = bool(getattr(result, "is_endpoint", False) or force_endpoint)
        if not endpoint or not isinstance(text, str) or not text.strip():
            return
        with self._engine_lock:
            self.engine.reset_asr()
            if hasattr(self.engine, "reset_vad"):
                self.engine.reset_vad()
        with self._state_lock:
            self._last_transcript_at = monotonic()
        if self._transcript_callback is not None:
            self._transcript_callback(text)
        cleaned = _recognition_text(text)
        self._note("Info", f"识别：{cleaned}（{self._audio_source_label()}）")
        self._logged_recognition = ""
        expected_generation = self.arbiter.generation if generation is None else generation
        if expected_generation != self.arbiter.generation:
            self._note("Warning", f"会话已更换，未提交：{cleaned}")
            return
        self._note("Info", f"提交运行时：{cleaned}")
        future = self.runtime_executor.submit_final(text)
        future.add_done_callback(lambda completed: self._handle_runtime_result(completed, expected_generation))

    def _handle_runtime_result(self, future: Future[Any], generation: int) -> None:
        try:
            result = future.result()
            task = getattr(result, "task", None)
            task_id = getattr(task, "task_id", "")
            status = getattr(getattr(task, "status", None), "value", None) or getattr(task, "status", "") or "unknown"
            evaluation = getattr(result, "evaluation", None)
            if generation != self.arbiter.generation:
                self._note("Warning", f"运行时结果已过期，未播报 id={task_id} 状态={status}")
                return
            if evaluation is not None and evaluation.success:
                response = evaluation.conclusion
                self._note("Info", f"运行时完成 id={task_id} 状态={status} 结论={response}")
            elif task is not None and getattr(task, "error", None):
                response = task.error
                self._note("Warning", f"运行时失败 id={task_id} 状态={status} 原因={response}")
            else:
                response = "操作已处理。"
                self._note("Info", f"运行时返回 id={task_id} 状态={status} 结论={response}")
            with self._engine_lock:
                audio = self.engine.synthesize(response)
            if generation != self.arbiter.generation:
                self._note("Warning", f"合成完成但会话已更换，未播放 id={task_id}")
                return
            samples = getattr(audio, "samples", audio)
            sample_rate = int(getattr(audio, "sample_rate", self.sample_rate))
            self._push_level("output", samples, sample_rate)
            with self._state_lock:
                call_sink = self._call_audio_sink
            if self.arbiter.state in {CaptureState.CALL, CaptureState.CALL_RECONNECTING}:
                if call_sink is not None:
                    call_sink(samples, sample_rate)
            elif self.speaker is not None:
                self.speaker.play(samples, sample_rate)
            self.gate.reply_finished()
            self._note("Info", "回复结束，等待后续指令")
        except Exception as exc:
            self._fail(exc)

    def _clear_audio(self, *, clear_sink: bool = True) -> None:
        while True:
            try:
                self._frames.get_nowait()
                self._frames.task_done()
            except Empty:
                break
        self._pre_roll.clear()
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

