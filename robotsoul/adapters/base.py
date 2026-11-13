from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from robotsoul.models import CapabilitySpec, Command, Feedback, Observation


@dataclass(frozen=True)
class BatteryStatus:
    """Latest battery reading reported by a robot.

    ``level_percent`` is the state of charge from 0 through 100.
    ``charging`` is true while the device reports that it is charging, false
    when it reports that it is not, and None when the level is known but the
    charge state is not.
    """

    level_percent: int
    charging: bool | None = None

    def __post_init__(self) -> None:
        level = self.level_percent
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100:
            raise ValueError("battery level must be an integer percent from 0 to 100")
        if self.charging is not None and not isinstance(self.charging, bool):
            raise ValueError("charging state must be true, false, or unknown")


class RobotAdapter(Protocol):
    """Synchronous adapter contract; implementations must enforce command deadlines locally."""

    robot_id: str

    def connect(self) -> None: ...

    def health(self) -> bool: ...

    def battery(self) -> BatteryStatus | None:
        """Return the latest battery reading, or None when this robot does not provide one."""
        return None

    def identity(self) -> tuple[str, str]: ...

    def discover_capabilities(self) -> dict[str, CapabilitySpec]: ...

    def drain_observations(self) -> tuple[Observation, ...]: ...

    def execute(self, command: Command) -> tuple[Feedback, ...]:
        """Return before command.deadline and fail safe on driver/network timeout."""
        ...

    def cancel(self, command: Command) -> Feedback: ...

    def close(self) -> None: ...
