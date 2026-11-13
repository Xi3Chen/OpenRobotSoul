"""DeepSeek-backed agent provider for OpenRobotSoul.

Uses the official DeepSeek API (OpenAI-compatible chat completions) with the
``deepseek-flash`` model (DeepSeek-V4.1-Flash).  The agent stays bounded: the
model may only choose one of the capabilities the runtime advertises, and every
returned parameter is checked against the declared schema before the intent is
handed back to the runtime.

Configuration (environment or constructor arguments):

* ``DEEPSEEK_API_KEY`` — required; the key is never logged or echoed.
* ``ROBOTSOUL_LLM_MODEL`` — optional, defaults to ``deepseek-flash``.
* ``ROBOTSOUL_LLM_BASE_URL`` — optional, defaults to ``https://api.deepseek.com``.

The provider returns ``None`` when the model answers with no matching skill or
with an intent that does not match the advertised schema.  Transport failures
raise ``DeepSeekAgentError`` and an overall timeout raises ``TimeoutError`` so
the runtime can report a precise task error.
"""

from __future__ import annotations

import json
import math
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Callable

from robotsoul.models import AgentIntent, CapabilitySpec, Observation

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT_SECONDS = 20.0

Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]

_SYSTEM_PROMPT = (
    "你是 OpenRobotSoul 的受限指令规划器。你只能从给定的 capabilities 中选择一个技能，"
    "并按参数 schema 给出参数。只输出一个 JSON 对象，格式为 "
    '{"skill": <技能名或 null>, "parameters": {<参数名>: <值>}, "rationale": <简短说明>}。'
    "如果用户指令无法用给定技能完成，把 skill 设为 null 并且 parameters 设为 {}。"
    "不要编造技能名、参数名或参数类型。"
)


class DeepSeekAgentError(RuntimeError):
    """The DeepSeek endpoint was unreachable or returned an unsupported answer."""


def _default_transport(url: str, headers: dict[str, str], payload: bytes, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()[:4096]


class DeepSeekAgent:
    """Bounded agent that asks DeepSeek Flash to pick one advertised skill."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: Transport | None = None,
        temperature: float = 0.0,
        max_tokens: int = 256,
    ) -> None:
        key = (api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")).strip()
        if not key:
            raise ValueError("DEEPSEEK_API_KEY is required for the DeepSeek agent")
        self._api_key = key
        self.model = (model or os.environ.get("ROBOTSOUL_LLM_MODEL") or DEFAULT_MODEL).strip()
        self.base_url = (base_url or os.environ.get("ROBOTSOUL_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self._transport = transport or _default_transport

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def plan(
        self,
        user_input: str,
        capabilities: dict[str, CapabilitySpec],
        observations: tuple[Observation, ...] = (),
    ) -> AgentIntent | None:
        if not isinstance(user_input, str) or not user_input.strip():
            return None
        if not capabilities:
            return None
        payload = json.dumps(self._request_body(user_input, capabilities), ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            status, body = self._transport(self.endpoint, headers, payload, self.timeout)
        except (socket.timeout, TimeoutError) as exc:
            raise TimeoutError(f"DeepSeek request timed out after {self.timeout:.0f}s") from exc
        except urllib.error.URLError as exc:
            raise DeepSeekAgentError(f"DeepSeek endpoint is unreachable: {exc.reason}") from exc
        except OSError as exc:
            raise DeepSeekAgentError(f"DeepSeek request failed: {exc}") from exc
        if status != 200:
            raise DeepSeekAgentError(f"DeepSeek API returned HTTP {status}")
        return self._extract_intent(body, capabilities)

    def _request_body(self, user_input: str, capabilities: dict[str, CapabilitySpec]) -> dict[str, Any]:
        schema = []
        for name, spec in capabilities.items():
            schema.append({
                "name": name,
                "risk_level": spec.risk_level.value,
                "parameters": {
                    param: spec_.value_type for param, spec_ in spec.parameters.items()
                },
            })
        user = "capabilities: " + json.dumps(schema, ensure_ascii=False) + "\n用户指令: " + user_input
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }

    def _extract_intent(self, body: bytes, capabilities: dict[str, CapabilitySpec]) -> AgentIntent | None:
        try:
            data = json.loads(body or b"{}")
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise DeepSeekAgentError(f"DeepSeek returned an unexpected response: {exc}") from exc
        if not isinstance(content, str) or not content.strip():
            return None
        try:
            parsed = json.loads(content)
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        skill = parsed.get("skill")
        if not isinstance(skill, str) or not skill:
            return None
        capability = capabilities.get(skill)
        if capability is None:
            return None
        parameters = parsed.get("parameters") or {}
        if not isinstance(parameters, dict):
            return None
        if set(parameters) != set(capability.parameters):
            return None
        for name, spec in capability.parameters.items():
            value = parameters[name]
            if spec.value_type == "boolean":
                if not isinstance(value, bool):
                    return None
            elif spec.value_type == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    return None
                if spec.minimum is not None and value < spec.minimum:
                    return None
                if spec.maximum is not None and value > spec.maximum:
                    return None
            elif spec.value_type == "string":
                if not isinstance(value, str):
                    return None
                if spec.max_length is not None and len(value) > spec.max_length:
                    return None
            else:
                return None
        rationale = parsed.get("rationale")
        return AgentIntent(skill, parameters, rationale if isinstance(rationale, str) else "")
