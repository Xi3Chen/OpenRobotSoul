"""Collapse repeated move steps into the one local motion command that performs them."""

from __future__ import annotations

from typing import Any, Mapping


_MOTION_AMOUNT = {
    "sport.move": "duration_ms",
    "sport.move_distance": "distance_m",
}
_VELOCITY = ("vx", "vy", "vyaw")


def motion_amount_key(skill: str) -> str | None:
    return _MOTION_AMOUNT.get(skill)


def same_velocity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(name) == right.get(name) for name in _VELOCITY)


def combined_amount(left: Any, right: Any, key: str, cap: float | None) -> int | float:
    total = float(left) + float(right)
    if cap is not None and total > float(cap):
        total = float(cap)
    if key == "duration_ms":
        return int(total)
    return total
