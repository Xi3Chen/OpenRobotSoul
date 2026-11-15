from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from math import isfinite
import json
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(StrEnum):
    RECEIVED = "received"
    CONTEXTUALIZING = "contextualizing"
    AWAITING_AGENT = "awaiting_agent"
    VALIDATING_INTENT = "validating_intent"
    REVIEWING = "reviewing"
    EXECUTING = "executing"
    MONITORING = "monitoring"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_EVENT = "waiting_event"
    AWAITING_INPUT = "awaiting_input"
    CANCEL_REQUESTED = "cancel_requested"
    INTERRUPTED = "interrupted"


class FeedbackStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(StrEnum):
    NORMAL = "normal"
    CONFIRM = "confirm"
    HIGH = "high"


class InvocationSource(StrEnum):
    AGENT = "agent"
    DASHBOARD = "dashboard"
    MAINTENANCE = "maintenance"


class ReviewVerdict(StrEnum):
    PASS = "pass"
    NEEDS_EVIDENCE = "needs_evidence"
    ALLOW = "allow"
    DENY = "deny"
    UNCERTAIN = "uncertain"

    @property
    def is_pass(self) -> bool:
        return self in {ReviewVerdict.PASS, ReviewVerdict.ALLOW}

    @property
    def is_needs_evidence(self) -> bool:
        return self in {ReviewVerdict.NEEDS_EVIDENCE, ReviewVerdict.UNCERTAIN}


@dataclass(frozen=True)
class EvidenceRequirement:
    """A bounded fact that must be true before a reviewed action can start.

    Requirements are deliberately geometric only at the boundary: the model
    names a direction and range, while the local evidence table decides whether
    the current sensor/human evidence satisfies it.
    """

    direction: str
    quantity: int = 0
    range_m: float = 1.0
    valid_for_seconds: float = 10.0
    evidence_type: str = "clearance"

    def __post_init__(self) -> None:
        if not isinstance(self.direction, str) or not self.direction.strip():
            raise ValueError("evidence direction must be non-empty text")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity < 0:
            raise ValueError("evidence quantity must be a non-negative integer")
        if isinstance(self.range_m, bool) or not isinstance(self.range_m, (int, float)) or not isfinite(float(self.range_m)) or self.range_m <= 0:
            raise ValueError("evidence range_m must be a positive finite number")
        if isinstance(self.valid_for_seconds, bool) or not isinstance(self.valid_for_seconds, (int, float)) or not isfinite(float(self.valid_for_seconds)) or self.valid_for_seconds <= 0:
            raise ValueError("evidence valid_for_seconds must be a positive finite number")
        if not isinstance(self.evidence_type, str) or not self.evidence_type.strip():
            raise ValueError("evidence_type must be non-empty text")

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "quantity": self.quantity,
            "range_m": float(self.range_m),
            "valid_for_seconds": float(self.valid_for_seconds),
            "evidence_type": self.evidence_type,
        }


@dataclass(frozen=True)
class ExecutionCondition:
    """A deterministic condition attached to an otherwise approved command."""

    name: str
    evidence: EvidenceRequirement | None = None
    minimum_battery_percent: int | None = None
    max_speed_mps: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("execution condition name must be non-empty text")
        if self.minimum_battery_percent is not None and (
            isinstance(self.minimum_battery_percent, bool)
            or not isinstance(self.minimum_battery_percent, int)
            or not 0 <= self.minimum_battery_percent <= 100
        ):
            raise ValueError("minimum_battery_percent must be an integer percent")
        if self.max_speed_mps is not None and (
            isinstance(self.max_speed_mps, bool)
            or not isinstance(self.max_speed_mps, (int, float))
            or not isfinite(float(self.max_speed_mps))
            or self.max_speed_mps <= 0
        ):
            raise ValueError("max_speed_mps must be a positive finite number")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"name": self.name}
        if self.evidence is not None:
            value["evidence"] = self.evidence.to_dict()
        if self.minimum_battery_percent is not None:
            value["minimum_battery_percent"] = self.minimum_battery_percent
        if self.max_speed_mps is not None:
            value["max_speed_mps"] = float(self.max_speed_mps)
        return value


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
    risk_level: RiskLevel = RiskLevel.NORMAL
    allowed_callers: tuple[str, ...] = ("agent", "dashboard")
    requires_operator_confirmation: bool = False
    interruptible: bool = True
    timeout_ms: int = 5000
    required: bool = False
    description: str = ""
    estimate_ms: int = 0
    completion: str = "accepted"
    retry_safe: bool = False

    @property
    def safety_class(self) -> str:
        """Compatibility label for integrations that still use the old field."""
        return "low" if self.risk_level == RiskLevel.NORMAL else self.risk_level.value


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
class AgentPlan:
    """Ordered skill steps. Timing stays on the skill, not in this object."""

    steps: tuple[AgentIntent, ...]
    rationale: str = ""


@dataclass(frozen=True)
class InvocationContext:
    source: InvocationSource
    principal_id: str | None = None
    session_id: str | None = None
    role: str | None = None
    operator_confirmed: bool = False
    principal_hash: str | None = None


@dataclass(frozen=True)
class ReviewProposal:
    proposal_id: str
    command: Command
    risk_level: RiskLevel
    policy_version: str
    arguments_digest: str
    created_at: datetime
    expires_at: datetime
    user_input: str


@dataclass(frozen=True)
class SafetyReviewResult:
    verdict: ReviewVerdict
    arguments_digest: str
    rationale: str
    evidence_requirements: tuple[EvidenceRequirement, ...] = ()
    conditions: tuple[ExecutionCondition, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, ReviewVerdict):
            raise ValueError("review verdict must be a ReviewVerdict")
        if not isinstance(self.arguments_digest, str) or not self.arguments_digest:
            raise ValueError("review arguments_digest must be non-empty text")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("review rationale must be non-empty text")
        for item in (*self.evidence_requirements, *self.conditions):
            if not isinstance(item, (EvidenceRequirement, ExecutionCondition)):
                raise ValueError("review conditions contain an invalid item")

    @property
    def passed(self) -> bool:
        return self.verdict.is_pass

    @property
    def needs_evidence(self) -> bool:
        return self.verdict.is_needs_evidence


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
    risk_level: RiskLevel = RiskLevel.NORMAL
    invocation_source: InvocationSource = InvocationSource.AGENT
    review_digest: str | None = None
    policy_version: str | None = None
    operator_confirmation_id: str | None = None
    operator_confirmed: bool = False
    actor_hash: str | None = None


def command_proposal_digest(command: Command, policy_version: str) -> str:
    payload = {
        "robot_id": command.robot_id,
        "task_id": command.task_id,
        "trace_id": command.trace_id,
        "skill": command.skill,
        "parameters": command.parameters,
        "risk_level": command.risk_level.value,
        "policy_version": policy_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


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
    reply: str = ""


@dataclass(frozen=True)
class ConversationContext:
    """History partition, deliberately separate from an authentication session."""

    robot_id: str
    channel: str
    principal_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        for name in ("robot_id", "channel", "principal_id", "conversation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"{name} must contain 1 to 256 characters")

    @property
    def session_key(self) -> str:
        # Hash a canonical encoding before PicoClaw's lower-casing key helper.
        encoded = json.dumps(self.__dict__, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sk_v1_" + sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskContext:
    task: Task
    conversation: ConversationContext
    observations: tuple[Observation, ...] = ()
    feedback: tuple[Feedback, ...] = ()
    decision_count: int = 0
    resume_input: str = ""
    history: tuple[AuditRecord, ...] = ()
    result_evaluators: tuple[dict[str, str], ...] = ()
    goal_kind: str = "general"
    expected_evaluator: str | None = None


@dataclass(frozen=True)
class AgentDecision:
    """One bounded high-level decision; it never grants device permissions."""

    kind: str
    steps: tuple[AgentIntent, ...] = ()
    reply: str = ""
    rule: dict[str, Any] | None = None
    wait: dict[str, Any] | None = None
    evidence_refs: tuple[str, ...] = ()
    decision_id: str = ""
    evaluator: str = "skill_completion"
    coding_task: str = ""
    legacy_coding: bool = False
