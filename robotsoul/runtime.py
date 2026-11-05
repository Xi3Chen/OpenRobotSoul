from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from math import isfinite
from threading import Lock
from typing import Any
from uuid import uuid4

from robotsoul.adapters.base import RobotAdapter
from robotsoul.adapters.fake import FakeRobotAdapter
from robotsoul.agent import AgentProvider, DemoAgent
from robotsoul.models import (
    AgentIntent,
    AuditRecord,
    CapabilitySpec,
    Command,
    Evaluation,
    Feedback,
    FeedbackStatus,
    Observation,
    Task,
    TaskResult,
    TaskStatus,
    ParameterSpec,
    utc_now,
)
from robotsoul.profiles import RobotProfile


@dataclass
class RobotRuntime:
    profile: RobotProfile
    adapter: RobotAdapter
    agent: AgentProvider
    capabilities: dict[str, CapabilitySpec]
    _active_commands: dict[str, Command] = field(default_factory=dict, init=False, repr=False)
    _cancel_confirmations: dict[str, Feedback] = field(default_factory=dict, init=False, repr=False)
    _seen_event_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _used_task_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _runtime_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @classmethod
    def from_profile(
        cls,
        profile: RobotProfile,
        agent: AgentProvider | None = None,
        adapter: RobotAdapter | None = None,
    ) -> "RobotRuntime":
        if profile.adapter_id != "fake-simulator" and adapter is None:
            raise ValueError(f"No adapter registered for {profile.adapter_id}")
        if adapter is not None and agent is None:
            raise ValueError("An explicit AgentProvider is required with a custom robot adapter")
        active_adapter = adapter if adapter is not None else FakeRobotAdapter(profile)
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
        return cls(profile, active_adapter, agent if agent is not None else DemoAgent(), effective)

    @staticmethod
    def _constrain_capability(
        declared: CapabilitySpec,
        discovered: CapabilitySpec,
    ) -> CapabilitySpec | None:
        if declared.safety_class != "low" or discovered.safety_class != "low":
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
        return CapabilitySpec(
            name=declared.name,
            parameters=parameters,
            safety_class="low",
            interruptible=declared.interruptible and discovered.interruptible,
            timeout_ms=min(declared.timeout_ms, discovered.timeout_ms),
            required=declared.required,
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
                "interruptible": capability.interruptible,
            }
            for name, capability in self.capabilities.items()
        }

    def cancel_task(self, task_id: str) -> Feedback | None:
        """Request local cancellation of the task's active command.

        A returned cancelled feedback is a stop acknowledgement from the adapter;
        None means that this runtime has no command active for the task.
        """
        with self._runtime_lock:
            command = self._active_commands.get(task_id)
        if command is None:
            return None
        try:
            confirmation = self.adapter.cancel(command)
        except Exception as exc:
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
        return confirmation

    def run_text(
        self,
        text: str,
        task_id: str | None = None,
        trace_id: str | None = None,
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
        if not text.strip():
            return self._failed_result(task, history, "Task input is empty", transition, record)
        transition(TaskStatus.CONTEXTUALIZING)
        observations, observation_error = self._collect_observations(record)
        if observation_error:
            return self._failed_result(task, history, observation_error, transition, record)
        transition(TaskStatus.AWAITING_AGENT)
        try:
            intent = self.agent.plan(text, self.capabilities, observations)
        except TimeoutError:
            return self._failed_result(task, history, "Agent request timed out", transition, record)
        except Exception as exc:
            return self._failed_result(task, history, f"Agent request failed: {exc}", transition, record)
        if intent is None:
            return self._failed_result(task, history, "No supported skill matched the request", transition, record)
        if not isinstance(intent, AgentIntent):
            return self._failed_result(task, history, "Agent returned an invalid intent", transition, record)
        record("agent.intent", {"skill": intent.skill, "parameter_names": sorted(intent.parameters) if isinstance(intent.parameters, dict) else []})
        transition(TaskStatus.VALIDATING_INTENT)
        validation_error = self._validate_intent(intent)
        if validation_error:
            return self._failed_result(task, history, validation_error, transition, record)

        capability = self.capabilities[intent.skill]
        command = Command(
            command_id=str(uuid4()),
            task_id=task.task_id,
            trace_id=task.trace_id,
            robot_id=task.robot_id,
            skill=intent.skill,
            parameters=dict(intent.parameters),
            deadline=utc_now() + timedelta(milliseconds=capability.timeout_ms),
        )
        record("command.created", {"command_id": command.command_id, "skill": command.skill})
        transition(TaskStatus.EXECUTING)
        with self._runtime_lock:
            self._active_commands[task.task_id] = command
            self._cancel_confirmations.pop(task.task_id, None)
        try:
            feedback = self.adapter.execute(command)
        except Exception as exc:
            with self._runtime_lock:
                self._active_commands.pop(task.task_id, None)
                confirmation = self._cancel_confirmations.pop(task.task_id, None)
            if confirmation is not None:
                record("command.cancel_confirmation", {"status": confirmation.status.value})
            return self._failed_result(
                task,
                history,
                f"Adapter execution failed: {exc}",
                transition,
                record,
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
            return self._failed_result(
                task,
                history,
                "Adapter returned after the command deadline",
                transition,
                record,
            )
        feedback_error = self._validate_feedback(command, feedback)
        if feedback_error:
            record("command.feedback_rejected", {"error": feedback_error})
            return self._failed_result(task, history, feedback_error, transition, record)
        terminal_cancel = next(
            (item for item in feedback if item.status == FeedbackStatus.CANCELLED),
            None,
        )
        if confirmation is not None and terminal_cancel is None:
            return self._failed_result(
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
            return TaskResult(task, feedback, None, tuple(history))
        terminal = next(
            (item for item in reversed(feedback) if item.status in {FeedbackStatus.SUCCEEDED, FeedbackStatus.FAILED}),
            None,
        )
        if terminal is None:
            return self._failed_result(task, history, "Adapter returned no terminal command feedback", transition, record, feedback)
        if terminal.status == FeedbackStatus.FAILED:
            return self._failed_result(
                task,
                history,
                terminal.message or "Command execution failed",
                transition,
                record,
                feedback,
            )
        transition(TaskStatus.MONITORING)
        transition(TaskStatus.EVALUATING)
        condition_met = terminal.evidence.get("condition_met") is True
        evaluation = Evaluation(
            task_id=task.task_id,
            trace_id=task.trace_id,
            success=condition_met,
            conclusion="Skill completed with observable evidence" if condition_met else "Completion evidence is missing",
            evidence=(terminal.message,) if condition_met else (),
            confidence=1.0 if condition_met else 0.0,
        )
        record("task.evaluation", {"success": evaluation.success, "evidence": list(evaluation.evidence)})
        if evaluation.success:
            transition(TaskStatus.COMPLETED)
        else:
            return self._failed_result(task, history, evaluation.conclusion, transition, record, feedback, evaluation)
        return TaskResult(task, feedback, evaluation, tuple(history))

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
        return TaskResult(task, feedback, evaluation, tuple(history))
