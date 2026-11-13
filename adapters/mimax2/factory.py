from __future__ import annotations

from adapters.mimax2.adapter import MiMax2Adapter
from adapters.mimax2.driver import MiMax2FlashDriver
from robotsoul.profiles import RobotProfile


def create_adapter(profile: RobotProfile) -> MiMax2Adapter:
    return MiMax2Adapter(profile, MiMax2FlashDriver(profile.robot_id))
