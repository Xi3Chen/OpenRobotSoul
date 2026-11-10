from __future__ import annotations

from adapters.unitree_go2.adapter import UnitreeGo2Adapter
from adapters.unitree_go2.driver import UnitreeGo2Driver
from robotsoul.profiles import RobotProfile


def create_driver(profile: RobotProfile) -> UnitreeGo2Driver:
    """Build the normalized bridge around the installed Unitree SDK/driver.

    Implement SDK imports and robot connection setup in this module after the
    exact vendor package and firmware combination has been selected.
    """
    raise RuntimeError(
        "Unitree Go2 SDK bridge is not configured yet. Implement "
        "adapters/unitree_go2/factory.py:create_driver for the selected SDK."
    )


def create_adapter(profile: RobotProfile) -> UnitreeGo2Adapter:
    return UnitreeGo2Adapter(profile, create_driver(profile))
