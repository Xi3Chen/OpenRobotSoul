"""Closed command phrases that skip the language gateway.

The matcher runs on a finished transcript. It only returns a skill the
robot has already registered, and it clamps numeric arguments to that
skill's declared range.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from robotsoul.models import AgentIntent, AgentPlan

_STAND_UP = re.compile(r"^(?:请)?(?:你)?(?:站起来|起立|站立|起来)$")
_STAND_DOWN = re.compile(r"^(?:请)?(?:你)?(?:趴下|卧倒)$")
_TURN_AROUND = re.compile(
    r"^(?:请)?(?:你)?(?:向后转|往后转|掉头|转过身|转身|(?:向后|往后)?转(?:一)?(?:百)?八十度)$"
)
_TURN_LEFT = re.compile(r"^(?:请)?(?:你)?(?:向左(?:边|面)?转|往左(?:边|面)?转|左转)$")
_TURN_RIGHT = re.compile(r"^(?:请)?(?:你)?(?:向右(?:边|面)?转|往右(?:边|面)?转|右转)$")
_STAND_THEN_TURN_AROUND = re.compile(
    r"^(?:请)?(?:你)?(?:站起来|起立|站立|起来)"
    r"(?:然后|再|，|,|、)?"
    r"(?:向后转|往后转|掉头|转过身|转身|(?:向后|往后)?转(?:一)?(?:百)?八十度)$"
)
_NOD = re.compile(
    r"^(?:请)?(?:你)?点(?:(?P<count>[0-9]+|[一二两三四五]))?(?:下|个|次)?头$"
)
_SHAKE_HEAD = re.compile(
    r"^(?:请)?(?:你)?摇(?:(?P<count>[0-9]+|[一二两三四五]))?(?:下|个|次)?头$"
)
_TILT_HEAD = re.compile(
    r"^(?:请)?(?:你)?(?:向|往)?(?P<direction>左|右)?(?:边|面)?歪(?:一)?(?:下|个)?头$"
)
_BACK_UP = re.compile(
    r"^(?:请)?(?:你)?(?:后退|往后退|向后退|退后)"
    r"(?:(?P<count>[0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十])(?P<unit>米|[mM]|步))?"
    r"$"
)
_STRAFE = re.compile(
    r"^(?:请)?(?:你)?(?:向|往)(?P<direction>左|右)(?:边|面)?(?:侧移|平移|横移)"
    r"(?:(?P<count>[0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十])(?P<unit>米|[mM]|步))?"
    r"$"
)
_MOVE = re.compile(
    r"^(?:请)?(?:你)?(?:向|往)?(?P<direction>前|后)(?:边|面)?(?:走|移动)"
    r"(?:(?P<count>[0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十])(?P<unit>步|秒|分钟|分|米|[mM]))?"
    r"$"
)
_DEFAULT_TRANSLATE_M = 0.5
_STEP_M = 0.3
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
# Common ASR mishearings of configured wake phrases. Keyed by the configured text.
_WAKE_ASR_ALIASES = {
    "索拉": (
        "左拉", "所拉", "索啦", "锁拉", "梭拉", "索那",
        "佐拉", "卓拉", "苏拉", "沙拉", "索娜", "索喇",
    ),
}
_COMMAND_LEADERS = frozenset("站趴转点摇歪退向往请你起")


def match_closed_command(
    text: str,
    capabilities: Mapping[str, Any],
    *,
    wake_phrase: str | Sequence[str] = "",
) -> AgentIntent | AgentPlan | None:
    if not isinstance(capabilities, Mapping):
        return None
    for phrase in _phrase_candidates(text, wake_phrase):
        matched = _match_normalized_phrase(phrase, capabilities)
        if matched is not None:
            return matched
    return None


def _match_normalized_phrase(
    phrase: str,
    capabilities: Mapping[str, Any],
) -> AgentIntent | AgentPlan | None:
    if _STAND_THEN_TURN_AROUND.fullmatch(phrase):
        if "sport.stand_up" in capabilities and "motion.turn_around" in capabilities:
            return AgentPlan(
                (
                    AgentIntent("sport.stand_up", {}, "闭集：站起来"),
                    AgentIntent("motion.turn_around", {}, "闭集：向后转"),
                ),
                "闭集：站起来再向后转",
            )
    if _STAND_UP.fullmatch(phrase) and "sport.stand_up" in capabilities:
        return AgentIntent("sport.stand_up", {}, "闭集：站起来")
    if _STAND_DOWN.fullmatch(phrase) and "sport.stand_down" in capabilities:
        return AgentIntent("sport.stand_down", {}, "闭集：趴下")
    if _TURN_AROUND.fullmatch(phrase) and "motion.turn_around" in capabilities:
        return AgentIntent("motion.turn_around", {}, "闭集：向后转")
    if _TURN_LEFT.fullmatch(phrase) and "motion.turn_left" in capabilities:
        return AgentIntent("motion.turn_left", {}, "闭集：向左转")
    if _TURN_RIGHT.fullmatch(phrase) and "motion.turn_right" in capabilities:
        return AgentIntent("motion.turn_right", {}, "闭集：向右转")
    nod = _NOD.fullmatch(phrase)
    if nod is not None and "expression.nod" in capabilities:
        count = _count(nod.group("count")) or 1
        return AgentIntent(
            "expression.nod",
            {"count": _clamp(count, _limit(capabilities["expression.nod"], "count", 1, 5))},
            "闭集：点头",
        )
    shake = _SHAKE_HEAD.fullmatch(phrase)
    if shake is not None and "expression.shake_head" in capabilities:
        count = _count(shake.group("count")) or 1
        return AgentIntent(
            "expression.shake_head",
            {"count": _clamp(count, _limit(capabilities["expression.shake_head"], "count", 1, 5))},
            "闭集：摇头",
        )
    tilt = _TILT_HEAD.fullmatch(phrase)
    if tilt is not None and "expression.tilt_head" in capabilities:
        direction = "right" if tilt.group("direction") == "右" else "left"
        return AgentIntent("expression.tilt_head", {"direction": direction}, "闭集：歪头")
    back = _BACK_UP.fullmatch(phrase)
    if back is not None and "motion.back_up" in capabilities:
        distance = _translate_distance_m(back.group("count"), back.group("unit"))
        return AgentIntent(
            "motion.back_up",
            {"distance_m": _clamp(distance, _limit(capabilities["motion.back_up"], "distance_m", 0.2, 3.0))},
            "闭集：后退",
        )
    strafe = _STRAFE.fullmatch(phrase)
    if strafe is not None:
        skill = "motion.strafe_left" if strafe.group("direction") == "左" else "motion.strafe_right"
        if skill in capabilities:
            distance = _translate_distance_m(strafe.group("count"), strafe.group("unit"))
            return AgentIntent(
                skill,
                {"distance_m": _clamp(distance, _limit(capabilities[skill], "distance_m", 0.2, 1.5))},
                "闭集：侧移",
            )
    move = _MOVE.fullmatch(phrase)
    if move is None:
        return None
    unit = (move.group("unit") or "").lower()
    amount = _count(move.group("count"))
    speed = _WALK_SPEED if move.group("direction") == "前" else -_WALK_SPEED
    if unit in {"米", "m"}:
        if move.group("direction") == "后" and "motion.back_up" in capabilities and amount is not None:
            return AgentIntent(
                "motion.back_up",
                {
                    "distance_m": _clamp(
                        amount, _limit(capabilities["motion.back_up"], "distance_m", 0.2, 3.0)
                    )
                },
                "闭集：后退",
            )
        capability = capabilities.get("sport.move_distance")
        if capability is None or amount is None:
            return None
        low, high = _limit(capability, "vx", -2.5, 3.8)
        return AgentIntent(
            "sport.move_distance",
            {
                "vx": _clamp(speed, (low, high)),
                "vy": 0.0,
                "vyaw": 0.0,
                "distance_m": _clamp(amount, _limit(capability, "distance_m", 0.2, 8.0)),
            },
            "闭集：按距离移动",
        )
    capability = capabilities.get("sport.move")
    if capability is None:
        return None
    if unit in {"分钟", "分"}:
        duration = 1000 if amount is None else amount * 60000
    elif unit == "秒":
        duration = 1000 if amount is None else amount * 1000
    else:
        duration = 1000 if amount is None else amount * 1000
    low, high = _limit(capability, "vx", -2.5, 3.8)
    return AgentIntent(
        "sport.move",
        {
            "vx": _clamp(speed, (low, high)),
            "vy": 0,
            "vyaw": 0,
            "duration_ms": _clamp(duration, _limit(capability, "duration_ms", 200, 120000)),
        },
        "闭集：按时间移动",
    )


def _phrase_candidates(text: str, wake_phrase: str | Sequence[str]) -> list[str]:
    """Yield normalized phrases, including ASR wake-garble fallbacks.

    Example: wake is 索拉 but ASR returns「左拉，向后转」→ also try「向后转」。
    """

    wakes = _wake_list(wake_phrase)
    primary = _normalize(text, wakes)
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        cleaned = value.strip("，。！？,.!?、 ")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            candidates.append(cleaned)

    add(primary)
    raw = "".join(str(text).split()).strip("，。！？,.!?、 ")
    aliases = []
    for wake in wakes:
        compact = "".join(wake.split())
        aliases.append(compact)
        aliases.extend(_WAKE_ASR_ALIASES.get(compact, ()))
    for alias in sorted({item for item in aliases if item}, key=len, reverse=True):
        if raw.startswith(alias):
            add(raw[len(alias) :])
        if primary.startswith(alias):
            add(primary[len(alias) :])
    # Same-length near-wake:「佐拉站起来」when wake is「索拉」(shared last char).
    for wake in wakes:
        compact = "".join(wake.split())
        if len(compact) != 2:
            continue
        for source in (raw, primary):
            if (
                len(source) > 2
                and source[1] == compact[1]
                and source[0] != compact[0]
                and source[2] in _COMMAND_LEADERS
            ):
                add(source[2:])
    for sep in ("，", ",", "、"):
        if sep in primary:
            add(primary.split(sep, 1)[1])
        if sep in raw:
            add(raw.split(sep, 1)[1])
    return candidates


def _wake_list(wake_phrase: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(wake_phrase, str):
        return tuple(part.strip() for part in wake_phrase.split(",") if part.strip())
    return tuple(phrase.strip() for phrase in wake_phrase if isinstance(phrase, str) and phrase.strip())


def _normalize(text: str, wake_phrase: str | Sequence[str]) -> str:
    cleaned = "".join(str(text).split())
    cleaned = cleaned.strip("，。！？,.!?、 ")
    for phrase in sorted(("".join(item.split()) for item in _wake_list(wake_phrase)), key=len, reverse=True):
        if phrase and cleaned.startswith(phrase):
            cleaned = cleaned[len(phrase) :].lstrip("，。！？,.!?、 ")
    return cleaned


def _translate_distance_m(count: str | None, unit: str | None) -> float:
    amount = _count(count)
    if amount is None:
        return _DEFAULT_TRANSLATE_M
    if (unit or "").lower() in {"米", "m"}:
        return amount
    if unit == "步":
        return amount * _STEP_M
    return amount


def _count(value: str | None) -> float | None:
    if not value:
        return None
    if value in _COUNTS:
        return _COUNTS[value]
    try:
        parsed: float | int = float(value) if "." in value else int(value)
    except ValueError:
        return None
    if isinstance(parsed, float) and parsed.is_integer():
        parsed = int(parsed)
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
