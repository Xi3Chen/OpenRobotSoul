from __future__ import annotations

from uuid import uuid4

from adapters.mimax2.driver import MiMax2FlashDriver
from robotsoul.adapters.base import BatteryStatus
from robotsoul.models import CapabilitySpec, Command, Feedback, FeedbackStatus, ParameterSpec, RiskLevel, utc_now
from robotsoul.profiles import RobotProfile


class MiMax2Adapter:
    """Profile-bounded Linux phone adapter exposing only the flash action."""

    def __init__(self, profile: RobotProfile, driver: MiMax2FlashDriver):
        if profile.robot_module != "mimax2":
            raise ValueError("MiMax2Adapter requires a mimax2 profile")
        self.profile = profile
        self.driver = driver
        self.robot_id = profile.robot_id
        self.connected = False
        self._capabilities: dict[str, CapabilitySpec] = {}

    def connect(self) -> None:
        self.driver.connect()
        if not self.driver.health() or self.driver.identity() != (self.profile.manufacturer, self.profile.model):
            self.driver.close()
            raise RuntimeError("Connected device identity does not match the MiMax2 profile")
        self.connected = True

    def health(self) -> bool:
        return self.connected and self.driver.health()

    def battery(self) -> BatteryStatus | None:
        return None

    def identity(self) -> tuple[str, str]:
        return self.driver.identity()

    def discover_capabilities(self) -> dict[str, CapabilitySpec]:
        if not self.connected:
            raise RuntimeError("MiMax2 adapter is not connected")
        declared = {item.name: item for item in self.profile.capabilities}
        if "flash.toggle" not in declared:
            self._capabilities = {}
            return {}
        self._capabilities = {
            "flash.toggle": CapabilitySpec(
                name="flash.toggle",
                parameters={"enabled": ParameterSpec("boolean")},
                risk_level=RiskLevel.NORMAL,
                allowed_callers=("agent", "dashboard"),
                requires_operator_confirmation=False,
                interruptible=True,
                timeout_ms=min(declared["flash.toggle"].timeout_ms, 1500),
                required=True,
            )
        }
        return dict(self._capabilities)

    def drain_observations(self) -> tuple[object, ...]:
        return ()

    def execute(self, command: Command) -> tuple[Feedback, ...]:
        error = self._validate(command)
        if error:
            return (self._feedback(command, FeedbackStatus.FAILED, error, "command_rejected"),)
        try:
            self.driver.set_flash(command.parameters["enabled"])
        except Exception as exc:
            return (self._feedback(command, FeedbackStatus.FAILED, f"MiMax2 flash action failed: {exc}", "device_error"),)
        return (
            self._feedback(command, FeedbackStatus.ACCEPTED, "MiMax2 flash action accepted"),
            self._feedback(command, FeedbackStatus.RUNNING, "MiMax2 flash action running"),
            self._feedback(
                command,
                FeedbackStatus.SUCCEEDED,
                "MiMax2 flashlight on" if command.parameters["enabled"] else "MiMax2 flashlight off",
                evidence={"enabled": command.parameters["enabled"]},
            ),
        )

    def cancel(self, command: Command) -> Feedback:
        return self._feedback(command, FeedbackStatus.FAILED, "MiMax2 flash action is not running", "no_active_command")

    def close(self) -> None:
        try:
            self.driver.close()
        finally:
            self.connected = False

    def _validate(self, command: Command) -> str | None:
        if not self.health():
            return "MiMax2 adapter is disconnected"
        if command.robot_id != self.robot_id:
            return "Robot ID mismatch"
        if command.deadline <= utc_now():
            return "Command deadline has expired"
        capability = self._capabilities.get(command.skill)
        if capability is None:
            return f"Unsupported MiMax2 skill: {command.skill}"
        if command.risk_level != capability.risk_level:
            return "Command risk does not match the capability"
        if command.invocation_source.value not in capability.allowed_callers:
            return "Command source is not permitted"
        if set(command.parameters) != {"enabled"} or not isinstance(command.parameters["enabled"], bool):
            return "Parameter enabled must be boolean"
        return None

    @staticmethod
    def _feedback(command: Command, status: FeedbackStatus, message: str, error_code: str | None = None, evidence: dict | None = None) -> Feedback:
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
