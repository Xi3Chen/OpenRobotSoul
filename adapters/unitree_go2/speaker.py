"""Go2 speaker transport.

The virtual output is an in-process PCM queue. A long-lived WebRTC audio track,
negotiated when the adapter connects, pulls 20 ms frames from that queue.
Stopping or replacing an utterance discards queued frames so the previous
sentence does not finish. Motion control stays on the DDS bridge; a speaker
failure does not disconnect it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Sequence

from robotsoul.runlog import log as runtime_log
from robotsoul.voice.audio import InterruptibleOutput
from robotsoul.voice.outputs import GO2_OUTPUT, register_output, unregister_output


_FRAME_SECONDS = 0.02
_PLAYBACK_RATE = 48000
_FRAME_SAMPLES = 960
_CONNECT_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class PcmChunk:
    samples: tuple[float, ...]
    sample_rate: int
    generation: int


class Go2PcmQueue:
    """Newest utterance only. Older chunks are removed as soon as a new one is submitted."""

    def __init__(self) -> None:
        self._items: deque[PcmChunk] = deque()
        self._generation = 0
        self._lock = Lock()

    def submit(self, samples: Sequence[float], sample_rate: int) -> int:
        if sample_rate <= 0:
            raise ValueError("audio sample rate must be positive")
        pieces = _chunk_samples(samples, sample_rate)
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._items.clear()
            self._items.extend(PcmChunk(piece, sample_rate, generation) for piece in pieces)
            return generation

    def pending(self, generation: int) -> bool:
        with self._lock:
            return any(item.generation == generation for item in self._items)

    def chunk_samples(self) -> tuple[tuple[float, ...], ...]:
        with self._lock:
            return tuple(item.samples for item in self._items)

    def pop(self) -> PcmChunk | None:
        with self._lock:
            if not self._items:
                return None
            return self._items.popleft()

    def discard(self, generation: int) -> None:
        with self._lock:
            if self._generation != generation:
                return
            self._items.clear()

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._items.clear()


class Go2PcmOutput:
    """Blocking playback backend. It returns when the track has taken every chunk or the utterance is cancelled."""

    def __init__(self, queue: Go2PcmQueue):
        self.queue = queue

    def play(self, samples: Sequence[float], sample_rate: int, cancel: Event | None = None) -> None:
        if not samples:
            return
        generation = self.queue.submit(samples, sample_rate)
        while self.queue.pending(generation):
            if cancel is not None and cancel.is_set():
                self.queue.discard(generation)
                return
            time.sleep(0.02)

    def stop(self) -> None:
        self.queue.clear()

    def close(self) -> None:
        self.stop()


class Go2Speaker:
    """Owns the WebRTC audio connection and the registered virtual output."""

    def __init__(self, ip: str, aes_key: str | None = None):
        if not ip or ip.strip() != ip:
            raise ValueError("Go2 speaker IP must be a non-empty address")
        self.ip = ip
        self.aes_key = aes_key
        self.queue = Go2PcmQueue()
        self.output = InterruptibleOutput(Go2PcmOutput(self.queue))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._connection: Any | None = None
        self._ready = Event()
        self._error: BaseException | None = None
        self._closed = False

    def start(self) -> None:
        self._thread = Thread(target=self._run, name="go2-speaker", daemon=True)
        self._thread.start()
        if not self._ready.wait(_CONNECT_TIMEOUT_SECONDS + 5):
            self.close()
            raise TimeoutError(f"Go2 speaker WebRTC connection timed out for {self.ip}")
        if self._error is not None:
            error = self._error
            self.close()
            raise error
        register_output(GO2_OUTPUT, self.output)
        runtime_log("Go2", "Info", f"机身喇叭已登记 ip={self.ip}")

    def close(self) -> None:
        self._closed = True
        unregister_output(GO2_OUTPUT, self.output)
        try:
            self.output.close()
        except Exception:
            pass
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=5)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not Thread.current_thread():
            thread.join(timeout=5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(asyncio.wait_for(self._connect(), _CONNECT_TIMEOUT_SECONDS))
        except Exception as exc:
            self._error = exc
            self._finish_loop(loop)
            self._ready.set()
            return
        if self._closed:
            self._finish_loop(loop)
            self._ready.set()
            return
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            self._finish_loop(loop)

    def _finish_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending and not loop.is_running():
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        if not loop.is_closed():
            loop.close()
        if self._loop is loop:
            self._loop = None

    async def _connect(self) -> None:
        connection_type, method, audio_channel = _load_webrtc()
        track = _pcm_track_type()(self.queue)
        original_init = audio_channel.__init__

        def patched_init(channel: Any, peer: Any, datachannel: Any) -> None:
            original_init(channel, peer, datachannel)
            _attach_pcm_track(peer, track)

        audio_channel.__init__ = patched_init
        try:
            connection = connection_type(method.LocalSTA, ip=self.ip, aes_128_key=self.aes_key)
            self._connection = connection
            await connection.connect()
            switch = getattr(getattr(connection, "audio", None), "switchAudioChannel", None)
            if callable(switch):
                switch(True)
        finally:
            audio_channel.__init__ = original_init

    async def _shutdown(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            disconnect = getattr(connection, "disconnect", None)
            if callable(disconnect):
                await disconnect()


def start_go2_speaker() -> Go2Speaker | None:
    """Register the Go2 virtual output when OPENROBOTSOUL_GO2_WEBRTC_IP is set."""
    ip = os.environ.get("OPENROBOTSOUL_GO2_WEBRTC_IP", "").strip()
    if not ip:
        return None
    key = os.environ.get("OPENROBOTSOUL_GO2_AES_KEY", "").strip() or None
    speaker = Go2Speaker(ip, key)
    speaker.start()
    return speaker


def _chunk_samples(samples: Sequence[float], sample_rate: int) -> tuple[tuple[float, ...], ...]:
    frame_count = max(1, int(sample_rate * _FRAME_SECONDS))
    values = tuple(float(value) for value in samples)
    if not values:
        return ()
    chunks = []
    for offset in range(0, len(values), frame_count):
        piece = values[offset:offset + frame_count]
        if len(piece) < frame_count:
            piece = piece + (0.0,) * (frame_count - len(piece))
        chunks.append(piece)
    return tuple(chunks)


def _resample(samples: tuple[float, ...], source_rate: int, target_rate: int) -> tuple[float, ...]:
    if not samples or source_rate == target_rate:
        return samples
    count = max(1, int(round(len(samples) * target_rate / source_rate)))
    if count == 1:
        return (samples[0],)
    scale = (len(samples) - 1) / (count - 1)
    converted = []
    for index in range(count):
        position = index * scale
        left = int(position)
        right = min(left + 1, len(samples) - 1)
        fraction = position - left
        converted.append(samples[left] * (1.0 - fraction) + samples[right] * fraction)
    return tuple(converted)


def _load_webrtc() -> tuple[Any, Any, Any]:
    _ensure_sounddevice_importable()
    root = Path(__file__).resolve().parent / "third_party" / "unitree_webrtc_connect"
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from unitree_webrtc_connect.webrtc_audio import WebRTCAudioChannel
    from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod

    return UnitreeWebRTCConnection, WebRTCConnectionMethod, WebRTCAudioChannel


def _ensure_sounddevice_importable() -> None:
    """The WebRTC library imports sounddevice even though playback does not use it."""
    try:
        import sounddevice  # noqa: F401
    except (ImportError, OSError):
        import types

        sys.modules["sounddevice"] = types.ModuleType("sounddevice")


def _attach_pcm_track(peer: Any, track: Any) -> None:
    for transceiver in peer.getTransceivers():
        if transceiver.kind == "audio":
            transceiver.sender.replaceTrack(track)
            return
    raise RuntimeError("Go2 WebRTC connection has no audio transceiver")


def _pcm_track_type() -> type:
    try:
        import av
        import numpy as np
        from aiortc import MediaStreamTrack
        from aiortc.mediastreams import MediaStreamError
    except ImportError as exc:
        raise RuntimeError("av, numpy and aiortc are required for the Go2 speaker track") from exc

    class PcmTrack(MediaStreamTrack):
        kind = "audio"

        def __init__(self, queue: Go2PcmQueue):
            super().__init__()
            self._queue = queue
            self._pts = 0
            self._origin: float | None = None
            self._sent = 0
            self._av = av
            self._np = np

        async def recv(self) -> Any:
            if self.readyState != "live":
                raise MediaStreamError
            if self._origin is None:
                self._origin = time.monotonic()
            else:
                self._sent += 1
                delay = self._origin + self._sent * _FRAME_SECONDS - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            chunk = self._queue.pop()
            samples = _silence() if chunk is None else _playback_samples(chunk)
            frame = _audio_frame(self._av, self._np, samples)
            frame.pts = self._pts
            self._pts += _FRAME_SAMPLES
            return frame

    return PcmTrack


def _silence() -> tuple[float, ...]:
    return (0.0,) * _FRAME_SAMPLES


def _playback_samples(chunk: PcmChunk) -> tuple[float, ...]:
    converted = _resample(chunk.samples, chunk.sample_rate, _PLAYBACK_RATE)
    if len(converted) > _FRAME_SAMPLES:
        return converted[:_FRAME_SAMPLES]
    if len(converted) < _FRAME_SAMPLES:
        return converted + (0.0,) * (_FRAME_SAMPLES - len(converted))
    return converted


def _audio_frame(av: Any, np: Any, samples: tuple[float, ...]) -> Any:
    pcm = np.clip(np.asarray(samples, dtype=np.float32) * 32767.0, -32768, 32767).astype(np.int16)
    frame = av.AudioFrame.from_ndarray(pcm.reshape(1, _FRAME_SAMPLES), format="s16", layout="mono")
    frame.sample_rate = _PLAYBACK_RATE
    frame.time_base = Fraction(1, _PLAYBACK_RATE)
    return frame

