"""Shared robot adapter protocol and registered adapter selection."""

from robotsoul.adapters.base import RobotAdapter
from robotsoul.adapters.registry import create_adapter

__all__ = ["RobotAdapter", "create_adapter"]
