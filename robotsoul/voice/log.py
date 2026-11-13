"""Durable wake and transcript records for the local voice service."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4


DEFAULT_VOICE_EVENT_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "voice_events.jsonl"
_MAX_TEXT_LENGTH = 1000
_ALLOWED_KINDS = {"wake", "transcript"}
_ALLOWED_SOURCES = {"local", "call"}


class VoiceEventLogError(ValueError):
    pass


def _event(kind: str, text: str, source: str, state: str) -> dict[str, Any]:
    if kind not in _ALLOWED_KINDS:
        raise VoiceEventLogError(f"Unsupported voice event kind: {kind}")
    if source not in _ALLOWED_SOURCES:
        raise VoiceEventLogError(f"Unsupported voice event source: {source}")
    if not isinstance(text, str) or not text.strip():
        raise VoiceEventLogError("Voice event text must not be empty")
    if not isinstance(state, str) or not state.strip():
        raise VoiceEventLogError("Voice event state must not be empty")
    clipped = text.strip()
    if len(clipped) > _MAX_TEXT_LENGTH:
        clipped = clipped[:_MAX_TEXT_LENGTH]
    return {
        "event_id": str(uuid4()),
        "kind": kind,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "state": state.strip(),
        "text": clipped,
    }


class InMemoryVoiceEventLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._lock = Lock()

    def record(self, *, kind: str, text: str, source: str, state: str) -> dict[str, Any]:
        event = _event(kind, text, source, state)
        with self._lock:
            self.events.append(event)
        return deepcopy(event)

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        size = _limit(limit)
        with self._lock:
            return [deepcopy(item) for item in reversed(self.events[-size:])]


class JsonlVoiceEventLog:
    """Append one wake or transcript event per line."""

    def __init__(self, path: str | Path = DEFAULT_VOICE_EVENT_LOG_PATH) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def record(self, *, kind: str, text: str, source: str, state: str) -> dict[str, Any]:
        event = _event(kind, text, source, state)
        payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("Voice event log write made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return event

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        size = _limit(limit)
        with self._lock:
            if not self.path.is_file():
                return []
            raw = self.path.read_bytes()
        events: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("kind") in _ALLOWED_KINDS and isinstance(item.get("text"), str):
                events.append(item)
        return list(reversed(events[-size:]))


def _limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise VoiceEventLogError("Voice event limit must be an integer")
    if limit < 1 or limit > 100:
        raise VoiceEventLogError("Voice event limit must be between 1 and 100")
    return limit
