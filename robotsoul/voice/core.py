from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from queue import Full, Queue
from threading import Lock
from time import monotonic
from typing import Callable, Generic, Protocol, TypeVar


class CaptureState(str, Enum):
    SLEEP = "SLEEP"
    PASSIVE = "PASSIVE"
    CALL_PENDING = "CALL_PENDING"
    CALL = "CALL"
    CALL_RECONNECTING = "CALL_RECONNECTING"


@dataclass(frozen=True)
class AudioFrame:
    generation: int
    samples: tuple[float, ...]
    sample_rate: int
    captured_at: float


class LocalCapture(Protocol):
    def probe(self) -> bool: ...
    def open(self) -> None: ...
    def close(self) -> None: ...


class CaptureArbiter:
    """Serializes local capture and browser-call ownership; no device opens implicitly."""

    def __init__(self, local: LocalCapture):
        self._local = local
        self._lock = Lock()
        self._state = CaptureState.SLEEP
        self._generation = 0
        self._local_open = False
        self._local_release_failed = False
        self._call_lease = False

    @property
    def state(self) -> CaptureState:
        with self._lock:
            return self._state

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def refresh_local(self) -> CaptureState:
        with self._lock:
            if self._call_lease:
                return self._state
            try:
                if self._local_release_failed:
                    self._local.close()
                    self._local_open = False
                    self._local_release_failed = False
                available = bool(self._local.probe())
                if available:
                    if not self._local_open:
                        self._local.open()
                        self._local_open = True
                        self._generation += 1
                    elif self._state != CaptureState.PASSIVE:
                        self._generation += 1
                    self._state = CaptureState.PASSIVE
                else:
                    if self._local_open:
                        self._local.close()
                        self._local_open = False
                        self._generation += 1
                    elif self._state != CaptureState.SLEEP:
                        self._generation += 1
                    self._state = CaptureState.SLEEP
            except Exception:
                if self._state != CaptureState.SLEEP or self._local_open:
                    self._generation += 1
                self._state = CaptureState.SLEEP
                try:
                    self._local.close()
                    self._local_open = False
                    self._local_release_failed = False
                except Exception:
                    self._local_release_failed = True
            return self._state

    def begin_call(self) -> int:
        with self._lock:
            if self._state in {CaptureState.CALL_PENDING, CaptureState.CALL, CaptureState.CALL_RECONNECTING}:
                raise RuntimeError("a call already owns audio capture")
            self._generation += 1
            self._state = CaptureState.CALL_PENDING
            if self._local_open:
                try:
                    self._local.close()
                except Exception as exc:
                    self._state = CaptureState.SLEEP
                    self._local_release_failed = True
                    raise RuntimeError("local microphone release failed; call audio rejected") from exc
                self._local_open = False
                self._local_release_failed = False
            self._call_lease = True
            self._state = CaptureState.CALL_PENDING
            return self._generation

    def call_connected(self) -> int:
        with self._lock:
            if not self._call_lease or self._state != CaptureState.CALL_PENDING:
                raise RuntimeError("call is not pending")
            self._generation += 1
            self._state = CaptureState.CALL
            return self._generation

    def call_disconnected(self) -> None:
        with self._lock:
            if not self._call_lease:
                raise RuntimeError("no active call lease")
            self._generation += 1
            self._state = CaptureState.CALL_RECONNECTING

    def call_reconnected(self) -> int:
        with self._lock:
            if not self._call_lease or self._state != CaptureState.CALL_RECONNECTING:
                raise RuntimeError("call is not reconnecting")
            self._generation += 1
            self._state = CaptureState.CALL
            return self._generation

    def end_call(self) -> CaptureState:
        with self._lock:
            if not self._call_lease:
                return self._state
            self._generation += 1
            self._call_lease = False
            self._state = CaptureState.SLEEP
            # Probe/open only after dropping the call lease and clearing its ownership.
            try:
                if self._local_release_failed:
                    self._local.close()
                    self._local_open = False
                    self._local_release_failed = False
                if self._local.probe():
                    self._local.open()
                    self._local_open = True
                    self._state = CaptureState.PASSIVE
            except Exception:
                if self._local_open:
                    self._local_release_failed = True
                try:
                    self._local.close()
                    self._local_open = False
                    self._local_release_failed = False
                except Exception:
                    self._local_release_failed = True
            return self._state

    def sleep(self) -> CaptureState:
        """Release all capture ownership without reopening a device during shutdown."""
        with self._lock:
            self._generation += 1
            self._call_lease = False
            try:
                if self._local_open or self._local_release_failed:
                    self._local.close()
                self._local_open = False
                self._local_release_failed = False
            except Exception:
                self._local_release_failed = True
                raise
            finally:
                self._state = CaptureState.SLEEP
            return self._state

    def fail_local(self) -> CaptureState:
        """Close a failed local stream and keep device ownership state accurate."""
        with self._lock:
            if self._call_lease:
                return self._state
            self._generation += 1
            try:
                if self._local_open or self._local_release_failed:
                    self._local.close()
                self._local_open = False
                self._local_release_failed = False
            except Exception:
                self._local_release_failed = True
                raise
            finally:
                self._state = CaptureState.SLEEP
            return self._state

    def accepts(self, frame: AudioFrame, *, source: str) -> bool:
        with self._lock:
            if frame.generation != self._generation:
                return False
            if source == "local":
                return self._state == CaptureState.PASSIVE and self._local_open and not self._call_lease
            if source == "call":
                return self._state == CaptureState.CALL and self._call_lease
            return False


T = TypeVar("T")


class BoundedPcmBuffer(Generic[T]):
    """Small thread-safe FIFO; overflow drops oldest audio to favor freshness."""

    def __init__(self, max_frames: int):
        if max_frames < 1:
            raise ValueError("max_frames must be positive")
        self._frames: deque[T] = deque(maxlen=max_frames)
        self._lock = Lock()

    def put(self, frame: T) -> None:
        with self._lock:
            self._frames.append(frame)

    def drain(self) -> tuple[T, ...]:
        with self._lock:
            frames = tuple(self._frames)
            self._frames.clear()
            return frames

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)


class ConversationGate:
    """Bounds a wake-triggered interaction; background speech never extends it."""

    def __init__(self, *, follow_up_seconds: float = 8, session_max_seconds: float = 120):
        if follow_up_seconds <= 0 or session_max_seconds <= 0:
            raise ValueError("conversation durations must be positive")
        self.follow_up_seconds = follow_up_seconds
        self.session_max_seconds = session_max_seconds
        self._started_at: float | None = None
        self._follow_up_deadline: float | None = None
        self._command_deadline: float | None = None

    @property
    def active(self) -> bool:
        return self._started_at is not None

    def wake(self, *, now: float | None = None) -> None:
        current = monotonic() if now is None else now
        if self.active and not self.expired(now=current):
            return
        self._started_at = current
        self._follow_up_deadline = None
        self._command_deadline = current + self.follow_up_seconds

    def note_command_audio(self) -> None:
        """Keep listening once speech arrives; do not close the session for silence."""
        if self.active:
            self._command_deadline = None

    def reply_finished(self, *, now: float | None = None) -> None:
        if not self.active or self._follow_up_deadline is not None:
            return
        current = monotonic() if now is None else now
        self._command_deadline = None
        self._follow_up_deadline = current + self.follow_up_seconds

    def expired(self, *, now: float | None = None) -> bool:
        if self._started_at is None:
            return False
        current = monotonic() if now is None else now
        hard_deadline = self._started_at + self.session_max_seconds
        command_expired = self._command_deadline is not None and current >= self._command_deadline
        follow_expired = self._follow_up_deadline is not None and current >= self._follow_up_deadline
        if current >= hard_deadline or command_expired or follow_expired:
            self._started_at = None
            self._follow_up_deadline = None
            self._command_deadline = None
            return True
        return False

    def reset(self) -> None:
        self._started_at = None
        self._follow_up_deadline = None
        self._command_deadline = None


R = TypeVar("R")


class RuntimeTextExecutor:
    """Runs synchronous RobotRuntime.run_text away from media/audio callbacks."""

    def __init__(self, run_text: Callable[[str], R], *, max_pending: int = 4):
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._run_text = run_text
        self._queue: Queue[tuple[str, Future[R]] | None] = Queue(maxsize=max_pending)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-runtime")
        self._lock = Lock()
        self._closed = False

    def submit_final(self, text: str) -> Future[R]:
        if not text.strip():
            raise ValueError("final text must not be empty")
        future: Future[R] = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("voice runtime executor is closed")
            try:
                self._queue.put_nowait((text, future))
            except Full as exc:
                raise RuntimeError("voice runtime queue is full") from exc
            self._pool.submit(self._work_one)
        return future

    def _work_one(self) -> None:
        item = self._queue.get()
        try:
            if item is not None:
                text, future = item
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(self._run_text(text))
                    except BaseException as exc:
                        future.set_exception(exc)
        finally:
            self._queue.task_done()

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
