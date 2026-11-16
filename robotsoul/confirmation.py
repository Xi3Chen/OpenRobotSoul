"""Fail-closed voice and gesture confirmation for confirm-tier actions."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from time import monotonic
from typing import Any, Callable
import re


class ConfirmationStatus(StrEnum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


class ConfirmationDecision(StrEnum):
    CONFIRM = "confirm"
    CANCEL = "cancel"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ConfirmationRequest:
    confirmation_id: str
    command_digest: str
    skill: str
    prompt: str
    irreversible: bool = False
    wake_phrase: str = ""
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.confirmation_id or not self.command_digest or not self.skill:
            raise ValueError("confirmation identity fields are required")
        if not self.prompt.strip():
            raise ValueError("confirmation prompt must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("confirmation timeout must be positive")


@dataclass(frozen=True)
class ConfirmationResult:
    status: ConfirmationStatus
    decision: ConfirmationDecision | None
    reason: str
    confirmation_id: str
    channel: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == ConfirmationStatus.CONFIRMED


@dataclass(frozen=True)
class GestureSample:
    gesture: str
    held_seconds: float
    facing_camera: bool
    distance_m: float
    source: str = "camera"


class ConfirmationManager:
    """One-shot confirmation window with strict local input rules.

    Audio playback is muted while the prompt is spoken and for a short guard
    period afterwards.  Only the first local-microphone answer is considered;
    ambiguous speech or a second speaker closes the window without executing.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        prompt_callback: Callable[[str], Any] | None = None,
        cue_callback: Callable[[str], Any] | None = None,
        stop_callback: Callable[[str], Any] | None = None,
        mute_guard_seconds: float = 0.2,
        gesture_min_hold_seconds: float = 0.5,
        gesture_min_distance_m: float = 0.15,
        gesture_max_distance_m: float = 5.0,
    ) -> None:
        if mute_guard_seconds < 0 or gesture_min_hold_seconds <= 0:
            raise ValueError("confirmation timing values are invalid")
        if not 0 < gesture_min_distance_m < gesture_max_distance_m:
            raise ValueError("gesture distance bounds are invalid")
        self._clock = clock
        self._prompt_callback = prompt_callback
        self._cue_callback = cue_callback
        self._stop_callback = stop_callback
        self.mute_guard_seconds = float(mute_guard_seconds)
        self.gesture_min_hold_seconds = float(gesture_min_hold_seconds)
        self.gesture_min_distance_m = float(gesture_min_distance_m)
        self.gesture_max_distance_m = float(gesture_max_distance_m)
        self._lock = RLock()
        self._request: ConfirmationRequest | None = None
        self._opened_at = 0.0
        self._expires_at = 0.0
        self._muted_until = 0.0
        self._first_input_seen = False
        self._voice_confirmed = False
        self._result: ConfirmationResult | None = None
        self._metrics = Counter()

    def open(self, request: ConfirmationRequest, *, now: float | None = None) -> ConfirmationRequest:
        current = self._clock() if now is None else float(now)
        with self._lock:
            if self._request is not None and self._result is None:
                raise RuntimeError("a confirmation window is already open")
            self._request = request
            self._opened_at = current
            self._expires_at = current + request.timeout_seconds
            self._muted_until = current + self.mute_guard_seconds
            self._first_input_seen = False
            self._voice_confirmed = False
            self._result = None
            self._metrics["opened"] += 1
        if self._prompt_callback is not None:
            self._prompt_callback(request.prompt)
        if self._cue_callback is not None:
            self._cue_callback("confirmation_open")
        return request

    begin = open

    @property
    def active(self) -> bool:
        with self._lock:
            return self._request is not None and self._result is None

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._request is not None and self._clock() < self._muted_until

    def remaining_seconds(self, *, now: float | None = None) -> float:
        current = self._clock() if now is None else float(now)
        with self._lock:
            return max(0.0, self._expires_at - current) if self.active else 0.0

    def accept_voice(
        self,
        transcript: str,
        *,
        source: str = "local",
        direction_ok: bool = True,
        speaker_count: int = 1,
        now: float | None = None,
    ) -> ConfirmationResult:
        current = self._clock() if now is None else float(now)
        with self._lock:
            expired = self._expire_locked(current)
            request = self._request
            if expired:
                return expired
            if request is None or self._result is not None:
                return self._rejected_locked("no confirmation window is open", "no_window")
            if current < self._muted_until:
                self._metrics["muted_input"] += 1
                return self._rejected_locked("confirmation input arrived during the speech guard period", "muted")
            if self._first_input_seen:
                return self._rejected_locked("only the first confirmation sentence is accepted", "second_input")
            self._first_input_seen = True
            if source != "local":
                self._metrics["wrong_source"] += 1
                return self._reject_first_locked("confirmation must come from the local microphone", "wrong_source")
            if speaker_count != 1 or not direction_ok:
                self._metrics["ambiguous"] += 1
                return self._reject_first_locked("confirmation speech was ambiguous or outside the direction gate", "ambiguous")
            decision = _parse_voice(transcript, request)
            if decision is None:
                self._metrics["ambiguous"] += 1
                return self._reject_first_locked("confirmation speech was not an exact confirm/cancel phrase", "ambiguous")
            if decision == ConfirmationDecision.CANCEL:
                self._metrics["cancelled"] += 1
                return self._finish_locked(ConfirmationStatus.CANCELLED, decision, "用户取消了动作", "voice")
            self._voice_confirmed = True
            self._metrics["voice_confirmed"] += 1
            if request.irreversible and not _has_wake_prefix(transcript, request.wake_phrase):
                return self._reject_first_locked("不可逆动作需要唤醒词前缀", "wake_prefix_required")
            if request.irreversible:
                # An irreversible action still requires a gesture in the same
                # window; a voice answer alone never authorizes it.
                self._metrics["waiting_for_gesture"] += 1
                return ConfirmationResult(ConfirmationStatus.OPEN, decision, "等待不可逆动作的有效手势", request.confirmation_id, "voice")
            self._metrics["confirmed"] += 1
            return self._finish_locked(ConfirmationStatus.CONFIRMED, decision, "已收到语音确认", "voice")

    confirm_voice = accept_voice

    def accept_gesture(
        self,
        sample: GestureSample,
        *,
        now: float | None = None,
    ) -> ConfirmationResult:
        current = self._clock() if now is None else float(now)
        with self._lock:
            request = self._request
            if sample.gesture.strip().lower() in {"palm_up", "palm-up", "stop", "停止", "掌心上推"}:
                self._metrics["global_stops"] += 1
                callback = self._stop_callback
                if callback is not None:
                    callback("gesture_global_stop")
                if request is not None and self._result is None:
                    self._result = ConfirmationResult(ConfirmationStatus.CANCELLED, ConfirmationDecision.CANCEL, "手势触发全局停止", request.confirmation_id, "gesture")
                return ConfirmationResult(ConfirmationStatus.CANCELLED, ConfirmationDecision.CANCEL, "手势触发全局停止", request.confirmation_id if request else "", "gesture")
            expired = self._expire_locked(current)
            if expired:
                return expired
            if request is None or self._result is not None:
                return self._rejected_locked("no confirmation window is open", "no_window")
            if self._first_input_seen and not self._voice_confirmed:
                return self._rejected_locked("only the first confirmation input is accepted", "second_input")
            if sample.source != "camera" or sample.gesture.strip().lower() not in {"ok", "确认", "thumbs_up"}:
                return self._reject_first_locked("gesture is not a confirmation gesture", "invalid_gesture")
            if sample.held_seconds < self.gesture_min_hold_seconds or not sample.facing_camera:
                return self._reject_first_locked("gesture was too brief or not facing the camera", "gesture_gate")
            if not self.gesture_min_distance_m <= sample.distance_m <= self.gesture_max_distance_m:
                return self._reject_first_locked("gesture distance is outside the valid range", "gesture_distance")
            self._first_input_seen = True
            if request.irreversible and not self._voice_confirmed:
                return self._reject_first_locked("不可逆动作需要语音和手势同时确认", "voice_required")
            self._metrics["confirmed"] += 1
            return self._finish_locked(ConfirmationStatus.CONFIRMED, ConfirmationDecision.CONFIRM, "已收到手势确认", "gesture")

    confirm_gesture = accept_gesture

    def tick(self, *, now: float | None = None) -> ConfirmationResult | None:
        current = self._clock() if now is None else float(now)
        with self._lock:
            return self._expire_locked(current)

    def result(self) -> ConfirmationResult | None:
        with self._lock:
            return self._result

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._request is not None and self._result is None,
                "confirmation_id": self._request.confirmation_id if self._request else None,
                "skill": self._request.skill if self._request else None,
                "irreversible": self._request.irreversible if self._request else False,
                "remaining_seconds": max(0.0, self._expires_at - self._clock()) if self._request and self._result is None else 0.0,
                "muted": self._request is not None and self._clock() < self._muted_until,
                "result": self._result.status.value if self._result else None,
                "metrics": dict(self._metrics),
            }

    snapshot = status

    def _expire_locked(self, current: float) -> ConfirmationResult | None:
        request = self._request
        if request is None or self._result is not None or current < self._expires_at:
            return None
        self._metrics["expired"] += 1
        return self._finish_locked(ConfirmationStatus.EXPIRED, None, "没有在 10 秒内收到确认，动作已放弃", None)

    def _finish_locked(
        self,
        status: ConfirmationStatus,
        decision: ConfirmationDecision | None,
        reason: str,
        channel: str | None,
    ) -> ConfirmationResult:
        request = self._request
        result = ConfirmationResult(status, decision, reason, request.confirmation_id if request else "", channel)
        self._result = result
        return result

    def _reject_first_locked(self, reason: str, metric: str) -> ConfirmationResult:
        self._metrics[metric] += 1
        return self._finish_locked(ConfirmationStatus.REJECTED, ConfirmationDecision.AMBIGUOUS, reason, None)

    def _rejected_locked(self, reason: str, metric: str) -> ConfirmationResult:
        self._metrics[metric] += 1
        request = self._request
        return ConfirmationResult(ConfirmationStatus.REJECTED, ConfirmationDecision.AMBIGUOUS, reason, request.confirmation_id if request else "", None)


def _parse_voice(transcript: str, request: ConfirmationRequest) -> ConfirmationDecision | None:
    if not isinstance(transcript, str):
        return None
    normalized = re.sub(r"[\s，。！？,.!?]", "", transcript.lower())
    if normalized in {"取消", "cancel"}:
        return ConfirmationDecision.CANCEL
    if normalized in {"确认", "confirm"}:
        return ConfirmationDecision.CONFIRM
    if request.irreversible and _has_wake_prefix(normalized, request.wake_phrase):
        suffix = normalized[len(re.sub(r"\s+", "", request.wake_phrase.lower())) :]
        if suffix in {"确认", "confirm"}:
            return ConfirmationDecision.CONFIRM
    return None


def _has_wake_prefix(transcript: str, wake_phrase: str) -> bool:
    if not wake_phrase:
        return False
    normalized = re.sub(r"[\s，。！？,.!?]", "", transcript.lower())
    wake = re.sub(r"\s+", "", wake_phrase.lower())
    return normalized.startswith(wake) and normalized[len(wake) :] in {"确认", "confirm"}


# Public aliases make the protocol easy to discover from either terminology.
VoiceConfirmationManager = ConfirmationManager
GestureConfirmation = GestureSample
