"""Compact sensor readings forwarded to the language model."""

from __future__ import annotations

from typing import Any


def language_sensor_context(observations: tuple[Any, ...]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for observation in observations:
        event_type = getattr(observation, "event_type", "")
        payload = getattr(observation, "payload", None)
        if not isinstance(event_type, str) or not event_type.startswith("robot."):
            continue
        if not isinstance(payload, dict) or not payload or not _compact(payload):
            continue
        observed_at = getattr(observation, "observed_at", None)
        stamp = observed_at.isoformat() if hasattr(observed_at, "isoformat") else ""
        items.append({"event_type": event_type, "observed_at": stamp, "reading": payload})
        if len(items) >= 12:
            break
    return items


def _compact(payload: dict[str, Any]) -> bool:
    for value in payload.values():
        if isinstance(value, (list, dict)) and len(value) > 16:
            return False
        if isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, (list, dict)) and len(nested) > 16:
                    return False
    return True
