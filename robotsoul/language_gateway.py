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

from robotsoul.settings import LlmConfigStore, LlmSettings


Completer = Callable[[list[dict[str, str]]], str]


def main() -> None:
    path_value = os.environ.get("OPENROBOTSOUL_LLM_CONFIG", "")
    path = Path(path_value) if path_value else None
    config_error = ""
    settings = LlmSettings()
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
    content = complete(
        [
            {
                "role": "system",
                "content": (
                    "你是机器人技能选择器。只能从给定技能里选择一个，拒绝，或把纯编程请求转出。"
                    "每个技能的 description 是 SDK 给出的动作含义，用它判断用户指的是哪一个动作。"
                    "不要编造技能，不要输出解释性正文。只输出一个 JSON 对象，格式是以下之一："
                    '{"skill":"技能名","parameters":{},"rationale":"简短理由"}；'
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
    skill = payload.get("skill")
    if not isinstance(skill, str) or skill not in allowed:
        return {"rejected": True, "reason": "模型选择了未注册的技能"}
    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    declared = allowed[skill].get("parameters")
    declared_names = set(declared) if isinstance(declared, dict) else set()
    cleaned = {key: value for key, value in parameters.items() if key in declared_names}
    return {
        "skill": skill,
        "parameters": cleaned,
        "rationale": _text(payload.get("rationale")) or "已选择已注册技能",
    }


def _review(request: dict[str, Any], complete: Completer) -> dict[str, Any]:
    content = complete(
        [
            {
                "role": "system",
                "content": (
                    "你是无工具安全复核。不能修改技能或参数，也不能调用工具。"
                    "只判断这次提案现在能否执行。只输出一个 JSON 对象："
                    '{"verdict":"allow"|"deny"|"uncertain","rationale":"理由"}。'
                    "拒绝或不确定时必须写明理由。"
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


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
