from __future__ import annotations

from typing import Callable

from adapters.simulator import create_adapter as create_simulator_adapter
from adapters.unitree_go2 import create_adapter as create_unitree_go2_adapter
from robotsoul.adapters.base import RobotAdapter
from robotsoul.profiles import RobotProfile


AdapterFactory = Callable[[RobotProfile], RobotAdapter]

# Deployment config selects an ID here; it never supplies a Python import path.
ADAPTER_FACTORIES: dict[str, AdapterFactory] = {
    "simulator": create_simulator_adapter,
    "unitree_go2": create_unitree_go2_adapter,
}


def create_adapter(profile: RobotProfile) -> RobotAdapter:
    try:
        factory = ADAPTER_FACTORIES[profile.robot_module]
    except KeyError as exc:
        raise ValueError(f"No adapter is registered for robot module {profile.robot_module!r}") from exc
    return factory(profile)
