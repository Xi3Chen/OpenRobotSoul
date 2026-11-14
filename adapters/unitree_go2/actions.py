"""Go2 SportClient actions exposed through the development profile.

Ranges for Move, Euler, and SpeedLevel follow the parameter bounds published
with Unitree's Go2 SportClient. They are request limits, not a record of
hardware-in-the-loop acceptance. ``duration_ms`` is the whole timed move. One native Move lasts at most about
two seconds, so the driver repeats it for ``move_cycle_count`` cycles and then
calls StopMove. ``sport.move_distance`` is separate: it repeats Move until the
fused sport position and lidar odometry reach ``distance_m``.
Body poses that have no SDK completion signal carry a nominal hold. That hold
is an uncalibrated sequencing budget, not a measured body time. Recalibrate it
on the robot when the firmware changes.

``Damp`` is intentionally absent. That call removes motor torque.

Confirm-tier is only the obstacle-avoidance mode switch. Ordinary posture,
gait changes, and timed motion execute after schema checks. High-tier is
limited to flips, jumps, handstands, dances, recovery stand, joystick source
changes, and the auto-recover switch.
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
    "duration_ms": _number(200, 120000),
}
_MOVE_DISTANCE = {
    "vx": _number(-2.5, 3.8),
    "vy": _number(-1.0, 1.0),
    "vyaw": _number(-4.0, 4.0),
    "distance_m": _number(0.2, 8.0),
}
_LEVEL = {"level": _number(-1, 1)}
_VOLUME = {"volume": _number(0, 10)}
_FLAG = {"enabled": ParameterSpec("boolean")}


@dataclass(frozen=True)
class SportAction:
    skill: str
    bridge_name: str
    kind: str
    risk_level: RiskLevel
    description: str
    parameters: dict[str, ParameterSpec]
    timeout_ms: int = 5000
    required: bool = False
    estimate_ms: int = 1500
    completion: str = "accepted"

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
            description=self.description,
            estimate_ms=self.estimate_ms,
            completion=self.completion,
        )


# Nominal holds are local sequencing budgets. They are not measured body times.
_NOMINAL_MS = {
    "balance_stand": 3000,
    "stand_up": 3000,
    "stand_down": 3000,
    "sit": 3000,
    "rise_sit": 3000,
    "hello": 4000,
    "stretch": 4000,
    "content": 4000,
    "heart": 4000,
    "scrape": 4000,
    "recovery_stand": 4000,
    "front_flip": 3000,
    "front_jump": 3000,
    "front_pounce": 3000,
    "dance1": 8000,
    "dance2": 8000,
    "left_flip": 3000,
    "back_flip": 3000,
    "handstand": 3000,
    "euler": 2000,
    "pose": 2000,
}


def _timing(bridge_name: str) -> tuple[str, int, int]:
    if bridge_name == "move":
        return "timed", 120000, 8000
    estimate_ms = _NOMINAL_MS.get(bridge_name)
    if estimate_ms is not None:
        return "nominal", estimate_ms, estimate_ms + 5000
    return "accepted", 1500, 5000


def _action(
    bridge_name: str,
    kind: str,
    risk_level: RiskLevel,
    parameters: dict[str, ParameterSpec] | None = None,
    required: bool = False,
    prefix: str = "sport",
    *,
    skill: str | None = None,
    description: str | None = None,
    completion: str | None = None,
    estimate_ms: int | None = None,
    timeout_ms: int | None = None,
) -> SportAction:
    default_completion, default_estimate, default_timeout = _timing(bridge_name)
    return SportAction(
        skill=skill or f"{prefix}.{bridge_name}",
        bridge_name=bridge_name,
        kind=kind,
        risk_level=risk_level,
        description=description if description is not None else _SDK_DESCRIPTION[bridge_name],
        parameters=dict(parameters or {}),
        timeout_ms=default_timeout if timeout_ms is None else timeout_ms,
        required=required,
        estimate_ms=default_estimate if estimate_ms is None else estimate_ms,
        completion=default_completion if completion is None else completion,
    )


# Meanings taken from adapters/unitree_sdk2:
# - example/go2/go2_sport_client.cpp comments
# - example/go2w/go2w_sport_client.cpp option comments and the Move note
# - include/unitree/robot/go2/sport/sport_client.hpp method names and parameters
# Methods without a comment keep the SDK name and do not add an unverified body effect.
_SDK_DESCRIPTION = {
    "stop_move": "停止高层速度运动。SDK 示例 go2w_sport_client.cpp 注释为「停止运动」，不是卸力。",
    "balance_stand": "进入平衡站立。SDK 示例注释为「平衡站立」；go2_sport_client.cpp 写明该姿态可由 roll、pitch、yaw 调整。",
    "stand_up": "切换到站立锁定。SDK 示例 go2w_sport_client.cpp 注释为「站立锁定」，go2_sport_client.cpp 注释为 position stand up，也作为默认站立。",
    "stand_down": "趴下。SDK 示例注释为「趴下」和 position stand down。",
    "sit": "请求坐下。SDK 示例 go2_sport_client.cpp 将 Sit() 列为特殊动作，头文件没有更多说明。",
    "rise_sit": "请求从坐姿起身。SDK 示例将 RiseSit() 列为特殊动作，头文件没有更多说明。",
    "hello": "SportClient::Hello()。SDK 只有方法名，无参数，头文件没有肢体动作说明。",
    "stretch": "SportClient::Stretch()。SDK 只有方法名，无参数，头文件没有肢体动作说明。",
    "content": "SportClient::Content()。SDK 只有方法名，无参数，头文件没有说明具体表现。",
    "heart": "SportClient::Heart()。SDK 只有方法名，无参数，头文件没有说明具体表现。",
    "scrape": "SportClient::Scrape()。SDK 只有方法名，无参数，头文件没有说明具体表现。",
    "free_walk": "SportClient::FreeWalk()。SDK 只有方法名，无参数，头文件没有进一步说明。",
    "static_walk": "SportClient::StaticWalk()。SDK 只有方法名，无参数，头文件没有进一步说明。",
    "trot_run": "SportClient::TrotRun()。SDK 只有方法名，无参数，头文件没有进一步说明。",
    "economic_gait": "SportClient::EconomicGait()。SDK 只有方法名，无参数，头文件没有进一步说明。",
    "recovery_stand": "恢复站立。SDK 示例注释为「恢复站立」和 recovery stand。",
    "front_flip": "SportClient::FrontFlip()，前空翻。无参数，头文件没有更多说明。",
    "front_jump": "SportClient::FrontJump()，向前跳。无参数，头文件没有更多说明。",
    "front_pounce": "SportClient::FrontPounce()，向前扑。无参数，头文件没有更多说明。",
    "dance1": "SportClient::Dance1()，舞蹈动作 1。无参数，头文件没有说明舞步内容。",
    "dance2": "SportClient::Dance2()，舞蹈动作 2。无参数，头文件没有说明舞步内容。",
    "left_flip": "SportClient::LeftFlip()，向左侧翻。无参数，头文件没有更多说明。",
    "back_flip": "SportClient::BackFlip()，后空翻。无参数，头文件没有更多说明。",
    "switch_avoid_mode": "SportClient::SwitchAvoidMode()，切换避障模式。无参数，头文件没有说明切换后的具体模式。",
    "pose": "SportClient::Pose(flag)。enabled 为真时进入，为假时退出。头文件没有说明姿势内容。",
    "classic_walk": "SportClient::ClassicWalk(flag)，经典步态。enabled 为真时进入，为假时退出。头文件没有更多说明。",
    "switch_joystick": "SportClient::SwitchJoystick(flag)，切换手柄控制。enabled 为真时启用，为假时关闭。",
    "handstand": "SportClient::HandStand(flag)，倒立。enabled 为真时进入，为假时退出。",
    "free_bound": "SportClient::FreeBound(flag)，自由弹跳。enabled 为真时进入，为假时退出。头文件没有更多说明。",
    "free_jump": "SportClient::FreeJump(flag)，自由跳跃。enabled 为真时进入，为假时退出。头文件没有更多说明。",
    "free_avoid": "SportClient::FreeAvoid(flag)，行走时避让。enabled 为真时进入，为假时退出。头文件没有说明具体避让行为。",
    "walk_upright": "SportClient::WalkUpright(flag)，直立行走。enabled 为真时进入，为假时退出。头文件没有更多说明。",
    "cross_step": "SportClient::CrossStep(flag)，交叉步。enabled 为真时进入，为假时退出。头文件没有更多说明。",
    "auto_recover_set": "SportClient::AutoRecoverSet(flag)，设置摔倒后是否自动恢复。enabled 为真时开启，为假时关闭。",
    "euler": "SportClient::Euler(roll, pitch, yaw)，调整机身滚转、俯仰和偏航。SDK 示例写明平衡站立可由这三个值控制，但没有标明单位。",
    "move": "按时间移动，速度控制。duration_ms 是整段时长，最长 120 秒。原生 Move 单次最长约 2 秒，程序按这个上限计算循环次数并重复发送，到时调用 StopMove。一次移动只调用一次，不要拆成多步。SDK 没有标明速度单位。",
    "move_distance": "按距离移动。distance_m 是相对起点的水平位移，范围 0.2 到 8 米。程序同时读取机体位置和雷达里程计，读数一致后调用 StopMove。到截止预算仍不够则失败。一次移动只调用一次，不要拆成多步。位置按米解释，坐标系未另行换算。",
    "speed_level": "SportClient::SpeedLevel(level)，速度档位。SDK 示例注释为「速度档位」，没有说明每个档位对应的速度。",
    "auto_recover_get": "SportClient::AutoRecoverGet()，查询摔倒后自动恢复开关的当前值。",
    "set_volume": "VuiClient::SetVolume(level)，设置机身音量。volume 为整数，官方文档取值范围 0~10，0 为静音，10 为最大。",
}


SPORT_ACTIONS: tuple[SportAction, ...] = (
    _action("stop_move", "none", RiskLevel.NORMAL, required=True),
    _action("balance_stand", "none", RiskLevel.NORMAL),
    _action("stand_up", "none", RiskLevel.NORMAL),
    _action("stand_down", "none", RiskLevel.NORMAL),
    _action("sit", "none", RiskLevel.NORMAL),
    _action("rise_sit", "none", RiskLevel.NORMAL),
    _action("hello", "none", RiskLevel.NORMAL),
    _action("stretch", "none", RiskLevel.NORMAL),
    _action("content", "none", RiskLevel.NORMAL),
    _action("heart", "none", RiskLevel.NORMAL),
    _action("scrape", "none", RiskLevel.NORMAL),
    _action("free_walk", "none", RiskLevel.NORMAL),
    _action("static_walk", "none", RiskLevel.NORMAL),
    _action("trot_run", "none", RiskLevel.NORMAL),
    _action("economic_gait", "none", RiskLevel.NORMAL),
    _action("recovery_stand", "none", RiskLevel.HIGH),
    _action("front_flip", "none", RiskLevel.HIGH),
    _action("front_jump", "none", RiskLevel.HIGH),
    _action("front_pounce", "none", RiskLevel.HIGH),
    _action("dance1", "none", RiskLevel.HIGH),
    _action("dance2", "none", RiskLevel.HIGH),
    _action("left_flip", "none", RiskLevel.HIGH),
    _action("back_flip", "none", RiskLevel.HIGH),
    _action("switch_avoid_mode", "none", RiskLevel.CONFIRM),
    _action("pose", "flag", RiskLevel.NORMAL, _FLAG),
    _action("classic_walk", "flag", RiskLevel.NORMAL, _FLAG),
    _action("switch_joystick", "flag", RiskLevel.HIGH, _FLAG),
    _action("handstand", "flag", RiskLevel.HIGH, _FLAG),
    _action("free_bound", "flag", RiskLevel.HIGH, _FLAG),
    _action("free_jump", "flag", RiskLevel.HIGH, _FLAG),
    _action("free_avoid", "flag", RiskLevel.NORMAL, _FLAG),
    _action("walk_upright", "flag", RiskLevel.NORMAL, _FLAG),
    _action("cross_step", "flag", RiskLevel.NORMAL, _FLAG),
    _action("auto_recover_set", "flag", RiskLevel.HIGH, _FLAG),
    _action("euler", "euler", RiskLevel.NORMAL, _EULER),
    _action("move", "move", RiskLevel.NORMAL, _MOVE),
    _action(
        "move",
        "move_distance",
        RiskLevel.NORMAL,
        _MOVE_DISTANCE,
        skill="sport.move_distance",
        description=_SDK_DESCRIPTION["move_distance"],
        completion="measured",
        estimate_ms=20000,
        timeout_ms=90000,
    ),
    _action("speed_level", "level", RiskLevel.NORMAL, _LEVEL),
    _action("auto_recover_get", "query", RiskLevel.NORMAL),
    _action("set_volume", "volume", RiskLevel.NORMAL, _VOLUME, prefix="vui"),
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
                "description": spec.description,
                "estimate_ms": spec.estimate_ms,
                "completion": spec.completion,
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
        if name in {"level", "duration_ms", "volume"} and float(value) != trunc(float(value)):
            return f"Parameter {name} must be an integer"
    if action.kind == "move_distance" and abs(float(parameters["vx"])) < 0.2:
        return "Parameter vx must be at least 0.2 in magnitude for a distance move"
    return None


def native_arguments(action: SportAction, parameters: dict[str, Any]) -> tuple[list[float], bool | None]:
    if action.kind in {"none", "query"}:
        return [], None
    if action.kind == "euler":
        return [float(parameters[name]) for name in ("roll", "pitch", "yaw")], None
    if action.kind in {"move", "move_distance"}:
        return [float(parameters[name]) for name in ("vx", "vy", "vyaw")], None
    if action.kind == "level":
        return [float(int(parameters["level"]))], None
    if action.kind == "volume":
        return [float(int(parameters["volume"]))], None
    if action.kind == "flag":
        return [], bool(parameters["enabled"])
    raise RuntimeError(f"Unsupported Go2 sport action kind: {action.kind}")
