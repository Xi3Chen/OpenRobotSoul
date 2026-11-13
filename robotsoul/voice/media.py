from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from typing import Any, Callable

from robotsoul.voice.webrtc import AiortcCallMedia, WebRtcUnavailable


@dataclass(frozen=True)
class _Request:
    kind: str
    payload: dict[str, Any] | None
    result: list[Any]
    done: Event


class VoiceMediaBridge:
    """Runs aiortc on its own asyncio thread, separate from HTTP and ASR."""

    def __init__(self, voice_service: Any):
        self.voice_service = voice_service
        self.media = AiortcCallMedia(voice_service)

    def answer(self, call_id: str, session: Any, offer: dict[str, str]) -> dict[str, str]:
        if not self.available:
            raise WebRtcUnavailable("aiortc and PyAV are required for WebRTC calls")
        return self.media.answer(call_id, session, offer)

    @property
    def available(self) -> bool:
        return self.media.available

    def close(self, call_id: str) -> None:
        self.media.close(call_id)

    def shutdown(self) -> None:
        self.media.shutdown()

