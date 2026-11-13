"""Go2 SportClient actions exposed through the development profile.

Ranges for Move, Euler, and SpeedLevel follow the parameter bounds published
with Unitree's Go2 SportClient. They are request limits, not a record of
hardware-in-the-loop acceptance. ``duration_ms`` is local: Move is sent once,
then StopMove runs when the duration ends or the command is cancelled.

``Damp`` is intentionally absent. That call removes motor torque.

Confirm-tier covers ordinary posture, gait changes, and timed motion.
High-tier is limited to flips, jumps, handstands, dances, recovery stand,
joystick source changes, and the auto-recover switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, trunc
from typing import Any

from robotsoul.models import CapabilitySpec, ParameterSpec, RiskLevel


def _number(minimum: float, maximum: float) -> ParameterSpec:
    return ParameterSpec("number", float(minimum), float(maximum))


_EULER = {
    "roll": _number(-0.75, 0.75),
    "pitch": _number(-0.75, 0.75),
    "yaw": _number(-0.6, 0.6),
}
_MOVE = {
    "vx": _number(-2.5, 3.8),
    "vy": _number(-1.0, 1.0),
    "vyaw": _number(-4.0, 4.0),
    "duration_ms": _number(200, 2000),
}
_LEVEL = {"level": _number(-1, 1)}
_FLAG = {"enabled": ParameterSpec("boolean")}


@dataclass(frozen=True)
class SportAction:
    skill: str
    bridge_name: str
    kind: str
    risk_level: RiskLevel
    parameters: dict[str, ParameterSpec]
    timeout_ms: int = 5000
    required: bool = False

    def capability(self) -> CapabilitySpec:
        high = self.risk_level == RiskLevel.HIGH
        return CapabilitySpec(
            name=self.skill,
            parameters=dict(self.parameters),
            risk_level=self.risk_level,
            allowed_callers=("dashboard",) if high else ("agent", "dashboard"),
            requires_operator_confirmation=high,
            interruptible=True,
            timeout_ms=self.timeout_ms,
            required=self.required,
        )


def _action(
    bridge_name: str,
    kind: str,
    risk_level: RiskLevel,
    parameters: dict[str, ParameterSpec] | None = None,
    timeout_ms: int = 5000,
    required: bool = False,
) -> SportAction:
    return SportAction(
        skill=f"sport.{bridge_name}",
        bridge_name=bridge_name,
        kind=kind,
        risk_level=risk_level,
        parameters=dict(parameters or {}),
        timeout_ms=timeout_ms,
        required=required,
    )


SPORT_ACTIONS: tuple[SportAction, ...] = (
    _action("stop_move", "none", RiskLevel.NORMAL, required=True),
    _action("balance_stand", "none", RiskLevel.CONFIRM),
    _action("stand_up", "none", RiskLevel.CONFIRM),
    _action("stand_down", "none", RiskLevel.CONFIRM),
    _action("sit", "none", RiskLevel.CONFIRM),
    _action("rise_sit", "none", RiskLevel.CONFIRM),
    _action("hello", "none", RiskLevel.CONFIRM),
    _action("stretch", "none", RiskLevel.CONFIRM),
    _action("content", "none", RiskLevel.CONFIRM),
    _action("heart", "none", RiskLevel.CONFIRM),
    _action("scrape", "none", RiskLevel.CONFIRM),
    _action("free_walk", "none", RiskLevel.CONFIRM),
    _action("static_walk", "none", RiskLevel.CONFIRM),
    _action("trot_run", "none", RiskLevel.CONFIRM),
    _action("economic_gait", "none", RiskLevel.CONFIRM),
    _action("recovery_stand", "none", RiskLevel.HIGH),
    _action("front_flip", "none", RiskLevel.HIGH),
    _action("front_jump", "none", RiskLevel.HIGH),
    _action("front_pounce", "none", RiskLevel.HIGH),
    _action("dance1", "none", RiskLevel.HIGH),
    _action("dance2", "none", RiskLevel.HIGH),
    _action("left_flip", "none", RiskLevel.HIGH),
    _action("back_flip", "none", RiskLevel.HIGH),
    _action("switch_avoid_mode", "none", RiskLevel.CONFIRM),
    _action("pose", "flag", RiskLevel.CONFIRM, _FLAG),
    _action("classic_walk", "flag", RiskLevel.CONFIRM, _FLAG),
    _action("switch_joystick", "flag", RiskLevel.HIGH, _FLAG),
    _action("handstand", "flag", RiskLevel.HIGH, _FLAG),
    _action("free_bound", "flag", RiskLevel.HIGH, _FLAG),
    _action("free_jump", "flag", RiskLevel.HIGH, _FLAG),
    _action("free_avoid", "flag", RiskLevel.CONFIRM, _FLAG),
    _action("walk_upright", "flag", RiskLevel.CONFIRM, _FLAG),
    _action("cross_step", "flag", RiskLevel.CONFIRM, _FLAG),
    _action("auto_recover_set", "flag", RiskLevel.HIGH, _FLAG),
    _action("euler", "euler", RiskLevel.CONFIRM, _EULER),
    _action("move", "move", RiskLevel.CONFIRM, _MOVE, timeout_ms=8000),
    _action("speed_level", "level", RiskLevel.CONFIRM, _LEVEL),
    _action("auto_recover_get", "query", RiskLevel.NORMAL),
)

_BY_SKILL = {action.skill: action for action in SPORT_ACTIONS}


def sport_action(skill: str) -> SportAction | None:
    return _BY_SKILL.get(skill)


def sport_capabilities() -> tuple[CapabilitySpec, ...]:
    return tuple(action.capability() for action in SPORT_ACTIONS)


def capability_documents() -> list[dict[str, Any]]:
    documents = []
    for spec in sport_capabilities():
        parameters = {}
        for name, parameter in spec.parameters.items():
            encoded: dict[str, Any] = {"type": parameter.value_type}
            if parameter.minimum is not None:
                encoded["min"] = parameter.minimum
            if parameter.maximum is not None:
                encoded["max"] = parameter.maximum
            if parameter.max_length is not None:
                encoded["max_length"] = parameter.max_length
            parameters[name] = encoded
        documents.append(
            {
                "name": spec.name,
                "risk_level": spec.risk_level.value,
                "allowed_callers": list(spec.allowed_callers),
                "requires_operator_confirmation": spec.requires_operator_confirmation,
                "parameters": parameters,
                "interruptible": spec.interruptible,
                "timeout_ms": spec.timeout_ms,
                "required": spec.required,
            }
        )
    return documents


def validate_parameters(action: SportAction, parameters: dict[str, Any]) -> str | None:
    expected = action.capability().parameters
    if set(parameters) != set(expected):
        return "Command parameters do not match the Go2 sport action"
    for name, parameter in expected.items():
        value = parameters[name]
        if parameter.value_type == "boolean":
            if not isinstance(value, bool):
                return f"Parameter {name} must be boolean"
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            return f"Parameter {name} must be a finite number"
        if parameter.minimum is not None and value < parameter.minimum:
            return f"Parameter {name} is below its verified minimum"
        if parameter.maximum is not None and value > parameter.maximum:
            return f"Parameter {name} exceeds its verified maximum"
        if name in {"level", "duration_ms"} and float(value) != trunc(float(value)):
            return f"Parameter {name} must be an integer"
    return None


def native_arguments(action: SportAction, parameters: dict[str, Any]) -> tuple[list[float], bool | None]:
    if action.kind in {"none", "query"}:
        return [], None
    if action.kind == "euler":
        return [float(parameters[name]) for name in ("roll", "pitch", "yaw")], None
    if action.kind == "move":
        return [float(parameters[name]) for name in ("vx", "vy", "vyaw")], None
    if action.kind == "level":
        return [float(int(parameters["level"]))], None
    if action.kind == "flag":
        return [], bool(parameters["enabled"])
    raise RuntimeError(f"Unsupported Go2 sport action kind: {action.kind}")
