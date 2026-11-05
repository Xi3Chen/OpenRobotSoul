from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(StrEnum):
    RECEIVED = "received"
    CONTEXTUALIZING = "contextualizing"
    AWAITING_AGENT = "awaiting_agent"
    VALIDATING_INTENT = "validating_intent"
    EXECUTING = "executing"
    MONITORING = "monitoring"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FeedbackStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ParameterSpec:
    value_type: str
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = None


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    parameters: dict[str, ParameterSpec]
    safety_class: str = "low"
    interruptible: bool = True
    timeout_ms: int = 5000
    required: bool = False


@dataclass(frozen=True)
class Observation:
    event_id: str
    robot_id: str
    device_id: str
    event_type: str
    observed_at: datetime
    received_at: datetime
    payload: dict[str, Any]
    source: str
    ttl_ms: int
    privacy_class: str = "operational"

    @property
    def is_fresh(self) -> bool:
        age_ms = (utc_now() - self.observed_at).total_seconds() * 1000
        return 0 <= age_ms <= self.ttl_ms


@dataclass(frozen=True)
class AgentIntent:
    skill: str
    parameters: dict[str, Any]
    rationale: str = ""


@dataclass(frozen=True)
class Command:
    command_id: str
    task_id: str
    trace_id: str
    robot_id: str
    skill: str
    parameters: dict[str, Any]
    deadline: datetime
    cancel_policy: str = "cancel_on_timeout"


@dataclass(frozen=True)
class Feedback:
    feedback_id: str
    command_id: str
    task_id: str
    trace_id: str
    status: FeedbackStatus
    timestamp: datetime
    message: str = ""
    error_code: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    task_id: str
    trace_id: str
    robot_id: str
    user_input: str
    status: TaskStatus = TaskStatus.RECEIVED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    error: str | None = None


@dataclass(frozen=True)
class Evaluation:
    task_id: str
    trace_id: str
    success: bool
    conclusion: str
    evidence: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class AuditRecord:
    timestamp: datetime
    event_type: str
    task_id: str
    trace_id: str
    details: dict[str, Any]


@dataclass(frozen=True)
class TaskResult:
    task: Task
    feedback: tuple[Feedback, ...]
    evaluation: Evaluation | None
    history: tuple[AuditRecord, ...]
