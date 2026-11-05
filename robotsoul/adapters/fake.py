from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from threading import Lock
from uuid import uuid4

from robotsoul.models import CapabilitySpec, Command, Feedback, FeedbackStatus, utc_now
from robotsoul.profiles import RobotProfile


class FakeRobotAdapter:
    """Deterministic adapter used for contract and end-to-end MVP validation."""

    def __init__(self, profile: RobotProfile):
        self.profile = profile
        self.robot_id = profile.robot_id
        self.capabilities = {capability.name: capability for capability in profile.capabilities}
        self.connected = False
        self.executed_commands: list[Command] = []
        self._feedback_cache: dict[str, tuple[Command, tuple[Feedback, ...]]] = {}
        self._lock = Lock()

    def connect(self) -> None:
        self.connected = True

    def health(self) -> bool:
        return self.connected

    def identity(self) -> tuple[str, str]:
        return self.profile.manufacturer, self.profile.model

    def discover_capabilities(self) -> dict[str, CapabilitySpec]:
        return dict(self.capabilities)

    def execute(self, command: Command) -> tuple[Feedback, ...]:
        with self._lock:
            cached = self._feedback_cache.get(command.command_id)
            if cached is not None:
                previous_command, previous_feedback = cached
                if previous_command == command:
                    return previous_feedback
                return (
                    self._feedback(
                        command,
                        FeedbackStatus.FAILED,
                        "Command ID was already used for a different request",
                        "idempotency_conflict",
                    ),
                )
            capability = self.capabilities.get(command.skill)
            validation_error = self._validate(command, capability)
            if validation_error:
                result = (self._feedback(command, FeedbackStatus.FAILED, validation_error, "command_rejected"),)
                self._feedback_cache[command.command_id] = (command, result)
                return result
            self.executed_commands.append(command)
            result = (
                self._feedback(command, FeedbackStatus.ACCEPTED, "Command accepted"),
                self._feedback(command, FeedbackStatus.RUNNING, "Simulated skill is running"),
                self._feedback(
                    command,
                    FeedbackStatus.SUCCEEDED,
                    "Simulated skill completed",
                    evidence={"condition_met": True, "skill": command.skill},
                ),
            )
            self._feedback_cache[command.command_id] = (command, result)
            return result

    def cancel(self, command: Command) -> Feedback:
        return self._feedback(command, FeedbackStatus.CANCELLED, "Simulated command cancelled")

    def close(self) -> None:
        self.connected = False

    def _validate(self, command: Command, capability: CapabilitySpec | None) -> str | None:
        if not self.connected:
            return "Adapter is disconnected"
        if command.robot_id != self.robot_id:
            return "Robot ID mismatch"
        if capability is None:
            return f"Unsupported skill: {command.skill}"
        if command.deadline <= utc_now():
            return "Command deadline has expired"
        if set(command.parameters) != set(capability.parameters):
            return "Command parameters do not match the skill schema"
        for name, spec in capability.parameters.items():
            value = command.parameters[name]
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

    @staticmethod
    def _feedback(
        command: Command,
        status: FeedbackStatus,
        message: str,
        error_code: str | None = None,
        evidence: dict | None = None,
    ) -> Feedback:
        return Feedback(
            feedback_id=str(uuid4()),
            command_id=command.command_id,
            task_id=command.task_id,
            trace_id=command.trace_id,
            status=status,
            timestamp=datetime.now(timezone.utc),
            message=message,
            error_code=error_code,
            evidence=evidence or {},
        )
