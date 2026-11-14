from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import json
from hashlib import sha256
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Any
from uuid import uuid4

from robotsoul.audit import (
    DEFAULT_TOOL_CALL_LOG_PATH,
    JsonlToolCallAuditSink,
    ToolCallAuditSink,
    private_command_values,
    safe_exception,
    safe_tool_arguments,
    summarize_feedback,
    tool_call_event,
)
from robotsoul.adapters.base import RobotAdapter
from robotsoul.adapters.registry import create_adapter
from robotsoul.agent import AgentProvider, AgentRejected, DemoAgent, resolve_slash_command
from robotsoul.runlog import log as runtime_log
from robotsoul.models import (
    AgentIntent,
    AgentPlan,
    AuditRecord,
    CapabilitySpec,
    Command,
    Evaluation,
    Feedback,
    FeedbackStatus,
    InvocationContext,
    InvocationSource,
    Observation,
    ReviewProposal,
    ReviewVerdict,
    RiskLevel,
    SafetyReviewResult,
    command_proposal_digest,
    Task,
    TaskResult,
    TaskStatus,
    ParameterSpec,
    utc_now,
)
from robotsoul.motion_plan import combined_amount, motion_amount_key, same_velocity
from robotsoul.profiles import RobotProfile


_REVIEW_WINDOW = timedelta(seconds=120)
_PLAN_MARGIN_MS = 2000
_MAX_PLAN_STEPS = 8


def step_allowance_ms(capability: CapabilitySpec, parameters: dict[str, Any]) -> int:
    """Return the command budget for one step.

    Timed steps use the requested duration. Nominal and measured steps use the
    skill's own estimate. The model does not supply this number.
    """

    completion = capability.completion
    estimate_ms = capability.estimate_ms
    if completion == "timed":
        raw = parameters.get("duration_ms", estimate_ms)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            duration = estimate_ms
        else:
            duration = int(raw)
        return max(capability.timeout_ms, duration + 5500)
    if completion == "nominal":
        return max(capability.timeout_ms, estimate_ms + 4500)
    if completion == "measured":
        return _measured_budget_ms(parameters, capability.timeout_ms, estimate_ms)
    return capability.timeout_ms


def _measured_budget_ms(parameters: dict[str, Any], timeout_ms: int, estimate_ms: int) -> int:
    distance = parameters.get("distance_m")
    speed = parameters.get("vx")
    if isinstance(distance, bool) or not isinstance(distance, (int, float)):
        return max(timeout_ms, estimate_ms)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or abs(float(speed)) < 0.2:
        return max(timeout_ms, estimate_ms)
    budget = int(abs(float(distance)) / abs(float(speed)) * 2000) + 5000
    return min(max(budget, 3000), timeout_ms)


def _coerce_steps(intent: Any) -> tuple[tuple[AgentIntent, ...], str | None]:
    if isinstance(intent, AgentPlan):
        if not intent.steps or len(intent.steps) > _MAX_PLAN_STEPS:
            return (), f"Agent plan must contain 1 to {_MAX_PLAN_STEPS} steps"
        if any(not isinstance(step, AgentIntent) for step in intent.steps):
            return (), "Agent returned an invalid plan"
        return intent.steps, None
    if isinstance(intent, AgentIntent):
        return (intent,), None
    return (), "Agent returned an invalid intent"


def _collapse_repeated_motion(
    steps: tuple[AgentIntent, ...],
    capabilities: dict[str, CapabilitySpec],
) -> tuple[AgentIntent, ...]:
    collapsed: list[AgentIntent] = []
    for step in steps:
        key = motion_amount_key(step.skill)
        previous = collapsed[-1] if collapsed else None
        if (
            key
            and previous is not None
            and previous.skill == step.skill
            and isinstance(previous.parameters, dict)
            and isinstance(step.parameters, dict)
            and same_velocity(previous.parameters, step.parameters)
            and key in previous.parameters
            and key in step.parameters
        ):
            spec = capabilities.get(step.skill)
            parameter = spec.parameters.get(key) if spec is not None else None
            cap = parameter.maximum if parameter is not None else None
            amount = combined_amount(previous.parameters[key], step.parameters[key], key, cap)
            collapsed[-1] = AgentIntent(
                previous.skill,
                {**previous.parameters, key: amount},
                previous.rationale,
            )
            continue
        collapsed.append(step)
    return tuple(collapsed)


class SafetyReviewer:
    """Tool-free reviewer contract for immutable confirm-tier proposals."""

    def review(self, proposal: ReviewProposal) -> SafetyReviewResult:
        raise NotImplementedError


def _trace(level: str, task_id: str, message: str) -> None:
    runtime_log("Runtime", level, f"id={task_id} {message}")


def _authenticated_dashboard_operator(context: InvocationContext) -> bool:
    return (
        context.source == InvocationSource.DASHBOARD
        and context.role == "operator"
        and bool(context.principal_id)
        and bool(context.session_id)
    )


@dataclass
class RobotRuntime:
    profile: RobotProfile
    adapter: RobotAdapter
    agent: AgentProvider
    capabilities: dict[str, CapabilitySpec]
    audit_sink: ToolCallAuditSink = field(default_factory=JsonlToolCallAuditSink, repr=False)
    safety_reviewer: SafetyReviewer | None = field(default=None, repr=False)
    _active_commands: dict[str, Command] = field(default_factory=dict, init=False, repr=False)
    _cancel_confirmations: dict[str, Feedback] = field(default_factory=dict, init=False, repr=False)
    _seen_event_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _used_task_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _preempted_task_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _used_review_bindings: set[str] = field(default_factory=set, init=False, repr=False)
    _runtime_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def from_profile(
        cls,
        profile: RobotProfile,
        agent: AgentProvider | None = None,
        adapter: RobotAdapter | None = None,
        audit_sink: ToolCallAuditSink | None = None,
        safety_reviewer: SafetyReviewer | None = None,
    ) -> "RobotRuntime":
        active_adapter = adapter if adapter is not None else create_adapter(profile)
        if adapter is not None and agent is None:
            active_adapter.close()
            raise ValueError("An explicit AgentProvider is required with a custom robot adapter")
        if profile.robot_module != "simulator" and agent is None:
            active_adapter.close()
            raise ValueError("An explicit AgentProvider is required with a hardware robot module")
        active_adapter.connect()
        try:
            if not active_adapter.health():
                raise RuntimeError("Robot adapter failed its health check")
            manufacturer, model = active_adapter.identity()
            if (manufacturer, model) != (profile.manufacturer, profile.model):
                raise RuntimeError("Connected robot identity does not match the selected profile")
            if active_adapter.robot_id != profile.robot_id:
                raise RuntimeError("Connected robot ID does not match the selected profile")
            discovered = active_adapter.discover_capabilities()
        except Exception:
            active_adapter.close()
            raise
        for name in profile.required_capabilities:
            if name not in discovered:
                active_adapter.close()
                raise RuntimeError(f"Required capability is unavailable: {name}")
        declared_capabilities = {capability.name: capability for capability in profile.capabilities}
        effective: dict[str, CapabilitySpec] = {}
        for name, discovered_spec in discovered.items():
            declared_spec = declared_capabilities.get(name)
            if declared_spec is None:
                continue
            effective_spec = cls._constrain_capability(declared_spec, discovered_spec)
            if effective_spec is not None:
                effective[name] = effective_spec
        for name in profile.required_capabilities:
            if name not in effective:
                active_adapter.close()
                raise RuntimeError(f"Required capability is incompatible: {name}")
        return cls(
            profile,
            active_adapter,
            agent if agent is not None else DemoAgent(),
            effective,
            audit_sink if audit_sink is not None else JsonlToolCallAuditSink(DEFAULT_TOOL_CALL_LOG_PATH),
            safety_reviewer,
        )

    @staticmethod
    def _constrain_capability(
        declared: CapabilitySpec,
        discovered: CapabilitySpec,
    ) -> CapabilitySpec | None:
        if declared.risk_level != discovered.risk_level:
            return None
        if set(declared.parameters) != set(discovered.parameters):
            return None
        parameters = {}
        for name, declared_parameter in declared.parameters.items():
            discovered_parameter = discovered.parameters[name]
            if declared_parameter.value_type != discovered_parameter.value_type:
                return None
            minimums = [
                value
                for value in (declared_parameter.minimum, discovered_parameter.minimum)
                if value is not None
            ]
            maximums = [
                value
                for value in (declared_parameter.maximum, discovered_parameter.maximum)
                if value is not None
            ]
            minimum = max(minimums) if minimums else None
            maximum = min(maximums) if maximums else None
            if minimum is not None and maximum is not None and minimum > maximum:
                return None
            lengths = [
                value
                for value in (declared_parameter.max_length, discovered_parameter.max_length)
                if value is not None
            ]
            parameters[name] = ParameterSpec(
                value_type=declared_parameter.value_type,
                minimum=minimum,
                maximum=maximum,
                max_length=min(lengths) if lengths else None,
            )
        allowed_callers = tuple(
            caller for caller in declared.allowed_callers if caller in discovered.allowed_callers
        )
        if not allowed_callers:
            return None
        return CapabilitySpec(
            name=declared.name,
            parameters=parameters,
            risk_level=declared.risk_level,
            allowed_callers=allowed_callers,
            requires_operator_confirmation=(
                declared.requires_operator_confirmation or discovered.requires_operator_confirmation
            ),
            interruptible=declared.interruptible and discovered.interruptible,
            timeout_ms=min(declared.timeout_ms, discovered.timeout_ms),
            required=declared.required,
            description=declared.description,
            estimate_ms=discovered.estimate_ms or declared.estimate_ms,
            completion=discovered.completion if discovered.estimate_ms or discovered.completion != "accepted" else declared.completion,
        )

    def available_tools(self) -> dict[str, dict[str, Any]]:
        """Return the constrained tool surface exposed to an agent provider."""
        return {
            name: {
                "parameters": {
                    parameter: {
                        "type": spec.value_type,
                        "min": spec.minimum,
                        "max": spec.maximum,
                        "max_length": spec.max_length,
                    }
                    for parameter, spec in capability.parameters.items()
                },
                "timeout_ms": capability.timeout_ms,
                "estimate_ms": capability.estimate_ms,
                "completion": capability.completion,
                "interruptible": capability.interruptible,
            }
            for name, capability in self.capabilities.items()
            if "agent" in capability.allowed_callers and capability.risk_level != RiskLevel.HIGH
        }

    def dashboard_tools(self) -> dict[str, CapabilitySpec]:
        return {
            name: capability
            for name, capability in self.capabilities.items()
            if "dashboard" in capability.allowed_callers
        }

    def close(self) -> None:
        """Release adapter resources once the runtime no longer accepts work."""
        with self._runtime_lock:
            if self._closed:
                return
            self._closed = True
        self.adapter.close()

    def invoke_skill(
        self,
        skill: str,
        parameters: dict[str, Any],
        *,
        invocation_context: InvocationContext,
        operator_confirmed: bool = False,
        user_input: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> TaskResult:
        """Invoke a typed skill using a caller context created by the service.

        Dashboard integrations must derive caller and role from the authenticated
        server session, never from client request fields.
        """
        if not isinstance(invocation_context, InvocationContext):
            raise TypeError("invocation_context must be a server-created InvocationContext")
        validation_error = self.validate_skill_request(skill, parameters, invocation_context)
        if validation_error is not None:
            return self._invocation_failure(skill, validation_error)
        resolved_input = user_input if user_input is not None else f"Dashboard operation: {skill}"
        return self.run_text(
            resolved_input,
            task_id=task_id,
            trace_id=trace_id,
            _intent=AgentIntent(skill, parameters),
            _invocation_context=invocation_context,
            _operator_confirmed=operator_confirmed,
        )

    def validate_skill_request(
        self,
        skill: str,
        parameters: dict[str, Any],
        invocation_context: InvocationContext,
    ) -> str | None:
        """Validate caller, role and exact typed arguments without executing."""
        if not isinstance(invocation_context, InvocationContext):
            return "Invalid caller context"
        if not isinstance(invocation_context.source, InvocationSource):
            return "Invalid caller context"
        capability = self.capabilities.get(skill)
        if capability is None:
            return "Capability is unavailable"
        if invocation_context.source.value not in capability.allowed_callers:
            return "Caller is not permitted for this capability"
        if capability.risk_level == RiskLevel.HIGH or capability.requires_operator_confirmation:
            if (
                invocation_context.source != InvocationSource.DASHBOARD
                or invocation_context.role != "operator"
                or not invocation_context.principal_id
                or not invocation_context.session_id
            ):
                return "Capability requires an authenticated Dashboard operator"
        if not isinstance(parameters, dict):
            return "invalid skill parameters"
        return self._validate_intent(AgentIntent(skill, parameters))

    def _invocation_failure(self, skill: str, message: str) -> TaskResult:
        task = Task(str(uuid4()), str(uuid4()), self.profile.robot_id, skill)
        task.status = TaskStatus.FAILED
        task.error = message
        return TaskResult(
            task=task,
            feedback=(),
            evaluation=None,
            history=(AuditRecord(
                utc_now(), "invocation.rejected", task.task_id, task.trace_id,
                {"skill": skill, "reason": message},
            ),),
        )

    def preempt_task(self, task_id: str) -> Feedback | None:
        """Drop a voice task that has not started, or cancel it if it is interruptible.

        A task that has not reached adapter execution is marked preempted and
        ``run_text`` returns before the skill runs. A running interruptible
        command uses ``cancel_task``. A running command that is not
        interruptible is left alone.
        """
        with self._runtime_lock:
            command = self._active_commands.get(task_id)
            if command is None:
                self._preempted_task_ids.add(task_id)
                return None
        capability = self.capabilities.get(command.skill)
        if capability is not None and not capability.interruptible:
            return None
        return self.cancel_task(task_id)

    def _consume_preempted(self, task_id: str) -> bool:
        with self._runtime_lock:
            if task_id not in self._preempted_task_ids:
                return False
            self._preempted_task_ids.discard(task_id)
            return True

    def cancel_task(self, task_id: str) -> Feedback | None:
        """Request local cancellation of the task's active command.

        A returned cancelled feedback is a stop acknowledgement from the adapter;
        None means that this runtime has no command active for the task.
        """
        with self._runtime_lock:
            command = self._active_commands.get(task_id)
        if command is None:
            return None
        capability = self.capabilities.get(command.skill)
        arguments = safe_tool_arguments(command, capability) if capability is not None else {}
        private_values = private_command_values(command)
        cancel_call_id = str(uuid4())
        audit_started_at = utc_now()
        self._write_tool_audit(
            tool_call_event(
                "started",
                command,
                capability,
                arguments,
                audit_started_at,
                call_id=cancel_call_id,
                method="adapter.cancel",
            )
        )
        cancel_started = monotonic()
        cancellation_error: BaseException | None = None
        try:
            confirmation = self.adapter.cancel(command)
        except Exception as exc:
            cancellation_error = exc
            confirmation = self._local_feedback(
                command,
                FeedbackStatus.FAILED,
                f"Adapter cancellation failed: {exc}",
                "cancel_failed",
            )
        if (
            not isinstance(confirmation, Feedback)
            or confirmation.status not in {FeedbackStatus.CANCELLED, FeedbackStatus.FAILED}
            or (confirmation.command_id, confirmation.task_id, confirmation.trace_id)
            != (command.command_id, command.task_id, command.trace_id)
        ):
            confirmation = self._local_feedback(
                command,
                FeedbackStatus.FAILED,
                "Adapter returned malformed cancellation feedback",
                "malformed_cancel_feedback",
            )
        if confirmation.status == FeedbackStatus.CANCELLED:
            with self._runtime_lock:
                if self._active_commands.get(task_id) == command:
                    self._cancel_confirmations[task_id] = confirmation
        completion_kwargs: dict[str, Any] = {
            "duration_ms": (monotonic() - cancel_started) * 1000,
            "outcome": "raised" if cancellation_error is not None else "returned",
        }
        if cancellation_error is not None:
            completion_kwargs["error"] = safe_exception(cancellation_error, private_values)
        else:
            completion_kwargs["response"] = summarize_feedback((confirmation,), private_values)
        self._write_tool_audit(
            tool_call_event(
                "completed",
                command,
                capability,
                arguments,
                utc_now(),
                call_id=cancel_call_id,
                method="adapter.cancel",
                **completion_kwargs,
            )
        )
        return confirmation

    def _write_tool_audit(self, event: dict[str, Any]) -> str | None:
        try:
            self.audit_sink.write(event)
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    def run_text(
        self,
        text: str,
        task_id: str | None = None,
        trace_id: str | None = None,
        *,
        _intent: AgentIntent | AgentPlan | None = None,
        _invocation_context: InvocationContext | None = None,
        _operator_confirmed: bool = False,
    ) -> TaskResult:
        resolved_task_id = task_id if task_id is not None else str(uuid4())
        resolved_trace_id = trace_id if trace_id is not None else str(uuid4())
        if not isinstance(resolved_task_id, str) or not resolved_task_id.strip():
            raise ValueError("task_id must be non-empty text")
        if not isinstance(resolved_trace_id, str) or not resolved_trace_id.strip():
            raise ValueError("trace_id must be non-empty text")
        with self._runtime_lock:
            if resolved_task_id in self._used_task_ids:
                raise ValueError(f"task_id has already been used in this runtime: {resolved_task_id}")
            self._used_task_ids.add(resolved_task_id)
        task = Task(resolved_task_id, resolved_trace_id, self.profile.robot_id, text)
        history: list[AuditRecord] = []
        feedback: tuple[Feedback, ...] = ()
        evaluation: Evaluation | None = None

        def record(event_type: str, details: dict[str, Any] | None = None) -> None:
            history.append(AuditRecord(utc_now(), event_type, task.task_id, task.trace_id, details or {}))

        def transition(status: TaskStatus) -> None:
            task.status = status
            task.updated_at = utc_now()
            record("task.status", {"status": status.value})

        record("task.received", {"input_chars": len(text)})
        _trace("Info", task.task_id, f"收到指令：{text.strip()}")
        if self._consume_preempted(resolved_task_id):
            return self._failed_result(task, history, "Voice command was replaced", transition, record)
        if not text.strip():
            return self._failed_result(task, history, "Task input is empty", transition, record)
        transition(TaskStatus.CONTEXTUALIZING)
        observations, observation_error = self._collect_observations(record)
        if observation_error:
            return self._failed_result(task, history, observation_error, transition, record)
        transition(TaskStatus.AWAITING_AGENT)
        try:
            if _intent is not None:
                intent = _intent
            else:
                slash_intent = resolve_slash_command(text, self.capabilities)
                if isinstance(slash_intent, str):
                    return self._failed_result(task, history, slash_intent, transition, record)
                if slash_intent is not None:
                    _trace("Info", task.task_id, "使用直接指令")
                    intent = slash_intent
                else:
                    _trace("Info", task.task_id, "等待语言决策")
                    intent = self.agent.plan(text, self.capabilities, observations)
        except TimeoutError:
            return self._failed_result(task, history, "Agent request timed out", transition, record)
        except AgentRejected as exc:
            return self._failed_result(task, history, exc.reason, transition, record)
        except Exception as exc:
            return self._failed_result(task, history, f"Agent request failed: {exc}", transition, record)
        if intent is None:
            return self._failed_result(task, history, "No supported skill matched the request", transition, record)
        steps, plan_error = _coerce_steps(intent)
        if plan_error:
            return self._failed_result(task, history, plan_error, transition, record)
        requested = len(steps)
        steps = _collapse_repeated_motion(steps, self.capabilities)
        if len(steps) != requested:
            record(
                "plan.collapsed",
                {"from": requested, "to": len(steps), "skills": [step.skill for step in steps]},
            )
        if len(steps) == 1:
            record("agent.intent", {"skill": steps[0].skill, "parameter_names": sorted(steps[0].parameters) if isinstance(steps[0].parameters, dict) else []})
            rationale = steps[0].rationale.strip() if isinstance(steps[0].rationale, str) else ""
            _trace("Info", task.task_id, f"已选择技能 {steps[0].skill}" + (f" 理由={rationale}" if rationale else ""))
        else:
            record(
                "agent.plan",
                {
                    "steps": [
                        {
                            "skill": step.skill,
                            "parameter_names": sorted(step.parameters) if isinstance(step.parameters, dict) else [],
                        }
                        for step in steps
                    ],
                    "rationale": intent.rationale if isinstance(intent, AgentPlan) else "",
                },
            )
            _trace("Info", task.task_id, f"已编排计划 步数={len(steps)}")
        context = _invocation_context or InvocationContext(InvocationSource.AGENT)
        if len(steps) > 1:
            for step in steps:
                authorization_error = self._authorize_step(step, context, _operator_confirmed)
                if authorization_error:
                    return self._failed_result(task, history, authorization_error, transition, record)
        allowances = []
        for step in steps:
            capability = self.capabilities.get(step.skill) if isinstance(step.parameters, dict) else None
            allowances.append(step_allowance_ms(capability, step.parameters) if capability is not None else 0)
        plan_deadline = utc_now() + timedelta(milliseconds=sum(allowances) + _PLAN_MARGIN_MS)
        record(
            "plan.scheduled",
            {"steps": [step.skill for step in steps], "allowance_ms": allowances, "margin_ms": _PLAN_MARGIN_MS},
        )
        executed: list[Feedback] = []
        for index, step in enumerate(steps):
            if utc_now() > plan_deadline:
                return self._failed_result(
                    task,
                    history,
                    "Plan deadline expired before the next step",
                    transition,
                    record,
                    tuple(executed),
                )
            outcome = self._execute_step(
                task,
                history,
                step,
                context,
                _operator_confirmed,
                text,
                transition,
                record,
                prior_feedback=tuple(executed),
                finalize=index == len(steps) - 1,
            )
            if isinstance(outcome, TaskResult):
                return outcome
            executed.extend(outcome)
        return self._failed_result(task, history, "Plan produced no result", transition, record, tuple(executed))

    def _authorize_step(
        self,
        intent: AgentIntent,
        context: InvocationContext,
        operator_confirmed: bool,
    ) -> str | None:
        validation_error = self._validate_intent(intent)
        if validation_error:
            return validation_error
        capability = self.capabilities[intent.skill]
        if context.source.value not in capability.allowed_callers:
            return "Caller is not permitted for this capability"
        dashboard_operator = (
            context.source == InvocationSource.DASHBOARD
            and context.role == "operator"
            and bool(context.principal_id)
            and bool(context.session_id)
            and operator_confirmed
            and context.operator_confirmed
        )
        if capability.risk_level == RiskLevel.HIGH and not dashboard_operator:
            return "High-risk capability requires authenticated Dashboard operator confirmation"
        if capability.requires_operator_confirmation and not dashboard_operator:
            return "Capability requires authenticated operator confirmation"
        if capability.risk_level == RiskLevel.CONFIRM and self.safety_reviewer is None and not _authenticated_dashboard_operator(context):
            return "Safety reviewer unavailable; operation was not executed"
        return None

    def _finish_failure(
        self,
        prior_feedback: tuple[Feedback, ...],
        task: Task,
        history: list[AuditRecord],
        error: str,
        transition: Any,
        record: Any,
        feedback: tuple[Feedback, ...] = (),
        evaluation: Evaluation | None = None,
    ) -> TaskResult:
        return self._failed_result(
            task,
            history,
            error,
            transition,
            record,
            prior_feedback + tuple(feedback),
            evaluation,
        )

    def _execute_step(
        self,
        task: Task,
        history: list[AuditRecord],
        intent: AgentIntent,
        invocation_context: InvocationContext,
        _operator_confirmed: bool,
        text: str,
        transition: Any,
        record: Any,
        *,
        prior_feedback: tuple[Feedback, ...],
        finalize: bool,
    ) -> TaskResult | tuple[Feedback, ...]:
        transition(TaskStatus.VALIDATING_INTENT)
        validation_error = self._validate_intent(intent)
        if validation_error:
            return self._finish_failure(prior_feedback, task, history, validation_error, transition, record)

        capability = self.capabilities[intent.skill]
        allowance = step_allowance_ms(capability, intent.parameters)
        context = invocation_context
        if context.source.value not in capability.allowed_callers:
            return self._finish_failure(prior_feedback, task, history, "Caller is not permitted for this capability", transition, record)
        if capability.risk_level == RiskLevel.HIGH and (
            context.source != InvocationSource.DASHBOARD
            or context.role != "operator"
            or not context.principal_id
            or not context.session_id
            or not _operator_confirmed
            or not context.operator_confirmed
        ):
            return self._finish_failure(prior_feedback, task, history, "High-risk capability requires authenticated Dashboard operator confirmation", transition, record)
        if capability.requires_operator_confirmation and (
            context.source != InvocationSource.DASHBOARD
            or context.role != "operator"
            or not context.principal_id
            or not context.session_id
            or not _operator_confirmed
            or not context.operator_confirmed
        ):
            return self._finish_failure(prior_feedback, task, history, "Capability requires authenticated operator confirmation", transition, record)
        proposal_command: Command | None = None
        operator_confirmation_id = None
        if capability.risk_level == RiskLevel.HIGH or capability.requires_operator_confirmation:
            operator_confirmation_id = str(uuid4())
        if capability.risk_level == RiskLevel.CONFIRM:
            transition(TaskStatus.REVIEWING)
            _trace("Info", task.task_id, f"开始安全复核 技能={intent.skill} 风险={capability.risk_level.value}")
            dashboard_operator = _authenticated_dashboard_operator(context)
            if self.safety_reviewer is None and not dashboard_operator:
                return self._finish_failure(prior_feedback, task, history, "Safety reviewer unavailable; operation was not executed", transition, record)
            canonical_parameters = json.dumps(intent.parameters, sort_keys=True, separators=(",", ":"), allow_nan=False)
            now = utc_now()
            proposal_command = Command(
                command_id=str(uuid4()), task_id=task.task_id, trace_id=task.trace_id,
                robot_id=task.robot_id, skill=intent.skill, parameters=json.loads(canonical_parameters),
                deadline=now + timedelta(milliseconds=allowance),
                risk_level=capability.risk_level, invocation_source=context.source,
                policy_version=self.profile.profile_version,
                operator_confirmed=context.operator_confirmed,
                actor_hash=context.principal_hash or (
                    sha256(context.principal_id.encode("utf-8")).hexdigest() if context.principal_id else None
                ),
            )
            digest = command_proposal_digest(proposal_command, self.profile.profile_version)
            proposal_command = Command(
                **{**proposal_command.__dict__, "review_digest": digest}
            )
            if self.safety_reviewer is None:
                record(
                    "safety.review.completed",
                    {"verdict": "allow", "reviewer": "dashboard_operator", "arguments_digest": digest},
                )
                _trace("Info", task.task_id, f"安全复核通过 技能={intent.skill} 复核者=操作员")
            else:
                proposal = ReviewProposal(
                    proposal_id=str(uuid4()), command=proposal_command, risk_level=capability.risk_level,
                    policy_version=self.profile.profile_version, arguments_digest=digest,
                    created_at=now, expires_at=now + _REVIEW_WINDOW,
                    user_input=text,
                )
                try:
                    review = self.safety_reviewer.review(proposal)
                except Exception as exc:
                    record("safety.review.failed", {"proposal_id": proposal.proposal_id, "error_type": type(exc).__name__})
                    return self._finish_failure(prior_feedback, task, history, "Safety review failed closed", transition, record)
                record("safety.review.completed", {"proposal_id": proposal.proposal_id, "verdict": getattr(getattr(review, "verdict", None), "value", "invalid"), "arguments_digest": digest})
                reviewed_digest = command_proposal_digest(proposal.command, self.profile.profile_version)
                expired = utc_now() >= proposal.expires_at
                verdict = review.verdict if isinstance(review, SafetyReviewResult) else None
                digest_matches = (
                    isinstance(review, SafetyReviewResult)
                    and review.arguments_digest == digest
                    and reviewed_digest == digest
                )
                if verdict == ReviewVerdict.ALLOW and digest_matches and not expired:
                    _trace("Info", task.task_id, f"安全复核通过 技能={intent.skill}")
                if verdict != ReviewVerdict.ALLOW or not digest_matches or expired:
                    reason = f"安全复核没有通过，没有执行 {intent.skill}"
                    if (
                        isinstance(review, SafetyReviewResult)
                        and verdict != ReviewVerdict.ALLOW
                        and digest_matches
                        and not expired
                        and review.rationale.strip()
                    ):
                        reason = review.rationale.strip()
                    elif expired:
                        reason = f"{intent.skill} 的安全复核超时，没有执行"
                    elif not digest_matches:
                        reason = f"{intent.skill} 的复核结果与提案不一致，没有执行"
                    return self._finish_failure(prior_feedback, task, history, reason, transition, record)
        if proposal_command is not None:
            proposal_command = Command(
                **{
                    **proposal_command.__dict__,
                    "deadline": utc_now() + timedelta(milliseconds=allowance),
                }
            )
        command = proposal_command or Command(
            command_id=str(uuid4()),
            task_id=task.task_id,
            trace_id=task.trace_id,
            robot_id=task.robot_id,
            skill=intent.skill,
            parameters=dict(intent.parameters),
            deadline=utc_now() + timedelta(milliseconds=allowance),
            risk_level=capability.risk_level,
            invocation_source=context.source,
            review_digest=proposal_command.review_digest if proposal_command is not None else None,
            policy_version=self.profile.profile_version,
            operator_confirmation_id=operator_confirmation_id,
            operator_confirmed=context.operator_confirmed,
            actor_hash=context.principal_hash or (
                sha256(context.principal_id.encode("utf-8")).hexdigest() if context.principal_id else None
            ),
        )
        if command.operator_confirmed:
            record("operator.confirmed", {
                "skill": command.skill,
                "risk_level": command.risk_level.value,
                "invocation_source": command.invocation_source.value,
                "actor_hash": command.actor_hash,
                "review_digest": command.review_digest,
            })
        if capability.risk_level == RiskLevel.CONFIRM:
            if command_proposal_digest(command, self.profile.profile_version) != command.review_digest:
                return self._finish_failure(prior_feedback, task, history, "Reviewed command changed before dispatch", transition, record)
            with self._runtime_lock:
                if command.review_digest in self._used_review_bindings:
                    return self._finish_failure(prior_feedback, task, history, "Reviewed proposal was already consumed", transition, record)
                self._used_review_bindings.add(command.review_digest)
        if self._consume_preempted(task.task_id):
            return self._finish_failure(prior_feedback, task, history, "Voice command was replaced", transition, record)
        record("command.created", {"command_id": command.command_id, "skill": command.skill})
        transition(TaskStatus.EXECUTING)
        _trace("Info", task.task_id, f"开始执行 技能={command.skill}")
        tool_arguments = safe_tool_arguments(command, capability)
        tool_call_id = str(uuid4())
        private_values = (*private_command_values(command), text)
        tool_started = tool_call_event(
            "started",
            command,
            capability,
            tool_arguments,
            utc_now(),
            call_id=tool_call_id,
        )
        audit_start_error = self._write_tool_audit(tool_started)
        if audit_start_error is not None:
            record(
                "tool.audit_write_failed",
                {"phase": "started", "tool": command.skill, "error": audit_start_error},
            )
            return self._finish_failure(prior_feedback, 
                task,
                history,
                "Tool call audit could not be recorded; command was not executed",
                transition,
                record,
            )
        record("tool.call.started", tool_started)
        with self._runtime_lock:
            self._active_commands[task.task_id] = command
            self._cancel_confirmations.pop(task.task_id, None)
        execute_started = monotonic()
        try:
            feedback = self.adapter.execute(command)
        except Exception as exc:
            tool_completed = tool_call_event(
                "completed",
                command,
                capability,
                tool_arguments,
                utc_now(),
                call_id=tool_call_id,
                duration_ms=(monotonic() - execute_started) * 1000,
                outcome="raised",
                error=safe_exception(exc, private_values),
            )
            record("tool.call.completed", tool_completed)
            completion_error = self._write_tool_audit(tool_completed)
            if completion_error is not None:
                record(
                    "tool.audit_write_failed",
                    {"phase": "completed", "tool": command.skill, "error": completion_error},
                )
            with self._runtime_lock:
                self._active_commands.pop(task.task_id, None)
                confirmation = self._cancel_confirmations.pop(task.task_id, None)
            if confirmation is not None:
                record("command.cancel_confirmation", {"status": confirmation.status.value})
            return self._finish_failure(prior_feedback, 
                task,
                history,
                f"Adapter execution failed: {exc}",
                transition,
                record,
            )
        tool_completed = tool_call_event(
            "completed",
            command,
            capability,
            tool_arguments,
            utc_now(),
            call_id=tool_call_id,
            duration_ms=(monotonic() - execute_started) * 1000,
            outcome="returned",
            response=summarize_feedback(feedback, private_values),
        )
        record("tool.call.completed", tool_completed)
        completion_error = self._write_tool_audit(tool_completed)
        if completion_error is not None:
            record(
                "tool.audit_write_failed",
                {"phase": "completed", "tool": command.skill, "error": completion_error},
            )
        with self._runtime_lock:
            self._active_commands.pop(task.task_id, None)
            confirmation = self._cancel_confirmations.pop(task.task_id, None)
        if confirmation is not None:
            record(
                "command.cancel_confirmation",
                {"status": confirmation.status.value, "feedback_id": confirmation.feedback_id},
            )
        if utc_now() > command.deadline:
            return self._finish_failure(prior_feedback, 
                task,
                history,
                "Adapter returned after the command deadline",
                transition,
                record,
            )
        feedback_error = self._validate_feedback(command, feedback)
        if feedback_error:
            record("command.feedback_rejected", {"error": feedback_error})
            return self._finish_failure(prior_feedback, task, history, feedback_error, transition, record)
        terminal_cancel = next(
            (item for item in feedback if item.status == FeedbackStatus.CANCELLED),
            None,
        )
        if confirmation is not None and terminal_cancel is None:
            return self._finish_failure(prior_feedback, 
                task,
                history,
                "Adapter cancellation confirmation contradicted command feedback",
                transition,
                record,
                feedback,
            )
        if terminal_cancel is not None and confirmation is None:
            record(
                "command.cancel_confirmation",
                {"status": terminal_cancel.status.value, "feedback_id": terminal_cancel.feedback_id},
            )
        for item in feedback:
            record(
                "command.feedback",
                {"command_id": item.command_id, "status": item.status.value, "error_code": item.error_code},
            )
        if any(item.status == FeedbackStatus.CANCELLED for item in feedback):
            transition(TaskStatus.CANCELLED)
            _trace("Warning", task.task_id, f"执行已取消 技能={command.skill}")
            return TaskResult(task, prior_feedback + tuple(feedback), None, tuple(history))
        terminal = next(
            (item for item in reversed(feedback) if item.status in {FeedbackStatus.SUCCEEDED, FeedbackStatus.FAILED}),
            None,
        )
        if terminal is None:
            return self._finish_failure(prior_feedback, task, history, "Adapter returned no terminal command feedback", transition, record, feedback)
        if terminal.status == FeedbackStatus.FAILED:
            return self._finish_failure(
                prior_feedback,
                task,
                history,
                terminal.message or "Command execution failed",
                transition,
                record,
                feedback,
            )
        condition_met = terminal.evidence.get("condition_met") is True
        nominal = terminal.evidence.get("completion") == "nominal"
        if condition_met and nominal:
            conclusion = "Nominal duration elapsed; body completion is estimated"
        elif condition_met:
            conclusion = "Skill completed with observable evidence"
        else:
            conclusion = "Completion evidence is missing"
        if prior_feedback and condition_met:
            conclusion = (
                "Plan completed; body completion is estimated where a step used a nominal duration"
                if any(item.evidence.get("completion") == "nominal" for item in (*prior_feedback, terminal))
                else "Plan completed"
            )
        if not finalize and condition_met:
            record("plan.step_completed", {"skill": command.skill})
            return feedback
        transition(TaskStatus.MONITORING)
        transition(TaskStatus.EVALUATING)
        evaluation = Evaluation(
            task_id=task.task_id,
            trace_id=task.trace_id,
            success=condition_met,
            conclusion=conclusion,
            evidence=(terminal.message,) if condition_met else (),
            confidence=1.0 if condition_met else 0.0,
        )
        record("task.evaluation", {"success": evaluation.success, "evidence": list(evaluation.evidence)})
        if evaluation.success:
            transition(TaskStatus.COMPLETED)
            _trace("Info", task.task_id, f"执行完成 技能={command.skill} 结论={evaluation.conclusion}")
        else:
            return self._finish_failure(prior_feedback, task, history, evaluation.conclusion, transition, record, feedback, evaluation)
        return TaskResult(task, prior_feedback + tuple(feedback), evaluation, tuple(history))

    def _validate_intent(self, intent: AgentIntent) -> str | None:
        if not isinstance(intent.skill, str) or not intent.skill:
            return "Agent returned an invalid skill name"
        if not isinstance(intent.parameters, dict):
            return "Agent returned invalid skill parameters"
        capability = self.capabilities.get(intent.skill)
        if capability is None:
            return f"Unsupported skill: {intent.skill}"
        if set(intent.parameters) != set(capability.parameters):
            return "Skill parameters do not match the declared schema"
        for name, spec in capability.parameters.items():
            value = intent.parameters[name]
            if spec.value_type == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return f"Parameter {name} must be numeric"
                if not isfinite(value):
                    return f"Parameter {name} must be finite"
                if spec.minimum is not None and value < spec.minimum:
                    return f"Parameter {name} is below its minimum"
                if spec.maximum is not None and value > spec.maximum:
                    return f"Parameter {name} exceeds its maximum"
            elif spec.value_type == "boolean":
                if not isinstance(value, bool):
                    return f"Parameter {name} must be boolean"
            elif spec.value_type == "string":
                if not isinstance(value, str):
                    return f"Parameter {name} must be text"
                if spec.max_length is not None and len(value) > spec.max_length:
                    return f"Parameter {name} is too long"
        return None

    def _collect_observations(
        self,
        record: Any,
    ) -> tuple[tuple[Observation, ...], str | None]:
        try:
            observations = self.adapter.drain_observations()
        except Exception as exc:
            return (), f"Could not read adapter observations: {exc}"
        if not isinstance(observations, tuple):
            return (), "Adapter returned malformed observations"

        accepted: list[Observation] = []
        now = utc_now()
        for observation in observations:
            if not isinstance(observation, Observation):
                return (), "Adapter returned a malformed observation"
            event_id = observation.event_id
            if not isinstance(event_id, str) or not event_id:
                return (), "Observation event_id must be non-empty text"
            with self._runtime_lock:
                duplicate = event_id in self._seen_event_ids
                self._seen_event_ids.add(event_id)
            if duplicate:
                record("observation.rejected", {"event_id": event_id, "reason": "duplicate"})
                continue
            if observation.robot_id != self.profile.robot_id:
                record("observation.rejected", {"event_id": event_id, "reason": "robot_id_mismatch"})
                continue
            if not all(
                isinstance(value, str) and value
                for value in (observation.device_id, observation.event_type, observation.source)
            ):
                record("observation.rejected", {"event_id": event_id, "reason": "missing_metadata"})
                continue
            if not isinstance(observation.payload, dict):
                record("observation.rejected", {"event_id": event_id, "reason": "invalid_payload"})
                continue
            if isinstance(observation.ttl_ms, bool) or not isinstance(observation.ttl_ms, int) or observation.ttl_ms <= 0:
                record("observation.rejected", {"event_id": event_id, "reason": "invalid_ttl"})
                continue
            if not self._is_aware_datetime(observation.observed_at) or not self._is_aware_datetime(observation.received_at):
                record("observation.rejected", {"event_id": event_id, "reason": "invalid_timestamp"})
                continue
            if observation.received_at > now or observation.observed_at > now or not observation.is_fresh:
                record("observation.rejected", {"event_id": event_id, "reason": "expired_or_future"})
                continue
            if not isinstance(observation.privacy_class, str) or not observation.privacy_class:
                record("observation.rejected", {"event_id": event_id, "reason": "invalid_privacy_class"})
                continue
            record(
                "observation.accepted",
                {
                    "event_id": event_id,
                    "device_id": observation.device_id,
                    "event_type": observation.event_type,
                    "source": observation.source,
                    "privacy_class": observation.privacy_class,
                },
            )
            if observation.privacy_class == "operational":
                accepted.append(observation)
            else:
                record("observation.withheld", {"event_id": event_id, "reason": "privacy_policy"})
        return tuple(accepted), None

    @staticmethod
    def _is_aware_datetime(value: Any) -> bool:
        return getattr(value, "tzinfo", None) is not None and value.utcoffset() is not None

    @staticmethod
    def _local_feedback(
        command: Command,
        status: FeedbackStatus,
        message: str,
        error_code: str,
    ) -> Feedback:
        return Feedback(
            feedback_id=str(uuid4()),
            command_id=command.command_id,
            task_id=command.task_id,
            trace_id=command.trace_id,
            status=status,
            timestamp=utc_now(),
            message=message,
            error_code=error_code,
        )

    @staticmethod
    def _validate_feedback(command: Command, feedback: tuple[Feedback, ...]) -> str | None:
        if not isinstance(feedback, tuple) or not feedback:
            return "Adapter returned no command feedback"
        for item in feedback:
            if not isinstance(item, Feedback) or not isinstance(item.status, FeedbackStatus):
                return "Adapter returned malformed command feedback"
            if not RobotRuntime._is_aware_datetime(item.timestamp):
                return "Adapter feedback contains an invalid timestamp"
            if item.timestamp > utc_now():
                return "Adapter feedback contains a future timestamp"
            if (item.command_id, item.task_id, item.trace_id) != (
                command.command_id,
                command.task_id,
                command.trace_id,
            ):
                return "Adapter feedback correlation IDs do not match the command"
        statuses = [item.status for item in feedback]
        timestamps = [item.timestamp for item in feedback]
        if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:])):
            return "Adapter feedback timestamps are out of order"
        if statuses in ([FeedbackStatus.FAILED], [FeedbackStatus.CANCELLED]):
            return None
        if len(statuses) < 2 or statuses[0] != FeedbackStatus.ACCEPTED:
            return "Adapter returned an invalid command lifecycle"
        terminal_statuses = {FeedbackStatus.SUCCEEDED, FeedbackStatus.FAILED, FeedbackStatus.CANCELLED}
        if statuses[-1] not in terminal_statuses or any(status in terminal_statuses for status in statuses[:-1]):
            return "Adapter returned an invalid command lifecycle"
        if statuses[1:-1] not in ([], [FeedbackStatus.RUNNING]):
            return "Adapter returned an invalid command lifecycle"
        if len(statuses) == 3 and statuses[1] != FeedbackStatus.RUNNING:
            return "Adapter returned an invalid command lifecycle"
        return None

    @staticmethod
    def _failed_result(
        task: Task,
        history: list[AuditRecord],
        error: str,
        transition: Any,
        record: Any,
        feedback: tuple[Feedback, ...] = (),
        evaluation: Evaluation | None = None,
    ) -> TaskResult:
        task.error = error
        record("task.failed", {"error": error})
        transition(TaskStatus.FAILED)
        _trace("Warning", task.task_id, f"指令未执行 原因={error}")
        return TaskResult(task, feedback, evaluation, tuple(history))
