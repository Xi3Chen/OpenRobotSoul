from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import isfinite
from threading import Event, Lock
from time import monotonic
from uuid import uuid4

from robotsoul.models import CapabilitySpec, Command, Feedback, FeedbackStatus, Observation, utc_now
from robotsoul.profiles import RobotProfile


@dataclass
class _InFlightCommand:
    command: Command
    cancel_requested: Event
    finished: Event
    feedback: tuple[Feedback, ...] | None = None


class FakeRobotAdapter:
    """Deterministic adapter used for contract and end-to-end MVP validation."""

    def __init__(self, profile: RobotProfile, execution_delay_seconds: float = 0):
        if execution_delay_seconds < 0:
            raise ValueError("execution_delay_seconds must not be negative")
        self.profile = profile
        self.robot_id = profile.robot_id
        self.capabilities = {capability.name: capability for capability in profile.capabilities}
        self.connected = False
        self.executed_commands: list[Command] = []
        self.execution_started = Event()
        self.execution_delay_seconds = execution_delay_seconds
        self._feedback_cache: dict[str, tuple[Command, tuple[Feedback, ...]]] = {}
        self._in_flight: dict[str, _InFlightCommand] = {}
        self._cancel_before_start: dict[str, Command] = {}
        self._observations: deque[Observation] = deque()
        self._lock = Lock()

    def connect(self) -> None:
        self.connected = True

    def health(self) -> bool:
        return self.connected

    def identity(self) -> tuple[str, str]:
        return self.profile.manufacturer, self.profile.model

    def discover_capabilities(self) -> dict[str, CapabilitySpec]:
        return dict(self.capabilities)

    def publish_observation(self, observation: Observation) -> None:
        with self._lock:
            self._observations.append(observation)

    def drain_observations(self) -> tuple[Observation, ...]:
        with self._lock:
            observations = tuple(self._observations)
            self._observations.clear()
        return observations

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
            in_flight = self._in_flight.get(command.command_id)
            if in_flight is not None:
                if in_flight.command == command:
                    return in_flight.feedback or ()
                return (
                    self._feedback(
                        command,
                        FeedbackStatus.FAILED,
                        "Command ID is already running a different request",
                        "idempotency_conflict",
                    ),
                )
            cancelled_command = self._cancel_before_start.get(command.command_id)
            if cancelled_command is not None:
                if cancelled_command != command:
                    return (
                        self._feedback(
                            command,
                            FeedbackStatus.FAILED,
                            "Command ID was cancelled for a different request",
                            "idempotency_conflict",
                        ),
                    )
                self._cancel_before_start.pop(command.command_id, None)
                result = (self._feedback(command, FeedbackStatus.CANCELLED, "Simulated command cancelled before start"),)
                self._feedback_cache[command.command_id] = (command, result)
                return result
            capability = self.capabilities.get(command.skill)
            validation_error = self._validate(command, capability)
            if validation_error:
                result = (self._feedback(command, FeedbackStatus.FAILED, validation_error, "command_rejected"),)
                self._feedback_cache[command.command_id] = (command, result)
                return result
            self.executed_commands.append(command)
            self.execution_started.set()
            pending_feedback = (
                self._feedback(command, FeedbackStatus.ACCEPTED, "Command accepted"),
                self._feedback(command, FeedbackStatus.RUNNING, "Simulated skill is running"),
            )
            state = _InFlightCommand(command, Event(), Event(), pending_feedback)
            self._in_flight[command.command_id] = state

        wait_seconds = min(
            self.execution_delay_seconds,
            max((command.deadline - utc_now()).total_seconds(), 0),
        )
        started_at = monotonic()
        if wait_seconds > 0:
            state.cancel_requested.wait(wait_seconds)

        if state.cancel_requested.is_set():
            terminal = self._feedback(command, FeedbackStatus.CANCELLED, "Simulated command stopped")
        elif utc_now() >= command.deadline or monotonic() - started_at + 1e-6 < self.execution_delay_seconds:
            terminal = self._feedback(
                command,
                FeedbackStatus.FAILED,
                "Command deadline expired during simulated execution",
                "command_timeout",
            )
        else:
            terminal = self._feedback(
                command,
                FeedbackStatus.SUCCEEDED,
                "Simulated skill completed",
                evidence={"condition_met": True, "skill": command.skill},
            )
        result = (*pending_feedback, terminal)
        with self._lock:
            state.feedback = result
            state.finished.set()
            self._feedback_cache[command.command_id] = (command, result)
            self._in_flight.pop(command.command_id, None)
            return result

    def cancel(self, command: Command) -> Feedback:
        with self._lock:
            in_flight = self._in_flight.get(command.command_id)
            if in_flight is None:
                cached = self._feedback_cache.get(command.command_id)
                if cached is not None:
                    return self._feedback(
                        command,
                        FeedbackStatus.FAILED,
                        "Command is no longer running",
                        "command_not_running",
                    )
                capability = self.capabilities.get(command.skill)
                if command.robot_id != self.robot_id or capability is None:
                    return self._feedback(
                        command,
                        FeedbackStatus.FAILED,
                        "Command is not a known active request",
                        "command_not_running",
                    )
                if not capability.interruptible:
                    return self._feedback(
                        command,
                        FeedbackStatus.FAILED,
                        "Skill is not interruptible",
                        "not_interruptible",
                    )
                validation_error = self._validate(command, capability)
                if validation_error:
                    return self._feedback(
                        command,
                        FeedbackStatus.FAILED,
                        validation_error,
                        "command_rejected",
                    )
                self._cancel_before_start[command.command_id] = command
                return self._feedback(command, FeedbackStatus.CANCELLED, "Simulated command cancelled before start")
            if in_flight.command != command:
                return self._feedback(
                    command,
                    FeedbackStatus.FAILED,
                    "Command ID does not match the active request",
                    "idempotency_conflict",
                )
            capability = self.capabilities.get(command.skill)
            if capability is None or not capability.interruptible:
                return self._feedback(
                    command,
                    FeedbackStatus.FAILED,
                    "Skill is not interruptible",
                    "not_interruptible",
                )
            in_flight.cancel_requested.set()
            state = in_flight

        wait_seconds = max((command.deadline - utc_now()).total_seconds(), 0)
        if not state.finished.wait(wait_seconds):
            return self._feedback(
                command,
                FeedbackStatus.FAILED,
                "Simulated command did not confirm stop before its deadline",
                "cancel_timeout",
            )
        terminal = state.feedback[-1] if state.feedback else None
        if terminal is not None and terminal.status == FeedbackStatus.CANCELLED:
            return terminal
        return self._feedback(
            command,
            FeedbackStatus.FAILED,
            "Command reached a terminal state before cancellation was confirmed",
            "command_not_cancelled",
        )

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
            timestamp=utc_now(),
            message=message,
            error_code=error_code,
            evidence=evidence or {},
        )
