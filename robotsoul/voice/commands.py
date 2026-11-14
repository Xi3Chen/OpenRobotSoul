"""Closed command phrases that skip the language gateway.

The matcher runs on a finished transcript. It only returns a skill the
robot has already registered, and it clamps numeric arguments to that
skill's declared range.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from robotsoul.models import AgentIntent

_STAND_UP = re.compile(r"^(?:请)?(?:你)?(?:站起来|起立|站立)$")
_STAND_DOWN = re.compile(r"^(?:请)?(?:你)?(?:趴下|卧倒)$")
_MOVE = re.compile(
    r"^(?:请)?(?:你)?(?:向|往)?(?P<direction>前|后)(?:边|面)?(?:走|移动)"
    r"(?P<count>[0-9]+|[一二两三四五六七八九十])?(?:步|秒|下)?$"
)
_COUNTS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_WALK_SPEED = 0.3


def match_closed_command(
    text: str,
    capabilities: Mapping[str, Any],
    *,
    wake_phrase: str | Sequence[str] = "",
) -> AgentIntent | None:
    phrase = _normalize(text, wake_phrase)
    if not phrase or not isinstance(capabilities, Mapping):
        return None
    if _STAND_UP.fullmatch(phrase) and "sport.stand_up" in capabilities:
        return AgentIntent("sport.stand_up", {}, "闭集：站起来")
    if _STAND_DOWN.fullmatch(phrase) and "sport.stand_down" in capabilities:
        return AgentIntent("sport.stand_down", {}, "闭集：趴下")
    move = _MOVE.fullmatch(phrase)
    capability = capabilities.get("sport.move") if move else None
    if move is None or capability is None:
        return None
    count = _count(move.group("count"))
    duration = _clamp(1000 if count is None else count * 1000, _limit(capability, "duration_ms", 200, 2000))
    speed = _WALK_SPEED if move.group("direction") == "前" else -_WALK_SPEED
    low, high = _limit(capability, "vx", -2.5, 3.8)
    return AgentIntent(
        "sport.move",
        {
            "vx": _clamp(speed, (low, high)),
            "vy": 0,
            "vyaw": 0,
            "duration_ms": duration,
        },
        "闭集：移动",
    )


def _normalize(text: str, wake_phrase: str | Sequence[str]) -> str:
    cleaned = "".join(str(text).split())
    cleaned = cleaned.strip("，。！？,.!?、 ")
    if isinstance(wake_phrase, str):
        raw_phrases = [part.strip() for part in wake_phrase.split(",") if part.strip()]
    else:
        raw_phrases = [phrase.strip() for phrase in wake_phrase if isinstance(phrase, str) and phrase.strip()]
    for phrase in sorted(("".join(item.split()) for item in raw_phrases), key=len, reverse=True):
        if phrase and cleaned.startswith(phrase):
            cleaned = cleaned[len(phrase) :].lstrip("，。！？,.!?、 ")
    return cleaned


def _count(value: str | None) -> int | None:
    if not value:
        return None
    if value in _COUNTS:
        return _COUNTS[value]
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _limit(capability: Any, name: str, low: float, high: float) -> tuple[float, float]:
    parameters = getattr(capability, "parameters", None)
    spec = parameters.get(name) if isinstance(parameters, Mapping) else None
    if spec is None:
        return low, high
    minimum = getattr(spec, "minimum", None)
    maximum = getattr(spec, "maximum", None)
    if isinstance(spec, Mapping):
        minimum = spec.get("minimum", spec.get("min", minimum))
        maximum = spec.get("maximum", spec.get("max", maximum))
    if isinstance(minimum, (int, float)):
        low = float(minimum)
    if isinstance(maximum, (int, float)):
        high = float(maximum)
    return low, high


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    if low > high:
        low, high = high, low
    clamped = min(max(value, low), high)
    if float(clamped).is_integer():
        return int(clamped)
    return clamped
