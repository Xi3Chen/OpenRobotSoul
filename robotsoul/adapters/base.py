from __future__ import annotations

from typing import Protocol

from robotsoul.models import CapabilitySpec, Command, Feedback, Observation


class RobotAdapter(Protocol):
    """Synchronous adapter contract; implementations must enforce command deadlines locally."""

    robot_id: str

    def connect(self) -> None: ...

    def health(self) -> bool: ...

    def identity(self) -> tuple[str, str]: ...

    def discover_capabilities(self) -> dict[str, CapabilitySpec]: ...

    def drain_observations(self) -> tuple[Observation, ...]: ...

    def execute(self, command: Command) -> tuple[Feedback, ...]:
        """Return before command.deadline and fail safe on driver/network timeout."""
        ...

    def cancel(self, command: Command) -> Feedback: ...

    def close(self) -> None: ...
