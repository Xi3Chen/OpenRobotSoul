from __future__ import annotations

import re
from typing import Protocol

from robotsoul.models import AgentIntent, CapabilitySpec, Observation


class AgentProvider(Protocol):
    def plan(
        self,
        user_input: str,
        capabilities: dict[str, CapabilitySpec],
        observations: tuple[Observation, ...] = (),
    ) -> AgentIntent | None:
        """Return a bounded high-level plan; providers must enforce their own request timeout."""


class DemoAgent:
    """Rule-based local agent that keeps the MVP runnable without credentials."""

    def plan(
        self,
        user_input: str,
        capabilities: dict[str, CapabilitySpec],
        observations: tuple[Observation, ...] = (),
    ) -> AgentIntent | None:
        text = user_input.strip()
        lowered = text.lower()
        if any(
            phrase in lowered
            for phrase in (
                "不要", "别", "不需要", "不用", "停止", "取消", "don't", "do not", "not ", "stop ", "cancel "
            )
        ):
            return None
        say_match = re.search(r"(?:say|speak|播报|说)[：:\s]*(.*)$", text, re.IGNORECASE)
        if say_match and "speech.say" in capabilities:
            spoken_text = say_match.group(1).strip() or "你好"
            return AgentIntent("speech.say", {"text": spoken_text}, "Matched a speech request")
        if any(token in lowered for token in ("抬头", "看", "head", "look")):
            if "head.look" not in capabilities:
                return None
            angle = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:度|degrees?)?", lowered)
            requested_angle = float(angle.group(1)) if angle else None
            yaw = 0
            pitch = 10
            if "左" in text or "left" in lowered:
                yaw = -(requested_angle if requested_angle is not None else 20)
            elif "右" in text or "right" in lowered:
                yaw = requested_angle if requested_angle is not None else 20
            elif requested_angle is not None and not any(token in lowered for token in ("抬头", "head up")):
                yaw = requested_angle
            if requested_angle is not None and any(token in lowered for token in ("抬头", "head up", "pitch")):
                pitch = requested_angle
            return AgentIntent("head.look", {"yaw_deg": yaw, "pitch_deg": pitch}, "Matched a look request")
        if any(token in lowered for token in ("前进", "后退", "移动", "move", "forward", "back")):
            if "base.move_distance" not in capabilities:
                return None
            distance_match = re.search(
                r"(-?\d+(?:\.\d+)?)\s*(?:m(?!/s)\b|米(?!每秒)|meters?\b(?!\s+per second))",
                lowered,
            )
            has_number = re.search(r"-?\d+(?:\.\d+)?", lowered) is not None
            if has_number and distance_match is None:
                return None
            distance = float(distance_match.group(1)) if distance_match else 0.2
            if any(token in lowered for token in ("后退", "back")):
                distance = -abs(distance)
            speed_match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:m/s|米每秒|meters? per second)", lowered)
            speed = float(speed_match.group(1)) if speed_match else 0.1
            return AgentIntent(
                "base.move_distance",
                {"distance_m": distance, "max_speed_mps": speed},
                "Matched a bounded move request",
            )
        return None
