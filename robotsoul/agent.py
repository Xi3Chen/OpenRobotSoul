from __future__ import annotations

import re
from typing import Protocol

from robotsoul.models import AgentIntent, CapabilitySpec, Observation


class AgentRejected(RuntimeError):
    """The language agent refused the utterance and supplied a user-facing reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason.strip() or "语言进程没有给出拒绝理由"
        super().__init__(self.reason)


class AgentProvider(Protocol):
    def plan(
        self,
        user_input: str,
        capabilities: dict[str, CapabilitySpec],
        observations: tuple[Observation, ...] = (),
    ) -> AgentIntent | None:
        """Return a bounded high-level plan; providers must enforce their own request timeout."""


_SLASH_COMMAND = re.compile(r"^/(\S+)(?:\s+(.*))?$", re.DOTALL)
_SLASH_ARGUMENT = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"]*)"|(\S+))')


class DemoAgent:
    """Offline stand-in. Free text is not keyword-matched; slash commands are handled by the runtime."""

    def plan(
        self,
        user_input: str,
        capabilities: dict[str, CapabilitySpec],
        observations: tuple[Observation, ...] = (),
    ) -> AgentIntent | None:
        return None


def resolve_slash_command(user_input: str, capabilities: dict[str, CapabilitySpec]) -> AgentIntent | str | None:
    """Resolve `/skill` or `/skill name=value` locally.

    Returns an intent, a user-facing error, or None when the text is not a slash command.
    """

    stripped = user_input.strip()
    if not stripped.startswith("/"):
        return None
    matched = _SLASH_COMMAND.fullmatch(stripped)
    if matched is None:
        return "直接指令无效。格式是 /技能名，或 /技能名 参数=值。"
    token, rest = matched.group(1), (matched.group(2) or "").strip()
    skill = _resolve_skill_token(token, capabilities)
    if isinstance(skill, str) and skill not in capabilities:
        return skill
    if not isinstance(skill, str):
        return "直接指令无效。格式是 /技能名，或 /技能名 参数=值。"
    try:
        parameters = _parse_slash_parameters(rest, capabilities[skill])
    except ValueError as exc:
        return str(exc)
    return AgentIntent(skill, parameters, "Direct slash command")


def _resolve_skill_token(token: str, capabilities: dict[str, CapabilitySpec]) -> str:
    normalized = token.replace("-", "_")
    if normalized in capabilities:
        return normalized
    matches = [
        name
        for name in capabilities
        if name.split(".")[-1] == normalized
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return f"直接指令不唯一：/{token}"
    return f"没有这个直接指令：/{token}"


def _parse_slash_parameters(rest: str, capability: CapabilitySpec) -> dict[str, object]:
    if not rest:
        provided: dict[str, object] = {}
    else:
        provided = {}
        cursor = 0
        for match in _SLASH_ARGUMENT.finditer(rest):
            if rest[cursor:match.start()].strip():
                raise ValueError("直接指令参数必须写成 参数=值。")
            key = match.group(1)
            if key not in capability.parameters:
                raise ValueError(f"技能没有参数 {key}")
            if key in provided:
                raise ValueError(f"参数 {key} 重复了")
            raw = match.group(2) if match.group(2) is not None else match.group(3)
            provided[key] = _coerce_slash_value(raw, capability.parameters[key])
            cursor = match.end()
        if rest[cursor:].strip():
            raise ValueError("直接指令参数必须写成 参数=值。")
    missing = [name for name in capability.parameters if name not in provided]
    if missing:
        rendered = "、".join(f"{name}=…" for name in missing)
        raise ValueError(f"直接指令缺少参数：{rendered}")
    return provided


def _coerce_slash_value(raw: str, spec: object) -> object:
    value_type = getattr(spec, "value_type", "")
    if value_type == "boolean":
        lowered = raw.lower()
        if lowered in {"true", "on", "yes"}:
            return True
        if lowered in {"false", "off", "no"}:
            return False
        raise ValueError(f"参数必须是 true 或 false：{raw}")
    if value_type == "number":
        try:
            if re.fullmatch(r"[+-]?\d+", raw):
                return int(raw)
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"参数必须是数字：{raw}") from exc
    return raw
