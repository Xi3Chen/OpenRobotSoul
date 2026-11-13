"""Shared robot adapter protocol and registered adapter selection."""

from robotsoul.adapters.base import BatteryStatus, RobotAdapter
from robotsoul.adapters.registry import create_adapter

__all__ = ["BatteryStatus", "RobotAdapter", "create_adapter"]
