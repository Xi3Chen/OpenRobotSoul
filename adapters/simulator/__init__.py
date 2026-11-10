"""Fake robot module used for contract tests and local development."""

from adapters.simulator.adapter import FakeRobotAdapter, create_adapter

__all__ = ["FakeRobotAdapter", "create_adapter"]
