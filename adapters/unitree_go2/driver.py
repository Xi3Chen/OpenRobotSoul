from __future__ import annotations

from typing import Protocol

from robotsoul.models import CapabilitySpec, Command, Feedback, Observation


class UnitreeGo2Driver(Protocol):
    """Normalized bridge implemented around the selected vendor SDK version.

    Implementations own SDK-specific imports, connection/authentication details,
    unit/frame conversion, hardware feedback mapping, and local stop behavior.
    A zero Sport API code means the service accepted the request. It does not prove
    that the body finished the pose.
    Report only skills supported by the connected device/firmware; the adapter
    intersects that list with profile-declared safety bounds before exposure.
    """

    robot_id: str

    def connect(self) -> None: ...

    def health(self) -> bool: ...

    def identity(self) -> tuple[str, str]: ...

    def discover_capabilities(self) -> dict[str, CapabilitySpec]: ...

    def drain_observations(self) -> tuple[Observation, ...]: ...

    def execute(self, command: Command) -> tuple[Feedback, ...]: ...

    def cancel(self, command: Command) -> Feedback: ...

    def close(self) -> None: ...
