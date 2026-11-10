from __future__ import annotations

import json
import math
import os
import re
from copy import deepcopy
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Protocol, Sequence
from uuid import uuid4

from robotsoul.models import CapabilitySpec, Command, Feedback, FeedbackStatus


DEFAULT_TOOL_CALL_LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "tool_calls.jsonl"

_SENSITIVE_KEY = re.compile(r"password|passwd|secret|token|credential|authorization|api[_-]?key", re.I)
_ASSIGNMENT_SECRET = re.compile(
    r"\b(password|passwd|secret|token|credential|api[_-]?key)\b\s*[:=]\s*[^\s,;]+",
    re.I,
)
_BEARER_SECRET = re.compile(r"\bBearer\s+[^\s,;]+", re.I)
_FILE_LOCK = Lock()
_MAX_STRING_LENGTH = 2048
_MAX_COLLECTION_ITEMS = 100


class ToolCallAuditSink(Protocol):
    def write(self, event: Mapping[str, Any]) -> None: ...


class NullToolCallAuditSink:
    def write(self, event: Mapping[str, Any]) -> None:
        return None


class InMemoryToolCallAuditSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._lock = Lock()

    def write(self, event: Mapping[str, Any]) -> None:
        with self._lock:
            self.events.append(deepcopy(dict(event)))


class JsonlToolCallAuditSink:
    """Append durable, one-event-per-line tool call records."""

    def __init__(self, path: str | Path = DEFAULT_TOOL_CALL_LOG_PATH) -> None:
        self.path = Path(path)

    def write(self, event: Mapping[str, Any]) -> None:
        payload = (json.dumps(dict(event), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with _FILE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("Tool call audit write made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def safe_tool_arguments(command: Command, capability: CapabilitySpec) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for name, value in command.parameters.items():
        if _SENSITIVE_KEY.search(name):
            safe[name] = {"redacted": True}
            continue
        spec = capability.parameters.get(name)
        if spec is None:
            safe[name] = {"redacted": True, "value_type": type(value).__name__}
        elif spec.value_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            safe[name] = value if math.isfinite(value) else {"invalid_number": True}
        elif isinstance(value, bool):
            safe[name] = value
        elif isinstance(value, str):
            safe[name] = {"redacted": True, "length": len(value)}
        else:
            safe[name] = {"redacted": True, "value_type": type(value).__name__}
    return safe


def summarize_feedback(
    feedback: Any,
    private_values: Sequence[str] = (),
) -> dict[str, Any]:
    if not isinstance(feedback, (tuple, list)):
        return {"invalid_result_type": type(feedback).__name__}

    items = []
    for item in feedback[:_MAX_COLLECTION_ITEMS]:
        if not isinstance(item, Feedback):
            items.append({"invalid_item_type": type(item).__name__})
            continue
        items.append(
            {
                "feedback_id": item.feedback_id,
                "status": item.status.value if isinstance(item.status, FeedbackStatus) else str(item.status),
                "timestamp": _json_datetime(item.timestamp),
                "message": (
                    _safe_text(item.message, private_values)
                    if isinstance(item.message, str)
                    else _safe_value("message", item.message, private_values)
                ),
                "error_code": _safe_value("error_code", item.error_code, private_values),
                "evidence": _safe_value("evidence", item.evidence, private_values),
            }
        )
    result: dict[str, Any] = {"feedback": items}
    if len(feedback) > _MAX_COLLECTION_ITEMS:
        result["truncated_items"] = len(feedback) - _MAX_COLLECTION_ITEMS
    return result


def safe_exception(error: BaseException, private_values: Sequence[str] = ()) -> dict[str, str]:
    return {
        "type": type(error).__name__,
        "message": _safe_text(str(error), private_values),
    }


def tool_call_event(
    phase: str,
    command: Command,
    capability: CapabilitySpec | None,
    arguments: Mapping[str, Any],
    timestamp: datetime,
    *,
    call_id: str | None = None,
    method: str = "adapter.execute",
    duration_ms: float | None = None,
    outcome: str | None = None,
    response: Mapping[str, Any] | None = None,
    error: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    resolved_call_id = call_id if call_id is not None else str(uuid4())
    event: dict[str, Any] = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "call_id": resolved_call_id,
        "phase": phase,
        "timestamp": timestamp.isoformat(),
        "task_id": command.task_id,
        "trace_id": command.trace_id,
        "command_id": command.command_id,
        "robot_id": command.robot_id,
        "tool": command.skill,
        "method": method,
        "deadline": command.deadline.isoformat(),
        "arguments": deepcopy(dict(arguments)),
        "risk_level": command.risk_level.value,
        "invocation_source": command.invocation_source.value,
        "operator_confirmed": command.operator_confirmed,
    }
    if command.review_digest is not None:
        event["review_digest"] = command.review_digest
    if command.policy_version is not None:
        event["policy_version"] = command.policy_version
    if command.actor_hash is not None:
        event["actor_hash"] = command.actor_hash
    if capability is not None:
        event["timeout_ms"] = capability.timeout_ms
        event["interruptible"] = capability.interruptible
    if duration_ms is not None:
        event["duration_ms"] = round(duration_ms, 3)
    if outcome is not None:
        event["outcome"] = outcome
    if response is not None:
        event["response"] = deepcopy(dict(response))
    if error is not None:
        event["error"] = deepcopy(dict(error))
    return event


def private_command_values(command: Command) -> tuple[str, ...]:
    return tuple(value for value in command.parameters.values() if isinstance(value, str))


def _safe_value(key: str, value: Any, private_values: Sequence[str], depth: int = 0) -> Any:
    if _SENSITIVE_KEY.search(key):
        return {"redacted": True}
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, datetime):
        return _json_datetime(value)
    if isinstance(value, Enum):
        return _safe_text(str(value.value), private_values)
    if isinstance(value, str):
        return _safe_text(value, private_values)
    if isinstance(value, bytes):
        return {"omitted_bytes": len(value)}
    if depth >= 6:
        return {"omitted": True, "value_type": type(value).__name__}
    if isinstance(value, Mapping):
        items = list(value.items())[:_MAX_COLLECTION_ITEMS]
        safe = {str(item_key): _safe_value(str(item_key), item_value, private_values, depth + 1)
                for item_key, item_value in items}
        if len(value) > _MAX_COLLECTION_ITEMS:
            safe["_truncated_items"] = len(value) - _MAX_COLLECTION_ITEMS
        return safe
    if isinstance(value, (tuple, list)):
        safe_items = [_safe_value(key, item, private_values, depth + 1)
                      for item in value[:_MAX_COLLECTION_ITEMS]]
        if len(value) > _MAX_COLLECTION_ITEMS:
            return {"items": safe_items, "truncated_items": len(value) - _MAX_COLLECTION_ITEMS}
        return safe_items
    return {"omitted": True, "value_type": type(value).__name__}


def _safe_text(value: str, private_values: Sequence[str]) -> str:
    safe = value
    for private_value in private_values:
        if private_value:
            safe = safe.replace(private_value, "[REDACTED]")
    safe = _ASSIGNMENT_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", safe)
    safe = _BEARER_SECRET.sub("Bearer [REDACTED]", safe)
    if len(safe) > _MAX_STRING_LENGTH:
        safe = safe[:_MAX_STRING_LENGTH] + "...[truncated]"
    return safe


def _json_datetime(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None
