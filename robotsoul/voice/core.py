from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
import inspect
from queue import Full, Queue
from threading import Lock
from time import monotonic
from typing import Any, Callable, Generic, Protocol, TypeVar
from uuid import uuid4


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
    """Bounds a wake-triggered interaction.

    The follow-up clock starts when the robot is idle. Speech, recognition,
    runtime work, and playback suspend it. A cough restores the remaining
    time; an empty transcript or a finished reply starts a fresh window.
    """

    def __init__(self, *, follow_up_seconds: float = 8, session_max_seconds: float = 120):
        if follow_up_seconds <= 0 or session_max_seconds <= 0:
            raise ValueError("conversation durations must be positive")
        self.follow_up_seconds = follow_up_seconds
        self.session_max_seconds = session_max_seconds
        self._lock = Lock()
        self._started_at: float | None = None
        self._follow_up_deadline: float | None = None
        self._command_deadline: float | None = None
        self._counts: dict[str, int] = {}
        self._levels: set[str] = set()
        self._mode = "command"
        self._follow_up_pending = False
        self._fresh: str | None = None
        self._suspended = False
        self._saved_command: float | None = None
        self._saved_follow: float | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._started_at is not None

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def wake(self, *, now: float | None = None) -> None:
        current = monotonic() if now is None else now
        with self._lock:
            if self._started_at is not None and not self._expired_locked(current):
                return
            self._clear_locked()
            self._started_at = current
            self._mode = "command"
            self._command_deadline = current + self.follow_up_seconds

    def note_command_audio(self) -> None:
        """Suspend the idle clock while speech is in progress."""
        self.engage("speech")

    def engage(self, reason: str) -> None:
        """Hold while a condition remains true. Repeated calls do not stack."""
        with self._lock:
            self._engage_locked(reason, monotonic())

    def disengage(self, reason: str, *, now: float | None = None) -> str:
        with self._lock:
            if reason not in self._levels:
                return ""
            self._levels.discard(reason)
            self._counts.pop(reason, None)
            return self._settle_locked(monotonic() if now is None else now)

    def hold(self, reason: str) -> None:
        """Add one reference. Each hold needs a matching release."""
        with self._lock:
            if self._started_at is None:
                return
            self._counts[reason] = self._counts.get(reason, 0) + 1
            self._suspend_locked(monotonic())

    def release(self, reason: str, *, now: float | None = None, fresh: str | None = None) -> str:
        with self._lock:
            count = self._counts.get(reason, 0)
            if count <= 0:
                if fresh:
                    self._fresh = fresh
                return "" if self._busy_locked() or self._started_at is None else self._settle_locked(
                    monotonic() if now is None else now
                )
            if count == 1:
                self._counts.pop(reason, None)
                self._levels.discard(reason)
            else:
                self._counts[reason] = count - 1
            if fresh:
                self._fresh = fresh
            return self._settle_locked(monotonic() if now is None else now)

    def reply_finished(self, *, now: float | None = None) -> bool:
        """Arm the follow-up window. Returns True when the clock starts now."""
        current = monotonic() if now is None else now
        with self._lock:
            if self._started_at is None:
                return False
            self._mode = "follow_up"
            self._command_deadline = None
            self._follow_up_pending = True
            self._fresh = "follow_up"
            if self._busy_locked():
                return False
            self._follow_up_deadline = current + self.follow_up_seconds
            self._follow_up_pending = False
            self._fresh = None
            self._suspended = False
            self._saved_command = None
            self._saved_follow = None
            return True

    def expired(self, *, now: float | None = None) -> bool:
        current = monotonic() if now is None else now
        with self._lock:
            return self._expired_locked(current)

    def reset(self) -> None:
        with self._lock:
            self._clear_locked()

    def _engage_locked(self, reason: str, now: float) -> None:
        if self._started_at is None or reason in self._levels:
            return
        self._levels.add(reason)
        self._counts[reason] = 1
        self._suspend_locked(now)

    def _busy_locked(self) -> bool:
        return bool(self._counts)

    def _suspend_locked(self, now: float) -> None:
        if self._suspended:
            return
        self._saved_command = None if self._command_deadline is None else self._command_deadline - now
        self._saved_follow = None if self._follow_up_deadline is None else self._follow_up_deadline - now
        self._command_deadline = None
        self._follow_up_deadline = None
        self._suspended = True

    def _settle_locked(self, now: float) -> str:
        if self._busy_locked() or self._started_at is None:
            return ""
        if self._follow_up_pending or self._fresh == "follow_up":
            self._follow_up_pending = False
            self._fresh = None
            self._mode = "follow_up"
            self._suspended = False
            self._saved_command = None
            self._saved_follow = None
            self._command_deadline = None
            self._follow_up_deadline = now + self.follow_up_seconds
            return "follow_up"
        if self._fresh == "command":
            self._fresh = None
            self._mode = "command"
            self._suspended = False
            self._saved_command = None
            self._saved_follow = None
            self._follow_up_deadline = None
            self._command_deadline = now + self.follow_up_seconds
            return "command"
        if self._suspended:
            self._suspended = False
            if self._saved_follow is not None:
                self._follow_up_deadline = now + self._saved_follow
            if self._saved_command is not None:
                self._command_deadline = now + self._saved_command
            self._saved_command = None
            self._saved_follow = None
        return ""

    def _expired_locked(self, current: float) -> bool:
        if self._started_at is None:
            return False
        if current >= self._started_at + self.session_max_seconds:
            self._clear_locked()
            return True
        if self._busy_locked():
            return False
        command_expired = self._command_deadline is not None and current >= self._command_deadline
        follow_expired = self._follow_up_deadline is not None and current >= self._follow_up_deadline
        if command_expired or follow_expired:
            self._clear_locked()
            return True
        return False

    def _clear_locked(self) -> None:
        self._started_at = None
        self._follow_up_deadline = None
        self._command_deadline = None
        self._counts.clear()
        self._levels.clear()
        self._mode = "command"
        self._follow_up_pending = False
        self._fresh = None
        self._suspended = False
        self._saved_command = None
        self._saved_follow = None


R = TypeVar("R")


class RuntimeTextExecutor:
    """Runs synchronous RobotRuntime.run_text away from media/audio callbacks.

    A newer final transcript supersedes queued text. The active task id is
    handed to ``cancel_task`` so an interruptible skill can stop.
    """

    def __init__(
        self,
        run_text: Callable[..., R],
        *,
        max_pending: int = 4,
        cancel_task: Callable[[str], Any] | None = None,
    ):
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._run_text = run_text
        self._cancel_task = cancel_task
        self._queue: Queue[tuple[int, str, str, Any, Future[R]] | None] = Queue(maxsize=max_pending)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-runtime")
        self._lock = Lock()
        self._closed = False
        self._epoch = 0
        self._active_task_id: str | None = None

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def submit_final(self, text: str, *, intent: Any = None) -> Future[R]:
        if not text.strip():
            raise ValueError("final text must not be empty")
        future: Future[R] = Future()
        task_id = str(uuid4())
        with self._lock:
            if self._closed:
                raise RuntimeError("voice runtime executor is closed")
            epoch = self._epoch
            try:
                self._queue.put_nowait((epoch, text, task_id, intent, future))
            except Full as exc:
                raise RuntimeError("voice runtime queue is full") from exc
            future.voice_epoch = epoch  # type: ignore[attr-defined]
            self._pool.submit(self._work_one)
        return future

    def supersede(self) -> None:
        """Ignore queued text and ask the runtime to drop the active task."""
        with self._lock:
            self._epoch += 1
            active = self._active_task_id
        if active and self._cancel_task is not None:
            try:
                self._cancel_task(active)
            except Exception:
                return

    def _work_one(self) -> None:
        item = self._queue.get()
        try:
            if item is None:
                return
            epoch, text, task_id, intent, future = item
            with self._lock:
                skipped = self._epoch != epoch
            if skipped:
                future.cancel()
                return
            if not future.set_running_or_notify_cancel():
                return
            with self._lock:
                if self._epoch != epoch:
                    superseded = True
                else:
                    self._active_task_id = task_id
                    superseded = False
            if superseded:
                future.set_result(None)
                return
            try:
                future.set_result(self._invoke(text, task_id, intent))
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                with self._lock:
                    if self._active_task_id == task_id:
                        self._active_task_id = None
        finally:
            self._queue.task_done()

    def _invoke(self, text: str, task_id: str, intent: Any) -> R:
        try:
            parameters = inspect.signature(self._run_text).parameters
        except (TypeError, ValueError):
            parameters = {}
        kwargs: dict[str, Any] = {}
        if "task_id" in parameters:
            kwargs["task_id"] = task_id
        if intent is not None and "intent" in parameters:
            kwargs["intent"] = intent
        if kwargs:
            return self._run_text(text, **kwargs)
        return self._run_text(text)

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
            self._epoch += 1
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
