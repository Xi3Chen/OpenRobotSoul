"""Optional single-call aiortc media bridge for the LAN Dashboard."""

from __future__ import annotations

import asyncio
from fractions import Fraction
from threading import Event, Lock, Thread
from typing import Any


class WebRtcUnavailable(RuntimeError):
    pass


class _Call:
    def __init__(self, peer: Any, track: Any, generation: int):
        self.peer = peer
        self.track = track
        self.generation = generation
        self.incoming: set[asyncio.Task[Any]] = set()
        self.outgoing: set[asyncio.Task[Any]] = set()
        self.disconnect_task: asyncio.Task[Any] | None = None


class _CallAudioSink:
    """PCM callback for one browser call. clear() drops audio already queued to play."""

    def __init__(self, bridge: AiortcCallMedia, call_id: str):
        self._bridge = bridge
        self._call_id = call_id

    def __call__(self, samples: Any, sample_rate: int) -> None:
        self._bridge._push(self._call_id, samples, sample_rate)

    def clear(self) -> None:
        self._bridge._clear_outgoing(self._call_id)


class AiortcCallMedia:
    """Terminates one authenticated browser call and routes PCM to VoiceService."""

    def __init__(self, voice_service: Any):
        self.voice_service = voice_service
        self._calls: dict[str, _Call] = {}
        self._lock = Lock()
        self._ready = Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = Thread(target=self._run_loop, name="voice-webrtc", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=3):
            raise WebRtcUnavailable("WebRTC event loop did not start")

    def _run_loop(self) -> None:
        try:
            from aiortc import MediaStreamTrack
            import av  # noqa: F401
        except ImportError as exc:
            self._ready.set()
            self._loop = None
            self._import_error = exc
            return
        self._track_type = self._make_track_type(MediaStreamTrack, av)
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    def _call(self, coroutine: Any) -> Any:
        if self._loop is None:
            raise WebRtcUnavailable("aiortc and PyAV are required for WebRTC calls") from getattr(self, "_import_error", None)
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=15)

    @property
    def available(self) -> bool:
        return self._loop is not None

    def answer(self, call_id: str, _session: Any, offer: dict[str, str]) -> dict[str, str]:
        if not self.available:
            raise WebRtcUnavailable("aiortc and PyAV are required for WebRTC calls")
        return self._call(self._answer(call_id, offer))

    @staticmethod
    def _make_track_type(base: Any, av: Any) -> type:
        class TtsTrack(base):
            kind = "audio"

            def __init__(self):
                super().__init__()
                self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=2000)
                self._timestamp = 0
                self._closed = False
                self._generation = 0

            def clear(self) -> None:
                self._generation += 1
                self._drop_queued()

            def _drop_queued(self) -> None:
                while not self._queue.empty():
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

            async def feed(self, samples: Any, sample_rate: int) -> None:
                if self._closed:
                    return
                self._generation += 1
                generation = self._generation
                self._drop_queued()
                frames = await asyncio.to_thread(self._encode_audio, samples, sample_rate, self._timestamp)
                if not frames or self._closed or generation != self._generation:
                    return
                self._timestamp = frames[-1].pts + 960
                for frame in frames:
                    if self._closed or generation != self._generation:
                        return
                    await self._queue.put(frame)

            @staticmethod
            def _encode_audio(samples: Any, sample_rate: int, start_pts: int) -> list[Any]:
                import numpy as np

                values = np.asarray(samples, dtype=np.float32).reshape(-1)
                if values.size == 0:
                    return []
                source_times = np.arange(values.size, dtype=np.float64) / sample_rate
                target_count = max(1, int(values.size * 48000 / sample_rate))
                target_times = np.arange(target_count, dtype=np.float64) / 48000
                values = np.interp(target_times, source_times, values)
                padded = np.pad(values, (0, (-len(values)) % 960))
                frames = []
                for index, chunk in enumerate(padded.reshape(-1, 960)):
                    pcm = np.clip(chunk * 32767.0, -32768, 32767).astype(np.int16)
                    frame = av.AudioFrame.from_ndarray(pcm.reshape(1, 960), format="s16", layout="mono")
                    frame.sample_rate = 48000
                    frame.pts = start_pts + index * 960
                    frame.time_base = Fraction(1, 48000)
                    frames.append(frame)
                return frames

            async def recv(self) -> Any:
                if self._closed:
                    raise asyncio.CancelledError
                await asyncio.sleep(0.02)
                try:
                    return self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    import numpy as np

                    frame = av.AudioFrame.from_ndarray(np.zeros((1, 960), dtype=np.int16), format="s16", layout="mono")
                    frame.sample_rate = 48000
                    frame.pts = self._timestamp
                    frame.time_base = Fraction(1, 48000)
                    self._timestamp += 960
                    return frame

            def close(self) -> None:
                self._closed = True
                self._generation += 1
                self._drop_queued()
                super().stop()

        return TtsTrack

    async def _answer(self, call_id: str, offer: dict[str, str]) -> dict[str, str]:
        from aiortc import RTCPeerConnection, RTCSessionDescription
        with self._lock:
            if call_id in self._calls:
                raise RuntimeError("a WebRTC answer has already been created for this call")
        peer = RTCPeerConnection()
        track = self._track_type()
        call = _Call(peer, track, self.voice_service.generation)
        peer.addTrack(track)

        @peer.on("track")
        def on_track(incoming: Any) -> None:
            if incoming.kind == "audio":
                task = asyncio.create_task(self._consume(call, incoming))
                call.incoming.add(task)
                task.add_done_callback(call.incoming.discard)

        @peer.on("connectionstatechange")
        async def on_connection_state_change() -> None:
            if peer.connectionState == "disconnected":
                try:
                    if self.voice_service.state.value == "CALL":
                        self.voice_service.call_disconnected()
                    if call.disconnect_task is not None:
                        call.disconnect_task.cancel()
                    call.disconnect_task = asyncio.create_task(self._expire_disconnected(call_id, call))
                except RuntimeError:
                    pass
            elif peer.connectionState == "connected" and self.voice_service.state.value == "CALL_RECONNECTING":
                if call.disconnect_task is not None:
                    call.disconnect_task.cancel()
                    call.disconnect_task = None
                call.generation = self.voice_service.call_reconnected()
            elif peer.connectionState == "connected" and self.voice_service.state.value == "CALL_PENDING":
                call.generation = self.voice_service.call_connected()
            elif peer.connectionState in {"failed", "closed"}:
                if call.disconnect_task is not None:
                    call.disconnect_task.cancel()
                    call.disconnect_task = None
                await self._expire_call(call_id, call)

        try:
            await peer.setRemoteDescription(RTCSessionDescription(sdp=offer["sdp"], type=offer["type"]))
            await peer.setLocalDescription(await peer.createAnswer())
            with self._lock:
                self._calls[call_id] = call
            self.voice_service.set_call_audio_sink(_CallAudioSink(self, call_id))
            description = peer.localDescription
            return {"sdp": description.sdp, "type": description.type}
        except Exception:
            track.close()
            await peer.close()
            raise

    async def _consume(self, call: _Call, incoming: Any) -> None:
        try:
            while True:
                frame = await incoming.recv()
                array = frame.to_ndarray(format="s16", layout="mono")
                values = array.reshape(-1).astype("float32") / 32768.0
                accepted = self.voice_service.accept_call_audio(call.generation, values, frame.sample_rate or 16000)
                if not accepted and self.voice_service.state.value not in {"CALL", "CALL_RECONNECTING"}:
                    await self._expire_call_for_peer(call)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._expire_call_for_peer(call)

    async def _expire_call_for_peer(self, call: _Call) -> None:
        with self._lock:
            call_id = next((key for key, value in self._calls.items() if value is call), None)
        if call_id is not None:
            await self._expire_call(call_id, call)

    async def _expire_disconnected(self, call_id: str, call: _Call) -> None:
        await asyncio.sleep(float(getattr(self.voice_service, "call_reconnect_grace_seconds", 15)))
        if call.peer.connectionState in {"disconnected", "failed"}:
            await self._expire_call(call_id, call)

    async def _expire_call(self, call_id: str, call: _Call) -> None:
        with self._lock:
            if self._calls.get(call_id) is not call:
                return
            self._calls.pop(call_id, None)
        call.track.close()
        current = asyncio.current_task()
        if call.disconnect_task is not None and call.disconnect_task is not current:
            call.disconnect_task.cancel()
        for task in call.incoming:
            if task is not current:
                task.cancel()
        for task in call.outgoing:
            task.cancel()
        pending = [task for task in (*call.incoming, *call.outgoing) if task is not current]
        await asyncio.gather(*pending, return_exceptions=True)
        await call.peer.close()
        self.voice_service.set_call_audio_sink(None)
        if self.voice_service.state.value in {"CALL_PENDING", "CALL", "CALL_RECONNECTING"}:
            try:
                await asyncio.to_thread(self.voice_service.end_call)
            except RuntimeError:
                pass

    def _push(self, call_id: str, samples: Any, sample_rate: int) -> None:
        with self._lock:
            call = self._calls.get(call_id)
        if call is None or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._start_feed, call, samples, sample_rate)
        except RuntimeError:
            return

    def _clear_outgoing(self, call_id: str) -> None:
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._clear_outgoing_now, call_id)
        except RuntimeError:
            return

    def _clear_outgoing_now(self, call_id: str) -> None:
        with self._lock:
            call = self._calls.get(call_id)
        if call is None:
            return
        clear = getattr(call.track, "clear", None)
        if callable(clear):
            clear()

    @staticmethod
    def _start_feed(call: _Call, samples: Any, sample_rate: int) -> None:
        if call.peer.connectionState in {"closed", "failed"}:
            return
        task = asyncio.create_task(call.track.feed(samples, sample_rate))
        call.outgoing.add(task)
        task.add_done_callback(call.outgoing.discard)

    def close(self, call_id: str) -> None:
        if self._loop is not None:
            self._call(self._close(call_id))

    async def _close(self, call_id: str) -> None:
        with self._lock:
            call = self._calls.pop(call_id, None)
        if call is None:
            return
        call.track.close()
        if call.disconnect_task is not None:
            call.disconnect_task.cancel()
        current = asyncio.current_task()
        for task in call.incoming:
            task.cancel()
        for task in call.outgoing:
            task.cancel()
        pending = [task for task in (*call.incoming, *call.outgoing) if task is not current]
        await asyncio.gather(*pending, return_exceptions=True)
        await call.peer.close()
        self.voice_service.set_call_audio_sink(None)

    def shutdown(self) -> None:
        if self._loop is None:
            return
        self._call(self._close_all())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)

    async def _close_all(self) -> None:
        for call_id in list(self._calls):
            await self._close(call_id)
