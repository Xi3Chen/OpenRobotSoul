from __future__ import annotations

from adapters.unitree_go2.adapter import UnitreeGo2Adapter
from adapters.unitree_go2.sdk_driver import create_sdk_driver
from robotsoul.profiles import RobotProfile


def create_driver(profile: RobotProfile):
    """Create the read-only driver for the pinned Unitree SDK2 bridge."""
    return create_sdk_driver(profile)


def create_adapter(profile: RobotProfile) -> UnitreeGo2Adapter:
    return UnitreeGo2Adapter(profile, create_driver(profile))
