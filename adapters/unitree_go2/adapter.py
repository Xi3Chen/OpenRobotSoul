from __future__ import annotations

from math import isfinite
from typing import Any
from uuid import uuid4

from adapters.unitree_go2.driver import UnitreeGo2Driver
from adapters.unitree_go2.speaker import start_go2_speaker
from robotsoul.adapters.base import BatteryStatus
from robotsoul.models import (
    CapabilitySpec,
    Command,
    Feedback,
    FeedbackStatus,
    RiskLevel,
    Observation,
    ParameterSpec,
    command_proposal_digest,
    utc_now,
)
from robotsoul.profiles import RobotProfile
from robotsoul.runlog import log as runtime_log


class UnitreeGo2Adapter:
    """Profile-bounded adapter around a vendor-specific Go2 driver bridge."""

    def __init__(self, profile: RobotProfile, driver: UnitreeGo2Driver):
        if profile.robot_module != "unitree_go2":
            raise ValueError("UnitreeGo2Adapter requires a unitree-go2 profile")
        self.profile = profile
        self.driver = driver
        self.robot_id = profile.robot_id
        self.connected = False
        self._speaker: Any | None = None
        self._capabilities: dict[str, CapabilitySpec] = {}

    def connect(self) -> None:
        try:
            self.driver.connect()
            if not self.driver.health():
                raise RuntimeError("Unitree Go2 driver failed its health check")
            if self.driver.robot_id != self.profile.robot_id:
                raise RuntimeError("Connected Go2 robot ID does not match the selected profile")
            manufacturer, model = self.driver.identity()
            if (manufacturer, model) != (self.profile.manufacturer, self.profile.model):
                raise RuntimeError("Connected device identity does not match the Go2 profile")
        except Exception:
            self.driver.close()
            self.connected = False
            raise
        self.connected = True
        self._start_speaker()

    def _start_speaker(self) -> None:
        self._speaker = None
        try:
            self._speaker = start_go2_speaker()
        except Exception as exc:
            self._speaker = None
            runtime_log("Go2", "Warning", f"机身喇叭未登记：{exc}")

    def health(self) -> bool:
        return self.connected and self.driver.health()

    def battery(self) -> BatteryStatus | None:
        if not self.connected:
            return None
        reader = getattr(self.driver, "battery", None)
        if not callable(reader):
            return None
        try:
            status = reader()
        except Exception:
            return None
        return status if isinstance(status, BatteryStatus) else None

    def identity(self) -> tuple[str, str]:
        return self.driver.identity()

    def discover_capabilities(self) -> dict[str, CapabilitySpec]:
        if not self.connected:
            raise RuntimeError("Unitree Go2 adapter is not connected")
        discovered = self.driver.discover_capabilities()
        if not isinstance(discovered, dict):
            raise RuntimeError("Go2 driver returned a malformed capability map")
        declared = {capability.name: capability for capability in self.profile.capabilities}
        self._capabilities = {
            name: effective
            for name, device_capability in discovered.items()
            if (profile_capability := declared.get(name)) is not None
            and (effective := _intersect_capabilities(profile_capability, device_capability)) is not None
        }
        return dict(self._capabilities)

    def drain_observations(self) -> tuple[Observation, ...]:
        if not self.connected:
            return ()
        observations = self.driver.drain_observations()
        if not isinstance(observations, tuple) or any(
            not isinstance(observation, Observation) for observation in observations
        ):
            raise RuntimeError("Go2 driver returned malformed observations")
        return observations

    def sensor_snapshot(self) -> dict[str, Any]:
        if not self.connected:
            return {"streams": []}
        reader = getattr(self.driver, "sensor_snapshot", None)
        if not callable(reader):
            return {"streams": []}
        try:
            snapshot = reader()
        except Exception as exc:
            return {"streams": [], "error": str(exc)}
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("streams"), list):
            return {"streams": [], "error": "Go2 sensor snapshot is invalid"}
        return snapshot

    def execute(self, command: Command) -> tuple[Feedback, ...]:
        error = self._validate_command(command)
        if error:
            return (self._feedback(command, FeedbackStatus.FAILED, error, "command_rejected"),)
        return self.driver.execute(command)

    def cancel(self, command: Command) -> Feedback:
        capability = self._capabilities.get(command.skill)
        if capability is None or command.robot_id != self.robot_id:
            return self._feedback(
                command,
                FeedbackStatus.FAILED,
                "Command is not registered for this Go2 profile",
                "command_rejected",
            )
        if not capability.interruptible:
            return self._feedback(
                command,
                FeedbackStatus.FAILED,
                "Skill is not interruptible",
                "not_interruptible",
            )
        return self.driver.cancel(command)

    def close(self) -> None:
        speaker = self._speaker
        self._speaker = None
        try:
            if speaker is not None:
                speaker.close()
        finally:
            try:
                self.driver.close()
            finally:
                self.connected = False

    def _validate_command(self, command: Command) -> str | None:
        if not self.connected or not self.driver.health():
            return "Unitree Go2 driver is disconnected or unhealthy"
        if command.robot_id != self.robot_id:
            return "Robot ID mismatch"
        if command.deadline <= utc_now():
            return "Command deadline has expired"
        capability = self._capabilities.get(command.skill)
        if capability is None:
            return f"Unsupported or unverified Go2 skill: {command.skill}"
        if command.risk_level != capability.risk_level:
            return "Command risk does not match the verified Go2 capability"
        if command.invocation_source.value not in capability.allowed_callers:
            return "Command source is not permitted for the verified Go2 capability"
        if capability.risk_level == RiskLevel.CONFIRM and not command.review_digest:
            return "Confirm-tier command is missing its review binding"
        if capability.risk_level == RiskLevel.CONFIRM and (
            command.policy_version != self.profile.profile_version
            or command.review_digest != command_proposal_digest(command, self.profile.profile_version)
        ):
            return "Confirm-tier command review binding is invalid or stale"
        if capability.risk_level == RiskLevel.HIGH and command.invocation_source.value != "dashboard":
            return "High-risk command requires Dashboard source"
        if capability.risk_level == RiskLevel.HIGH and (
            not command.operator_confirmation_id
            or command.policy_version != self.profile.profile_version
            or not command.operator_confirmed
            or not command.actor_hash
        ):
            return "High-risk command is missing authenticated operator confirmation"
        if set(command.parameters) != set(capability.parameters):
            return "Command parameters do not match the verified skill schema"
        for name, spec in capability.parameters.items():
            value = command.parameters[name]
            if spec.value_type == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                    return f"Parameter {name} must be a finite number"
                if spec.minimum is not None and value < spec.minimum:
                    return f"Parameter {name} is below its verified minimum"
                if spec.maximum is not None and value > spec.maximum:
                    return f"Parameter {name} exceeds its verified maximum"
            elif spec.value_type == "boolean":
                if not isinstance(value, bool):
                    return f"Parameter {name} must be boolean"
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


def _intersect_capabilities(
    profile: CapabilitySpec,
    device: CapabilitySpec,
) -> CapabilitySpec | None:
    if profile.risk_level != device.risk_level:
        return None
    if set(profile.parameters) != set(device.parameters):
        return None
    parameters = {}
    for name, profile_parameter in profile.parameters.items():
        device_parameter = device.parameters[name]
        if profile_parameter.value_type != device_parameter.value_type:
            return None
        minimums = [value for value in (profile_parameter.minimum, device_parameter.minimum) if value is not None]
        maximums = [value for value in (profile_parameter.maximum, device_parameter.maximum) if value is not None]
        minimum = max(minimums) if minimums else None
        maximum = min(maximums) if maximums else None
        if minimum is not None and maximum is not None and minimum > maximum:
            return None
        lengths = [value for value in (profile_parameter.max_length, device_parameter.max_length) if value is not None]
        parameters[name] = ParameterSpec(
            value_type=profile_parameter.value_type,
            minimum=minimum,
            maximum=maximum,
            max_length=min(lengths) if lengths else None,
        )
    allowed_callers = tuple(
        caller for caller in profile.allowed_callers if caller in device.allowed_callers
    )
    if not allowed_callers:
        return None
    return CapabilitySpec(
        name=profile.name,
        parameters=parameters,
        risk_level=profile.risk_level,
        allowed_callers=allowed_callers,
        requires_operator_confirmation=(profile.requires_operator_confirmation or device.requires_operator_confirmation),
        interruptible=profile.interruptible and device.interruptible,
        timeout_ms=min(profile.timeout_ms, device.timeout_ms),
        required=profile.required,
        description=profile.description,
        estimate_ms=device.estimate_ms or profile.estimate_ms,
        completion=device.completion if device.estimate_ms or device.completion != "accepted" else profile.completion,
    )
