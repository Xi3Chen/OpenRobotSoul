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

Ordinary posture, gait changes, dances, recovery stand, and timed motion
execute after schema checks. Confirm-tier covers motions that need a large
clear area and that obstacle avoidance cannot make safe (flips, jumps, pounce,
handstand, free bound), plus the obstacle-avoidance mode switch, the joystick
source switch, and the auto-recover switch. High-tier is reserved for switches
that change the robot's own protection service; currently that is
``obstacles.avoid_set`` (ObstaclesAvoidClient::SwitchSet), Dashboard-only with
fresh operator password confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, pi, trunc
from typing import Any

from adapters.unitree_go2.motion import BACK_UP_SPEED_MPS, STRAFE_SPEED_MPS
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
    retry_safe: bool = False
    target_radians: float | None = None
    move_velocity: tuple[float, float, float] | None = None

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
            retry_safe=self.retry_safe,
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
    retry_safe: bool = False,
    target_radians: float | None = None,
    move_velocity: tuple[float, float, float] | None = None,
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
        retry_safe=retry_safe,
        target_radians=target_radians,
        move_velocity=move_velocity,
    )


# Meanings taken from adapters/unitree_sdk2:
# - example/go2/go2_sport_client.cpp comments
# - example/go2w/go2w_sport_client.cpp option comments and the Move note
# - include/unitree/robot/go2/sport/sport_client.hpp method names and parameters
# Methods without a comment keep the SDK name and do not add an unverified body effect.
_SDK_DESCRIPTION = {
    "stop_move": "停止高层速度运动。SDK 示例 go2w_sport_client.cpp 注释为「停止运动」，不是卸力。",
    "balance_stand": (
        "进入平衡站立。SDK 示例注释为「平衡站立」；go2_sport_client.cpp 写明该姿态可由 roll、pitch、yaw 调整。"
        "调用 sport.euler 保持某个姿态角前先进入此模式。"
        "点头、摇头、歪头请用 expression.nod / expression.shake_head / expression.tilt_head，不要手拼 Euler 序列。"
    ),
    "stand_up": (
        "切换到站立锁定。口语：站起来、起立、站立、起来。"
        "SDK 示例 go2w_sport_client.cpp 注释为「站立锁定」，go2_sport_client.cpp 注释为 position stand up，也作为默认站立。"
        "parameters 为空。若用户同时要求转向，先本技能再接 motion.turn_*，不要追问。"
    ),
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
    "obstacles_avoid_set": (
        "ObstaclesAvoidClient::SwitchSet(enable)，打开或关闭机身避障服务。"
        "enabled 为真时开启服务，为假时关闭服务。关闭会去掉机身避障保护，属于高危操作；"
        "仅 Dashboard 操作员可执行，并需重新输入当前账户密码。"
        "这与 sport.switch_avoid_mode / sport.free_avoid 不同：后两者不替代本服务总开关。"
    ),
    "euler": (
        "SportClient::Euler(roll, pitch, yaw)，在平衡站立下设定并保持机身姿态角，不是原地转身，也不是点头/摇头/歪头。"
        "Go2 无独立颈部：pitch 抬头低头，yaw 左右扭身，roll 左右侧倾；单次调用只设一个姿态并保持。"
        "用户要点头用 expression.nod，摇头用 expression.shake_head，歪头用 expression.tilt_head。"
        "用户要左转、右转、转身、调头时不要用本技能，改用 motion.turn_* 或 sport.move 的 vyaw。"
        "SDK 示例写明平衡站立可由这三个值控制，但没有标明单位。"
    ),
    "move": (
        "按时间做速度控制移动。vx 前进/后退，vy 左右平移，vyaw 原地转向角速度。"
        "固定左转/右转约 90° 或向后转 180° 时优先用 motion.turn_left / motion.turn_right / motion.turn_around；"
        "只有任意角度或任意时长原地转才用本技能的 vyaw（vx/vy 可为 0）。不要用 sport.euler 转身。"
        "duration_ms 是整段时长，范围 200 毫秒到 120 秒；SDK 没有标明速度单位。"
        "程序按约 2 秒周期重复发送并在到时请求 StopMove；timed 只证明本地周期结束且 StopMove 被接受，不证明走过指定距离或转到指定角度。"
        "一次连续移动只调用一次，不要拆成多步。"
    ),
    "move_distance": (
        "按距离直线移动；vx/vy/vyaw 含义同 sport.move，必须提供三个速度分量和 distance_m（0.2 到 8 米）。"
        "distance_m 始终是正的水平路程目标；前进/后退/侧移方向由速度分量决定，SDK 未标明速度单位。"
        "纯后退优先用 motion.back_up；纯左右侧移优先用 motion.strafe_left / motion.strafe_right。"
        "只改变朝向用 motion.turn_*；任意角度原地转用 sport.move 的 vyaw。"
        "宿主融合机体位置与雷达里程计，只有读数一致到达目标并成功请求 StopMove 才算完成；传感器缺失/不一致或截止时间到仍未到位都会失败。"
        "一次移动只调用一次，不要拆成多步。位置按米解释，坐标系未另行换算。"
    ),
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
    _action("recovery_stand", "none", RiskLevel.NORMAL),
    _action("front_flip", "none", RiskLevel.CONFIRM),
    _action("front_jump", "none", RiskLevel.CONFIRM),
    _action("front_pounce", "none", RiskLevel.CONFIRM),
    _action("dance1", "none", RiskLevel.NORMAL),
    _action("dance2", "none", RiskLevel.NORMAL),
    _action("left_flip", "none", RiskLevel.CONFIRM),
    _action("back_flip", "none", RiskLevel.CONFIRM),
    _action("switch_avoid_mode", "none", RiskLevel.CONFIRM),
    _action("pose", "flag", RiskLevel.NORMAL, _FLAG),
    _action("classic_walk", "flag", RiskLevel.NORMAL, _FLAG),
    _action("switch_joystick", "flag", RiskLevel.CONFIRM, _FLAG),
    _action("handstand", "flag", RiskLevel.CONFIRM, _FLAG),
    _action("free_bound", "flag", RiskLevel.CONFIRM, _FLAG),
    _action("free_jump", "flag", RiskLevel.CONFIRM, _FLAG),
    _action("free_avoid", "flag", RiskLevel.NORMAL, _FLAG),
    _action("walk_upright", "flag", RiskLevel.NORMAL, _FLAG),
    _action("cross_step", "flag", RiskLevel.NORMAL, _FLAG),
    _action("auto_recover_set", "flag", RiskLevel.CONFIRM, _FLAG),
    _action(
        "obstacles_avoid_set",
        "flag",
        RiskLevel.HIGH,
        _FLAG,
        prefix="obstacles",
        skill="obstacles.avoid_set",
        description=_SDK_DESCRIPTION["obstacles_avoid_set"],
        completion="accepted",
        estimate_ms=1500,
        timeout_ms=8000,
    ),
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
    _action(
        "move", "turn", RiskLevel.NORMAL,
        skill="motion.turn_left",
        description=(
            "原地向左闭环转向固定 90°。口语：左转、向左转、往左转。"
            "parameters 必须为 {}，不要填角度。以 IMU yaw 测量到位；IMU 不可读、取消或超时会失败。"
            "失败后不能假定机器人仍朝原方向；不可自动重试。"
        ),
        completion="measured", estimate_ms=5000, timeout_ms=12000,
        target_radians=pi / 2,
    ),
    _action(
        "move", "turn", RiskLevel.NORMAL,
        skill="motion.turn_right",
        description=(
            "原地向右闭环转向固定 90°。口语：右转、向右转、往右转。"
            "parameters 必须为 {}，不要填角度。以 IMU yaw 测量到位；IMU 不可读、取消或超时会失败。"
            "失败后不能假定机器人仍朝原方向；不可自动重试。"
        ),
        completion="measured", estimate_ms=5000, timeout_ms=12000,
        target_radians=-pi / 2,
    ),
    _action(
        "move", "turn", RiskLevel.NORMAL,
        skill="motion.turn_around",
        description=(
            "原地向左闭环转向固定 180°（掉头）。口语：向后转、往后转、掉头、转过身、转身、转一百八十度。"
            "parameters 必须为 {}；方向固定为向左，不支持选择旋转方向或任意角度，不要追问是否 180° 或面向哪边。"
            "以 IMU yaw 测量到位；IMU 不可读、取消或超时会失败。失败后不能假定机器人仍朝原方向；不可自动重试。"
        ),
        completion="measured", estimate_ms=10000, timeout_ms=25000,
        target_radians=pi,
    ),
    _action(
        "move", "translate", RiskLevel.CONFIRM,
        {"distance_m": _number(0.2, 3.0)},
        skill="motion.back_up",
        description=(
            "按距离向后平移。口语：后退、往后退、向后走一米。"
            "只提供正数 distance_m（0.2–3.0 米），速度由技能固定，不要填 vx/vy。"
            "此技能为 confirm 风险级，须经过宿主复核；使用融合位置与里程计测量，后方盲区或传感器缺失/不一致会阻止或中断动作。"
            "失败后结果可能不确定，不能自动重试或假定未移动。"
        ),
        completion="measured", estimate_ms=8000, timeout_ms=20000,
        move_velocity=(-BACK_UP_SPEED_MPS, 0.0, 0.0),
    ),
    _action(
        "move", "translate", RiskLevel.CONFIRM,
        {"distance_m": _number(0.2, 1.5)},
        skill="motion.strafe_left",
        description=(
            "按距离向左侧移。口语：向左平移、向左侧移、往左横移。"
            "只提供正数 distance_m（0.2–1.5 米），速度由技能固定，不要填 vx/vy。此技能为 confirm 风险级，须经过宿主复核。"
            "位置反馈不可用或到截止时间未到位时会失败；失败后不能假定未移动，也不能自动重试。"
        ),
        completion="measured", estimate_ms=6000, timeout_ms=15000,
        move_velocity=(0.0, STRAFE_SPEED_MPS, 0.0),
    ),
    _action(
        "move", "translate", RiskLevel.CONFIRM,
        {"distance_m": _number(0.2, 1.5)},
        skill="motion.strafe_right",
        description=(
            "按距离向右侧移。口语：向右平移、向右侧移、往右横移。"
            "只提供正数 distance_m（0.2–1.5 米），速度由技能固定，不要填 vx/vy。此技能为 confirm 风险级，须经过宿主复核。"
            "位置反馈不可用或到截止时间未到位时会失败；失败后不能假定未移动，也不能自动重试。"
        ),
        completion="measured", estimate_ms=6000, timeout_ms=15000,
        move_velocity=(0.0, -STRAFE_SPEED_MPS, 0.0),
    ),
    _action(
        "euler", "expression", RiskLevel.NORMAL,
        {"direction": ParameterSpec("string", max_length=5)},
        prefix="expression", skill="expression.tilt_head",
        description=(
            "向指定方向轻微侧倾约 1 秒后恢复中立。口语：歪头、向左歪头、向右歪头。"
            "direction 只能是 left 或 right；不要用 sport.euler 手拼。完成状态为名义估计，不是姿态传感器验证。"
            "仅在宿主明确判定本次失败且允许重试时才重试，避免重复动作。"
        ),
        completion="nominal", estimate_ms=2000, timeout_ms=7000,
        retry_safe=True,
    ),
    _action(
        "euler", "expression", RiskLevel.NORMAL,
        {"count": _number(1, 5)},
        prefix="expression", skill="expression.nod",
        description=(
            "点头 1 到 5 次，每次向下再向上，完成后恢复中立。口语：点头、点个头、点一下头。"
            "count 必须是 1..5 的整数；未说明次数时用 1。不要用 sport.euler 手拼序列。"
            "完成状态为名义估计，不是姿态传感器验证；仅在宿主明确判定本次失败且允许重试时才重试。"
        ),
        completion="nominal", estimate_ms=6000, timeout_ms=11000,
        retry_safe=True,
    ),
    _action(
        "euler", "expression", RiskLevel.NORMAL,
        {"count": _number(1, 5)},
        prefix="expression", skill="expression.shake_head",
        description=(
            "左右摇头 1 到 5 次，每次左摆再右摆，完成后恢复中立。口语：摇头、摇摇头。"
            "count 必须是 1..5 的整数；未说明次数时用 1。不要用 sport.euler 手拼序列。"
            "完成状态为名义估计，不是姿态传感器验证；仅在宿主明确判定本次失败且允许重试时才重试。"
        ),
        completion="nominal", estimate_ms=6000, timeout_ms=11000,
        retry_safe=True,
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
                "retry_safe": spec.retry_safe,
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
        if parameter.value_type == "string":
            if not isinstance(value, str):
                return f"Parameter {name} must be text"
            if parameter.max_length is not None and len(value) > parameter.max_length:
                return f"Parameter {name} is too long"
            if name == "direction" and value not in {"left", "right"}:
                return "Parameter direction must be 'left' or 'right'"
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            return f"Parameter {name} must be a finite number"
        if parameter.minimum is not None and value < parameter.minimum:
            return f"Parameter {name} is below its verified minimum"
        if parameter.maximum is not None and value > parameter.maximum:
            return f"Parameter {name} exceeds its verified maximum"
        if name in {"level", "duration_ms", "volume", "count"} and float(value) != trunc(float(value)):
            return f"Parameter {name} must be an integer"
    if action.kind == "move_distance" and abs(float(parameters["vx"])) < 0.2:
        return "Parameter vx must be at least 0.2 in magnitude for a distance move"
    return None


def native_arguments(action: SportAction, parameters: dict[str, Any]) -> tuple[list[float], bool | None]:
    if action.kind in {"none", "query"}:
        return [], None
    if action.kind in {"turn", "expression", "translate"}:
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
