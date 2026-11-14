"""Resident language gateway.

The main process starts ``python -m robotsoul.language_gateway`` and keeps it
for the life of the dashboard or ``run`` command. It reads ``config/llm.json``
and returns one JSON object per request. It has no file, shell, or robot tools.

This process is not the Go PicoClaw binary. PicoClaw remains the separate
coding-agent executable.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from robotsoul.motion_plan import combined_amount, motion_amount_key, same_velocity
from robotsoul.robot_config import default_robot_config_path, identity_document, load_robot_deployment_config
from robotsoul.settings import LlmConfigStore, LlmSettings


Completer = Callable[[list[dict[str, str]]], str]
_IDENTITY: dict[str, str] | None = None
_MAX_PLAN_STEPS = 8


def main() -> None:
    global _IDENTITY
    path_value = os.environ.get("OPENROBOTSOUL_LLM_CONFIG", "")
    path = Path(path_value) if path_value else None
    robot_path = os.environ.get("OPENROBOTSOUL_ROBOT_CONFIG", "").strip()
    config_error = ""
    settings = LlmSettings()
    try:
        deployment = load_robot_deployment_config(Path(robot_path) if robot_path else None)
        _IDENTITY = identity_document(deployment)
    except ValueError:
        _IDENTITY = {"robot_name": "机器人", "soul": ""}
    try:
        if path is not None:
            settings = LlmConfigStore(path if path.is_file() else None).snapshot()
    except ValueError as exc:
        config_error = str(exc)

    def complete(messages: list[dict[str, str]]) -> str:
        if config_error:
            raise RuntimeError(config_error)
        return complete_chat(settings, messages)

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _write({"id": None, "rejected": True, "reason": "语言进程收到了无法解析的请求"})
            continue
        if not isinstance(request, dict):
            _write({"id": None, "rejected": True, "reason": "语言进程收到了无法解析的请求"})
            continue
        _write(handle_request(request, complete))


def handle_request(request: dict[str, Any], complete: Completer) -> dict[str, Any]:
    request_id = request.get("id")
    kind = request.get("type")
    try:
        if kind == "decide":
            return {"id": request_id, **_decide(request, complete)}
        if kind == "review":
            return {"id": request_id, **_review(request, complete)}
    except Exception as exc:
        reason = str(exc).strip() or "语言进程没有完成这次判断"
        if kind == "review":
            return {"id": request_id, "verdict": "deny", "rationale": reason}
        return {"id": request_id, "rejected": True, "reason": reason}
    return {"id": request_id, "rejected": True, "reason": "语言进程无法处理这次请求"}


def complete_chat(settings: LlmSettings, messages: list[dict[str, str]]) -> str:
    if not settings.base_url or not settings.model or not settings.api_key:
        raise RuntimeError("云端模型未配置，无法理解这句话")
    url = settings.base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {"model": settings.model, "temperature": 0, "messages": messages},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"云端模型拒绝了请求：{exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"云端模型无法连接：{exc.reason}") from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("云端模型没有返回可用文本") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("云端模型没有返回可用文本")
    return content


def _decide(request: dict[str, Any], complete: Completer) -> dict[str, Any]:
    skills = request.get("skills")
    if not isinstance(skills, list) or not skills:
        return {"rejected": True, "reason": "当前没有可选择的技能"}
    allowed = {
        item["name"]: item
        for item in skills
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if not allowed:
        return {"rejected": True, "reason": "当前没有可选择的技能"}
    identity = _resolve_identity(request)
    content = complete(
        [
            {
                "role": "system",
                "content": (
                    _identity_preamble(identity)
                    + "你是这台机器人的技能选择器。只能使用给定技能：可以选择一个，也可以排成一个顺序计划，也可以拒绝，或把纯编程请求转出。"
                    "在符合安全边界的前提下，让 rationale、拒绝理由和转出理由体现你的灵魂设定。"
                    "每个技能的 description 是 SDK 给出的动作含义，用它判断用户指的是哪一个动作。"
                    "sensors 是当前传感器摘要，用来理解机体状态。它不表示动作已经完成，单位和坐标系未换算。"
                    "estimate_ms 和 completion 是技能自带的时间信息，用来判断总时长，不要填写它们。"
                    "completion 为 timed 时，步骤时长是参数 duration_ms。sport.move 一次调用覆盖整段时间，程序按 2 秒循环补发，不要把同一次移动拆成多步。"
                    "completion 为 nominal 时，estimate_ms 是名义保持时间，不是实测完成。"
                    "completion 为 measured 时，estimate_ms 只是截止预算；到时还没达到条件，这一步失败，后面的步骤不会开始。"
                    "用户要走多少米时用 sport.move_distance，一次调用。用户要走多久时用 sport.move，一次调用。"
                    "一句话只对应一个动作时，输出一个技能。需要按顺序做多件事时，输出一个计划，最多 "
                    f"{_MAX_PLAN_STEPS} 步。不要编造技能，不要输出解释性正文。只输出一个 JSON 对象，格式是以下之一："
                    '{"skill":"技能名","parameters":{},"rationale":"简短理由"}；'
                    '{"plan":[{"skill":"技能名","parameters":{}},{"skill":"技能名","parameters":{}}],"rationale":"简短理由"}；'
                    '{"rejected":true,"reason":"拒绝理由"}；'
                    '{"coding_task":"隔离工作区中的编程任务","reason":"为什么这不是机器人动作"}。'
                    "参数必须符合给定类型和范围。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "text": request.get("text"),
                        "skills": list(allowed.values()),
                        "observations": request.get("observations") or [],
                        "sensors": _sensor_context(request.get("sensors")),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    )
    payload = _parse_json_object(content)
    coding_task = payload.get("coding_task")
    if isinstance(coding_task, str) and coding_task.strip():
        return {
            "coding_task": coding_task.strip()[:4000],
            "reason": _text(payload.get("reason")) or "这句话需要改代码，没有调用机器人技能",
        }
    if payload.get("rejected") is True:
        return {"rejected": True, "reason": _text(payload.get("reason")) or "没有对应的已注册技能"}
    plan = payload.get("plan")
    if isinstance(plan, list):
        return _accept_plan(plan, allowed, _text(payload.get("rationale")))
    skill = payload.get("skill")
    if not isinstance(skill, str) or skill not in allowed:
        return {"rejected": True, "reason": "模型选择了未注册的技能"}
    return {
        "skill": skill,
        "parameters": _clean_parameters(allowed[skill], payload.get("parameters")),
        "rationale": _text(payload.get("rationale")) or "已选择已注册技能",
    }


def _clean_parameters(skill: dict[str, Any], parameters: Any) -> dict[str, Any]:
    declared = skill.get("parameters")
    declared_names = set(declared) if isinstance(declared, dict) else set()
    if not isinstance(parameters, dict):
        return {}
    return {key: value for key, value in parameters.items() if key in declared_names}


def _accept_plan(plan: list[Any], allowed: dict[str, dict[str, Any]], rationale: str) -> dict[str, Any]:
    if not plan or len(plan) > _MAX_PLAN_STEPS:
        return {"rejected": True, "reason": f"计划需要 1 到 {_MAX_PLAN_STEPS} 个已注册技能"}
    steps = []
    for item in plan:
        if not isinstance(item, dict):
            return {"rejected": True, "reason": "计划里有无法执行的步骤"}
        skill = item.get("skill")
        if not isinstance(skill, str) or skill not in allowed:
            return {"rejected": True, "reason": "模型选择了未注册的技能"}
        steps.append({"skill": skill, "parameters": _clean_parameters(allowed[skill], item.get("parameters"))})
    return {
        "plan": _collapse_motion_steps(steps, allowed),
        "rationale": rationale or "已按顺序选择已注册技能",
    }


def _parameter_cap(allowed: dict[str, dict[str, Any]], skill: str, name: str) -> float | None:
    declared = allowed.get(skill, {}).get("parameters")
    if not isinstance(declared, dict):
        return None
    parameter = declared.get(name)
    if not isinstance(parameter, dict):
        return None
    cap = parameter.get("max", parameter.get("maximum"))
    if isinstance(cap, bool) or not isinstance(cap, (int, float)):
        return None
    return float(cap)


def _collapse_motion_steps(
    steps: list[dict[str, Any]],
    allowed: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    collapsed: list[dict[str, Any]] = []
    for step in steps:
        key = motion_amount_key(step["skill"])
        previous = collapsed[-1] if collapsed else None
        if (
            key
            and previous is not None
            and previous["skill"] == step["skill"]
            and same_velocity(previous["parameters"], step["parameters"])
            and key in previous["parameters"]
            and key in step["parameters"]
        ):
            previous["parameters"][key] = combined_amount(
                previous["parameters"][key],
                step["parameters"][key],
                key,
                _parameter_cap(allowed, step["skill"], key),
            )
            continue
        collapsed.append(step)
    return collapsed


def _review(request: dict[str, Any], complete: Completer) -> dict[str, Any]:
    identity = _resolve_identity(request)
    content = complete(
        [
            {
                "role": "system",
                "content": (
                    _identity_preamble(identity)
                    + "你是无工具安全复核。不能修改技能或参数，也不能调用工具。"
                    "只判断这次提案现在能否执行。只输出一个 JSON 对象："
                    '{"verdict":"allow"|"deny"|"uncertain","rationale":"理由"}。'
                    "拒绝或不确定时必须写明理由；语气可与灵魂设定一致，但 verdict 必须严格客观。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "skill": request.get("skill"),
                        "parameters": request.get("parameters"),
                        "user_input": request.get("user_input"),
                        "risk_level": request.get("risk_level"),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    )
    payload = _parse_json_object(content)
    verdict = payload.get("verdict")
    if verdict not in {"allow", "deny", "uncertain"}:
        verdict = "uncertain"
    rationale = _text(payload.get("rationale"))
    if verdict != "allow" and not rationale:
        rationale = "复核没有给出拒绝理由，已拒绝执行"
    if verdict == "allow" and not rationale:
        rationale = "复核允许执行"
    return {"verdict": verdict, "rationale": rationale}


def _parse_json_object(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise RuntimeError("云端模型没有返回 JSON 对象")
    return payload


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _resolve_identity(request: dict[str, Any]) -> dict[str, str]:
    payload = request.get("identity")
    if isinstance(payload, dict):
        robot_name = payload.get("robot_name")
        soul = payload.get("soul")
        if isinstance(robot_name, str) and robot_name.strip():
            return {
                "robot_name": robot_name.strip()[:64],
                "soul": soul.strip()[:4000] if isinstance(soul, str) else "",
            }
    if isinstance(_IDENTITY, dict):
        return dict(_IDENTITY)
    return {"robot_name": "机器人", "soul": ""}


def _identity_preamble(identity: dict[str, str]) -> str:
    robot_name = identity.get("robot_name") or "机器人"
    soul = identity.get("soul") or ""
    text = f"你的名字是{robot_name}，你是一台实体机器人。"
    if soul:
        text += f"你的灵魂设定是：{soul}。"
    return text


def _sensor_context(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    readings = [item for item in value if isinstance(item, dict)]
    return readings[:12]


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
