"""Local motion loops for Go2.

SportClient.Move is a velocity command. One accepted call keeps the body
moving for about a second, and the wrapped command rejects durations above
two seconds. Longer travel is a local loop: this module counts the two-second
cycles, and the driver repeats Move inside each cycle. Distance travel stops
from fused position readings, not from the cycle count.
"""

from __future__ import annotations

from math import hypot, isfinite, pi


NATIVE_MOVE_MS = 2000
MOVE_RESEND_MS = 500
TURN_SPEED_RAD_S = 0.8
TURN_TOLERANCE_RAD = 0.08
BACK_UP_SPEED_MPS = 0.5
STRAFE_SPEED_MPS = 0.3


def move_cycle_count(duration_ms: int, slice_ms: int = NATIVE_MOVE_MS) -> int:
    """How many native Move windows cover duration_ms."""

    if duration_ms <= 0 or slice_ms <= 0:
        return 0
    return (duration_ms + slice_ms - 1) // slice_ms


def normalize_angle(angle: float) -> float:
    """Normalize an angle difference to [-pi, pi)."""

    return (angle + pi) % (2 * pi) - pi


class YawTurnTracker:
    """Accumulate signed yaw deltas while handling the IMU +/-pi boundary."""

    def __init__(self, initial_yaw: float, target_radians: float, tolerance_rad: float = TURN_TOLERANCE_RAD) -> None:
        if not isfinite(initial_yaw) or not isfinite(target_radians) or not isfinite(tolerance_rad):
            raise ValueError("Turn angles must be finite")
        self.initial_yaw = float(initial_yaw)
        self.previous_yaw = float(initial_yaw)
        self.target_radians = float(target_radians)
        self.tolerance_rad = float(tolerance_rad)
        self.delta_radians = 0.0

    def observe(self, yaw: float) -> float:
        if not isfinite(yaw):
            raise ValueError("IMU yaw must be finite")
        self.delta_radians += normalize_angle(float(yaw) - self.previous_yaw)
        self.previous_yaw = float(yaw)
        return self.delta_radians

    @property
    def reached(self) -> bool:
        if self.target_radians >= 0:
            return self.delta_radians >= self.target_radians - self.tolerance_rad
        return self.delta_radians <= self.target_radians + self.tolerance_rad


def horizontal(position: object) -> tuple[float, float] | None:
    if not isinstance(position, (list, tuple)) or len(position) < 2:
        return None
    x, y = position[0], position[1]
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    if not isfinite(x) or not isfinite(y):
        return None
    return float(x), float(y)


def fuse_displacements(readings: dict[str, float], target_m: float) -> tuple[float | None, str]:
    """Combine horizontal displacements already converted from sensor positions.

    A source that stays near the origin while another has moved is treated as
    stuck and left out. Two live sources must agree before the distance counts
    as reached. The returned distance is in the same unit as the readings;
    callers treat sport position and lidar odometry as meters.
    """

    if not readings:
        return None, "none"
    peak = max(readings.values())
    active = readings
    if len(readings) > 1 and peak >= 0.3:
        moving = {key: value for key, value in readings.items() if value >= 0.05}
        if moving and len(moving) < len(readings):
            active = moving
    values = list(active.values())
    if len(values) == 1:
        traveled = values[0]
        return traveled, "reached" if traveled + 1e-9 >= target_m else "tracking"
    low, high = min(values), max(values)
    if high - low > max(0.4, 0.25 * max(high, target_m)):
        return None, "disagree"
    traveled = (low + high) / 2.0
    return traveled, "reached" if traveled + 1e-9 >= target_m else "tracking"


class DistanceTracker:
    """Remember the pose at the start and report when the target displacement is met."""

    def __init__(self, target_m: float) -> None:
        self.target_m = float(target_m)
        self.origins: dict[str, tuple[float, float]] = {}
        self.readings: dict[str, float] = {}
        self.traveled_m: float | None = None
        self.status = "waiting"
        self._locked = False

    def observe(self, samples: dict[str, tuple[float, float]]) -> str | None:
        if not self._locked:
            self.origins = dict(samples)
            self._locked = True
            if not self.origins:
                self.status = "no_position"
                return "no_position"
            self.status = "tracking"
            return None
        readings: dict[str, float] = {}
        for key, origin in self.origins.items():
            point = samples.get(key)
            if point is None:
                continue
            readings[key] = hypot(point[0] - origin[0], point[1] - origin[1])
        self.readings = readings
        traveled, status = fuse_displacements(readings, self.target_m)
        self.traveled_m = traveled
        self.status = status
        if status == "reached":
            return "done"
        return None
