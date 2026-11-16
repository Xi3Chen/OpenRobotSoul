"""Deterministic local safety gates and directional evidence.

The language process may propose a risk decision, but this module owns the
last check immediately before hardware dispatch.  It is intentionally small
and dependency free so it can also run when the cloud process is unavailable.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import cos, isfinite, radians, sin
from threading import RLock
from time import monotonic
from typing import Any, Callable, Iterable

from robotsoul.models import (
    Command,
    EvidenceRequirement,
    ExecutionCondition,
    Feedback,
    FeedbackStatus,
    utc_now,
)


_DIRECTION_ANGLES = {
    "front": 0.0,
    "forward": 0.0,
    "前": 0.0,
    "前方": 0.0,
    "right": -90.0,
    "右": -90.0,
    "右侧": -90.0,
    "back": 180.0,
    "rear": 180.0,
    "后": 180.0,
    "后方": 180.0,
    "left": 90.0,
    "左": 90.0,
    "左侧": 90.0,
}


def _direction_angle(direction: str) -> float:
    normalized = "".join(direction.lower().split())
    if normalized in _DIRECTION_ANGLES:
        return _DIRECTION_ANGLES[normalized]
    try:
        value = float(normalized.rstrip("°"))
    except (TypeError, ValueError):
        raise ValueError(f"unsupported evidence direction: {direction}") from None
    if not isfinite(value):
        raise ValueError("evidence direction angle must be finite")
    return value % 360.0


def _angle_delta(left: float, right: float) -> float:
    return abs((left - right + 180.0) % 360.0 - 180.0)


@dataclass(frozen=True)
class DirectionalEvidence:
    direction: str
    distance_m: float
    quantity: int
    observed_at: datetime
    source: str
    negative: bool = False
    confidence: float = 1.0
    heading_degrees: float = 0.0
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.distance_m, bool) or not isinstance(self.distance_m, (int, float)) or not isfinite(float(self.distance_m)) or self.distance_m <= 0:
            raise ValueError("evidence distance_m must be a positive finite number")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity < 0:
            raise ValueError("evidence quantity must be a non-negative integer")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("evidence observed_at must include a timezone")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("evidence source must be non-empty text")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) or not 0 <= self.confidence <= 1:
            raise ValueError("evidence confidence must be between 0 and 1")
        if self.valid_until is not None and (self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None):
            raise ValueError("evidence valid_until must include a timezone")

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "distance_m": float(self.distance_m),
            "quantity": self.quantity,
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
            "negative": self.negative,
            "confidence": float(self.confidence),
            "heading_degrees": float(self.heading_degrees),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
        }


@dataclass(frozen=True)
class EvidenceCheck:
    met: bool
    reason: str
    requirement: EvidenceRequirement
    evidence: DirectionalEvidence | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "met": self.met,
            "reason": self.reason,
            "requirement": self.requirement.to_dict(),
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


class DirectionalEvidenceTable:
    """Fresh directional evidence that rotates with the robot heading.

    Sensor evidence that reports an occupant/obstacle is contradictory and
    cannot be overwritten by a later human statement.  Human statements can
    fill an unmeasured direction and expire on their own.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._lock = RLock()
        self._heading_degrees = 0.0
        self._records: list[DirectionalEvidence] = []

    @property
    def heading_degrees(self) -> float:
        with self._lock:
            return self._heading_degrees

    def rotate(self, delta_degrees: float) -> None:
        if isinstance(delta_degrees, bool) or not isinstance(delta_degrees, (int, float)) or not isfinite(float(delta_degrees)):
            raise ValueError("heading delta must be finite")
        with self._lock:
            self._heading_degrees = (self._heading_degrees + float(delta_degrees)) % 360.0

    def update(
        self,
        direction: str,
        distance_m: float,
        *,
        quantity: int = 0,
        source: str = "sensor",
        observed_at: datetime | None = None,
        negative: bool = False,
        confidence: float = 1.0,
        valid_for_seconds: float | None = None,
    ) -> DirectionalEvidence:
        if valid_for_seconds is not None and (
            isinstance(valid_for_seconds, bool)
            or not isinstance(valid_for_seconds, (int, float))
            or not isfinite(float(valid_for_seconds))
            or valid_for_seconds <= 0
        ):
            raise ValueError("evidence valid_for_seconds must be positive and finite")
        record = DirectionalEvidence(
            direction=direction,
            distance_m=distance_m,
            quantity=quantity,
            observed_at=observed_at or utc_now(),
            source=source,
            negative=negative,
            confidence=confidence,
            heading_degrees=self.heading_degrees,
            valid_until=(observed_at or utc_now()) + timedelta(seconds=float(valid_for_seconds)) if valid_for_seconds is not None else None,
        )
        with self._lock:
            self._records.append(record)
            self._prune_locked(record.observed_at)
        return record

    add = update

    def human_statement(
        self,
        direction: str,
        distance_m: float,
        *,
        quantity: int = 0,
        valid_for_seconds: float = 10.0,
        observed_at: datetime | None = None,
    ) -> DirectionalEvidence:
        # The validity is carried by the requirement at check time.  Keeping
        # this as an explicit source prevents it from masking sensor evidence.
        if valid_for_seconds <= 0:
            raise ValueError("human evidence validity must be positive")
        return self.update(
            direction,
            distance_m,
            quantity=quantity,
            source="human",
            observed_at=observed_at,
            negative=True,
            valid_for_seconds=valid_for_seconds,
        )

    add_human_statement = human_statement

    def check(self, requirement: EvidenceRequirement, *, now: datetime | None = None) -> EvidenceCheck:
        if not isinstance(requirement, EvidenceRequirement):
            raise TypeError("requirement must be an EvidenceRequirement")
        current = now or utc_now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("evidence check time must include a timezone")
        wanted_angle = (self.heading_degrees + _direction_angle(requirement.direction)) % 360.0
        with self._lock:
            self._prune_locked(current)
            candidates = [
                item for item in self._records
                if _angle_delta((item.heading_degrees + _direction_angle(item.direction)) % 360.0, wanted_angle) <= 35.0
                and (current - item.observed_at).total_seconds() <= requirement.valid_for_seconds
                and (item.valid_until is None or current <= item.valid_until)
            ]
            if not candidates:
                return EvidenceCheck(False, "required directional evidence is missing or expired", requirement)
            contradictory = [item for item in candidates if not item.negative and item.quantity > requirement.quantity and item.distance_m <= requirement.range_m]
            if contradictory:
                newest = max(contradictory, key=lambda item: item.observed_at)
                return EvidenceCheck(False, "fresh contradictory evidence is present", requirement, newest)
            valid = [item for item in candidates if item.negative and item.distance_m >= requirement.range_m and item.quantity <= requirement.quantity]
            if not valid:
                return EvidenceCheck(False, "directional evidence does not meet the required range or quantity", requirement, max(candidates, key=lambda item: item.observed_at))
            return EvidenceCheck(True, "directional evidence is fresh and sufficient", requirement, max(valid, key=lambda item: item.observed_at))

    check_requirement = check

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._prune_locked(utc_now())
            return {
                "heading_degrees": self._heading_degrees,
                "records": [item.to_dict() for item in self._records],
            }

    def _prune_locked(self, now: datetime) -> None:
        # Keep a bounded operational history; requirements perform the exact
        # age check because each review can choose a different validity window.
        cutoff = now - timedelta(minutes=10)
        self._records = [item for item in self._records if item.observed_at >= cutoff]


@dataclass(frozen=True)
class SafetyCheck:
    allowed: bool
    reason: str = ""
    error_code: str | None = None
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "error_code": self.error_code,
            "evidence": self.evidence or {},
        }


class SafetyReviewMetrics:
    """Small bounded counter set exposed to the Dashboard."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._counts = Counter()
        self._reasons: list[dict[str, Any]] = []

    def record_review(self, verdict: Any, reason: str = "") -> None:
        value = getattr(verdict, "value", verdict)
        value = str(value or "deny")
        with self._lock:
            self._counts[f"review_{value}"] += 1
            if reason:
                self._reasons.append({"kind": "review", "verdict": value, "reason": reason})
                self._reasons = self._reasons[-100:]

    def record_condition_failure(self, reason: str, error_code: str | None = None) -> None:
        with self._lock:
            self._counts["condition_failures"] += 1
            self._reasons.append({"kind": "condition", "error_code": error_code, "reason": reason})
            self._reasons = self._reasons[-100:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"counts": dict(self._counts), "recent_reasons": list(self._reasons)}

    metrics = snapshot


class LocalSafetySupervisor:
    """Fail-closed speed, zone, watchdog, e-stop and condition gate."""

    def __init__(
        self,
        robot_id: str,
        *,
        max_speed_mps: float = 0.8,
        watchdog_timeout_seconds: float = 2.0,
        zone: dict[str, float] | None = None,
        evidence_table: DirectionalEvidenceTable | None = None,
        clock: Callable[[], float] = monotonic,
        stop_callback: Callable[[str], Any] | None = None,
    ) -> None:
        if not isinstance(robot_id, str) or not robot_id:
            raise ValueError("robot_id is required")
        if max_speed_mps <= 0 or watchdog_timeout_seconds <= 0:
            raise ValueError("safety limits must be positive")
        self.robot_id = robot_id
        self.max_speed_mps = float(max_speed_mps)
        self.watchdog_timeout_seconds = float(watchdog_timeout_seconds)
        self.zone = dict(zone or {})
        self.evidence = evidence_table or DirectionalEvidenceTable()
        self._clock = clock
        self._stop_callback = stop_callback
        self._lock = RLock()
        self._connected = False
        self._last_heartbeat = 0.0
        self._emergency_stop = False
        self._estop_reason = ""
        self._position = (0.0, 0.0, 0.0)
        self._yaw_degrees = 0.0
        self._battery_percent: int | None = None
        self._conditions: dict[str, tuple[ExecutionCondition, ...]] = {}
        self._metrics = Counter()

    def set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = bool(connected)
            if not connected:
                self._metrics["disconnects"] += 1

    def heartbeat(self, *, now: float | None = None) -> None:
        with self._lock:
            self._connected = True
            self._last_heartbeat = self._clock() if now is None else float(now)

    def update_state(self, state: dict[str, Any], *, observed_at: datetime | None = None) -> None:
        if not isinstance(state, dict):
            raise ValueError("safety state must be an object")
        position = state.get("position")
        if isinstance(position, (list, tuple)) and len(position) >= 2 and all(isinstance(item, (int, float)) and not isinstance(item, bool) and isfinite(float(item)) for item in position[:2]):
            z = position[2] if len(position) > 2 and isinstance(position[2], (int, float)) and not isinstance(position[2], bool) else 0.0
            with self._lock:
                self._position = (float(position[0]), float(position[1]), float(z))
        velocity = state.get("velocity")
        if isinstance(velocity, (list, tuple)) and len(velocity) >= 2:
            speed = (float(velocity[0]), float(velocity[1]))
            if all(isfinite(item) for item in speed):
                with self._lock:
                    self._metrics["state_updates"] += 1
        yaw = state.get("yaw_degrees")
        if isinstance(yaw, (int, float)) and not isinstance(yaw, bool) and isfinite(float(yaw)):
            with self._lock:
                self.evidence.rotate(float(yaw) - self._yaw_degrees)
                self._yaw_degrees = float(yaw)
        battery = state.get("battery_percent", state.get("soc"))
        if isinstance(battery, int) and not isinstance(battery, bool) and 0 <= battery <= 100:
            with self._lock:
                self._battery_percent = battery
        heartbeat_at = self._clock()
        if observed_at is not None and observed_at.tzinfo is not None and observed_at.utcoffset() is not None:
            age = max(0.0, (utc_now() - observed_at).total_seconds())
            heartbeat_at -= age
        self.heartbeat(now=heartbeat_at)

    def bind_conditions(self, command_id: str, conditions: Iterable[ExecutionCondition] = ()) -> None:
        parsed = tuple(condition for condition in conditions if isinstance(condition, ExecutionCondition))
        with self._lock:
            self._conditions[command_id] = parsed

    def clear_conditions(self, command_id: str) -> None:
        with self._lock:
            self._conditions.pop(command_id, None)

    def trigger_emergency_stop(self, reason: str = "local emergency stop") -> SafetyCheck:
        with self._lock:
            self._emergency_stop = True
            self._estop_reason = reason.strip() or "local emergency stop"
            self._metrics["emergency_stops"] += 1
        callback = self._stop_callback
        if callback is not None:
            try:
                callback(self._estop_reason)
            except Exception:
                with self._lock:
                    self._metrics["stop_failures"] += 1
                return SafetyCheck(False, "emergency stop was requested but hardware stop was not confirmed", "stop_failed")
        return SafetyCheck(False, self._estop_reason, "emergency_stop")

    emergency_stop = trigger_emergency_stop

    def clear_emergency_stop(self, *, operator_authenticated: bool = False) -> SafetyCheck:
        if not operator_authenticated:
            return SafetyCheck(False, "clearing emergency stop requires an authenticated operator", "operator_required")
        with self._lock:
            self._emergency_stop = False
            self._estop_reason = ""
            self._metrics["emergency_stop_clears"] += 1
        return SafetyCheck(True, "emergency stop cleared")

    def check_command(self, command: Command, *, phase: str = "before") -> SafetyCheck:
        with self._lock:
            if command.robot_id != self.robot_id:
                return SafetyCheck(False, "robot identity does not match the safety supervisor", "robot_mismatch")
            if self._emergency_stop:
                self._metrics["rejected_emergency_stop"] += 1
                return SafetyCheck(False, self._estop_reason or "emergency stop is active", "emergency_stop")
            if not self._connected:
                self._metrics["rejected_disconnect"] += 1
                return SafetyCheck(False, "robot connection is not healthy", "disconnected")
            if self._clock() - self._last_heartbeat > self.watchdog_timeout_seconds:
                self._metrics["watchdog_expirations"] += 1
                return SafetyCheck(False, "robot state watchdog expired", "watchdog_expired")
            parameters = command.parameters
            vx = float(parameters.get("vx", 0.0)) if isinstance(parameters.get("vx", 0.0), (int, float)) else 0.0
            vy = float(parameters.get("vy", 0.0)) if isinstance(parameters.get("vy", 0.0), (int, float)) else 0.0
            speed = (vx * vx + vy * vy) ** 0.5
            requested = parameters.get("max_speed_mps")
            if isinstance(requested, (int, float)) and not isinstance(requested, bool):
                speed = max(speed, abs(float(requested)))
            if speed > self.max_speed_mps + 1e-9:
                self._metrics["rejected_speed"] += 1
                return SafetyCheck(False, f"requested speed exceeds local limit {self.max_speed_mps:g} m/s", "speed_limit", {"speed_mps": speed, "max_speed_mps": self.max_speed_mps})
            predicted = self._predicted_position(parameters, speed)
            if not self._inside_zone(predicted):
                self._metrics["rejected_zone"] += 1
                return SafetyCheck(False, "predicted motion leaves the configured safety zone", "zone_limit", {"predicted_position": predicted, "zone": dict(self.zone)})
            conditions = self._conditions.get(command.command_id, ())
            for condition in conditions:
                check = self._check_condition(condition, parameters)
                if not check.allowed:
                    self._metrics["condition_failures"] += 1
                    return check
            self._metrics[f"checks_{phase}"] += 1
            return SafetyCheck(True, "local safety conditions satisfied", evidence={"phase": phase, "predicted_position": predicted})

    check = check_command

    def after_feedback(self, command: Command, feedback: Iterable[Feedback]) -> SafetyCheck:
        items = tuple(feedback)
        if any(item.status == FeedbackStatus.SUCCEEDED for item in items):
            return self.check_command(command, phase="after")
        return SafetyCheck(True, "no successful feedback requires a landing check")

    def status(self) -> dict[str, Any]:
        with self._lock:
            watchdog_expired = self._connected and self._clock() - self._last_heartbeat > self.watchdog_timeout_seconds
            return {
                "connected": self._connected,
                "emergency_stop": self._emergency_stop,
                "emergency_stop_reason": self._estop_reason,
                "watchdog_expired": watchdog_expired,
                "last_heartbeat_age_seconds": max(0.0, self._clock() - self._last_heartbeat) if self._last_heartbeat else None,
                "position": list(self._position),
                "yaw_degrees": self._yaw_degrees,
                "battery_percent": self._battery_percent,
                "max_speed_mps": self.max_speed_mps,
                "zone": dict(self.zone),
                "evidence": self.evidence.snapshot(),
                "metrics": dict(self._metrics),
            }

    snapshot = status

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)

    def _check_condition(self, condition: ExecutionCondition, parameters: dict[str, Any]) -> SafetyCheck:
        if condition.evidence is not None:
            check = self.evidence.check(condition.evidence)
            if not check.met:
                return SafetyCheck(False, f"execution condition {condition.name} failed: {check.reason}", "condition_failed", {"condition": condition.to_dict(), "check": check.to_dict()})
        if condition.minimum_battery_percent is not None:
            if self._battery_percent is None:
                return SafetyCheck(False, f"execution condition {condition.name} failed: battery evidence is missing", "condition_failed", {"condition": condition.to_dict()})
            if self._battery_percent < condition.minimum_battery_percent:
                return SafetyCheck(False, f"execution condition {condition.name} failed: battery is too low", "condition_failed", {"condition": condition.to_dict(), "battery_percent": self._battery_percent})
        if condition.max_speed_mps is not None:
            vx = parameters.get("vx", 0.0)
            vy = parameters.get("vy", 0.0)
            if isinstance(vx, (int, float)) and isinstance(vy, (int, float)):
                speed = (float(vx) * float(vx) + float(vy) * float(vy)) ** 0.5
                requested = parameters.get("max_speed_mps")
                if isinstance(requested, (int, float)) and not isinstance(requested, bool):
                    speed = max(speed, abs(float(requested)))
                if speed > condition.max_speed_mps:
                    return SafetyCheck(False, f"execution condition {condition.name} failed: speed is too high", "condition_failed", {"condition": condition.to_dict(), "speed_mps": speed})
        return SafetyCheck(True, f"execution condition {condition.name} satisfied")

    def _predicted_position(self, parameters: dict[str, Any], speed: float) -> tuple[float, float, float]:
        x, y, z = self._position
        duration = parameters.get("duration_ms", 0)
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            seconds = max(0.0, float(duration) / 1000.0)
            x += float(parameters.get("vx", 0.0)) * seconds
            y += float(parameters.get("vy", 0.0)) * seconds
        distance = parameters.get("distance_m")
        if isinstance(distance, (int, float)) and not isinstance(distance, bool):
            x += float(distance) * cos(radians(self._yaw_degrees))
            y += float(distance) * sin(radians(self._yaw_degrees))
        return (x, y, z)

    def _inside_zone(self, position: tuple[float, float, float]) -> bool:
        if not self.zone:
            return True
        bounds = self.zone
        return (
            ("min_x" not in bounds or position[0] >= bounds["min_x"])
            and ("max_x" not in bounds or position[0] <= bounds["max_x"])
            and ("min_y" not in bounds or position[1] >= bounds["min_y"])
            and ("max_y" not in bounds or position[1] <= bounds["max_y"])
        )


# Short aliases used by integrations and tests.
SafetySupervisor = LocalSafetySupervisor
DirectionalClearanceTable = DirectionalEvidenceTable
