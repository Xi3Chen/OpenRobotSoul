"""Lazy sounddevice adapters. Importing this module never probes or opens hardware."""

from __future__ import annotations

import inspect
import os
from array import array
import shutil
import subprocess
from threading import Event, Lock, Thread
from typing import Any, Callable, Sequence


class VoiceAudioError(RuntimeError):
    pass


def _sounddevice() -> Any:
    try:
        import sounddevice
    except ImportError as exc:
        raise VoiceAudioError("sounddevice is required for local audio I/O") from exc
    return sounddevice


def resolve_pulse_source(device: str | None) -> str | None:
    """Map a voice device id to a PulseAudio source name."""
    if device in (None, "", "default"):
        return os.environ.get("ROBOTSOUL_VOICE_PULSE_SOURCE") or None
    if device != "usb-microphone":
        return device
    return _pulse_usb_source()


def _pulse_usb_source() -> str | None:
    pactl = shutil.which("pactl")
    if pactl is None:
        return None
    try:
        completed = subprocess.run(
            [pactl, "list", "sources", "short"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    usb_sources: list[str] = []
    for line in completed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1].strip()
        lowered = name.lower()
        if not name or name.endswith(".monitor") or "usb" not in lowered:
            continue
        usb_sources.append(name)
    for name in usb_sources:
        if "wireless_mic" in name.lower():
            return name
    return usb_sources[0] if usb_sources else None


def _resolve_device(sd: Any, device: Any, direction: str) -> Any:
    aliases = {"usb-microphone": ("input", "max_input_channels"), "usb-speaker": ("output", "max_output_channels")}
    if device not in aliases:
        return device
    expected_direction, channel_key = aliases[device]
    if direction != expected_direction:
        raise VoiceAudioError(f"{device} cannot be used as an audio {direction} device")
    try:
        for index, info in enumerate(sd.query_devices()):
            if "usb" in str(info.get("name", "")).lower() and int(info.get(channel_key, 0)) > 0:
                return index
    except Exception as exc:
        raise VoiceAudioError(f"Unable to enumerate USB {direction} devices: {exc}") from exc
    return None


class SoundDeviceCapture:
    def __init__(self, callback: Callable[[Sequence[float], int], None], device: Any = None,
                 sample_rate: int = 16000, block_duration: float = 0.1, channels: int = 1):
        self.callback = callback
        self.device = device
        self.sample_rate = sample_rate
        self.block_duration = block_duration
        self.channels = channels
        self._stream: Any = None

    def set_callback(self, callback: Callable[[Sequence[float], int], None]) -> None:
        self.callback = callback

    def probe(self) -> bool:
        sd = _sounddevice()
        try:
            device = _resolve_device(sd, self.device, "input")
            if self.device == "usb-microphone" and device is None:
                return False
            info = sd.query_devices(device, "input")
            return int(info.get("max_input_channels", 0)) >= self.channels
        except Exception:
            return False

    def open(self) -> None:
        if self._stream is not None:
            return
        sd = _sounddevice()
        device = _resolve_device(sd, self.device, "input")
        if self.device == "usb-microphone" and device is None:
            raise VoiceAudioError("No USB microphone is available")
        if not self.probe():
            raise VoiceAudioError(f"No input device with {self.channels} channel(s) available")

        def on_audio(indata: Any, _frames: int, _time: Any, status: Any) -> None:
            if status:
                return
            # Own the callback data before returning to PortAudio.
            copied = indata.copy()
            if getattr(copied, "ndim", 1) > 1:
                copied = copied[:, 0]
            if hasattr(copied, "tolist"):
                copied = copied.tolist()
            self.callback(tuple(float(value) for value in copied), self.sample_rate)

        try:
            self._stream = sd.InputStream(device=device, samplerate=self.sample_rate,
                                          channels=self.channels, dtype="float32",
                                          blocksize=max(1, int(self.sample_rate * self.block_duration)),
                                          callback=on_audio)
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise VoiceAudioError(f"Unable to open input audio device: {exc}") from exc

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()


class SoundDeviceOutput:
    def __init__(self, device: Any = None):
        self.device = device

    def play(self, samples: Sequence[float], sample_rate: int, cancel: Event | None = None) -> None:
        if not samples:
            return
        sd = _sounddevice()
        device = _resolve_device(sd, self.device, "output")
        if self.device == "usb-speaker" and device is None:
            raise VoiceAudioError("No USB speaker is available")
        try:
            sd.play(samples, samplerate=sample_rate, device=device, blocking=False)
            if cancel is None:
                wait = getattr(sd, "wait", None)
                if callable(wait):
                    wait()
                return
            while not cancel.is_set():
                active = _sounddevice_active(sd)
                if active is False:
                    return
                cancel.wait(0.02)
        except Exception as exc:
            raise VoiceAudioError(f"Unable to play audio: {exc}") from exc

    def stop(self) -> None:
        _sounddevice().stop()

    def close(self) -> None:
        self.stop()


class PulseAudioCapture:
    """Linux PulseAudio capture adapter used on phones without PortAudio."""

    def __init__(self, callback: Callable[[Sequence[float], int], None], device: str | None = None,
                 sample_rate: int = 48000, block_duration: float = 0.1):
        self.callback = callback
        self.device = device if device is not None else os.environ.get("ROBOTSOUL_VOICE_PULSE_SOURCE")
        self.sample_rate = sample_rate
        self.block_bytes = max(2, int(sample_rate * block_duration) * 2)
        self._process: subprocess.Popen[bytes] | None = None
        self._stop = Event()
        self._thread: Thread | None = None

    def set_callback(self, callback: Callable[[Sequence[float], int], None]) -> None:
        self.callback = callback

    def probe(self) -> bool:
        return shutil.which("parec") is not None and shutil.which("pactl") is not None

    def open(self) -> None:
        if self._process is not None:
            return
        if not self.probe():
            raise VoiceAudioError("parec and pactl are required for PulseAudio capture")
        source = resolve_pulse_source(self.device)
        if self.device == "usb-microphone" and not source:
            raise VoiceAudioError("No USB microphone is available")
        self.device = source
        command = ["parec", "--raw", "--format=s16le", f"--rate={self.sample_rate}", "--channels=1"]
        if source:
            command.append(f"--device={source}")
        try:
            self._process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise VoiceAudioError(f"Unable to open PulseAudio input: {exc}") from exc
        self._stop.clear()
        self._thread = Thread(target=self._read, name="pulseaudio-capture", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        while not self._stop.is_set():
            data = process.stdout.read(self.block_bytes)
            if not data:
                return
            samples = array("h")
            samples.frombytes(data[: len(data) - (len(data) % 2)])
            self.callback(tuple(value / 32768.0 for value in samples), self.sample_rate)

    def close(self) -> None:
        self._stop.set()
        process, self._process = self._process, None
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1)


class PulseAudioOutput:
    """Linux PulseAudio playback adapter used on phones without PortAudio."""

    def __init__(self, device: str | None = None):
        self.device = device or os.environ.get("ROBOTSOUL_VOICE_PULSE_SINK")
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = Lock()

    def play(self, samples: Sequence[float], sample_rate: int, cancel: Event | None = None) -> None:
        if not samples:
            return
        if shutil.which("pacat") is None:
            raise VoiceAudioError("pacat is required for PulseAudio playback")
        values = array("h", (max(-32768, min(32767, int(float(value) * 32767.0))) for value in samples))
        command = ["pacat", "--raw", "--format=s16le", f"--rate={sample_rate}", "--channels=1"]
        if self.device:
            command.append(f"--device={self.device}")
        data = values.tobytes()
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            with self._lock:
                self._process = process
            if cancel is None:
                process.communicate(data, timeout=max(2.0, len(values) / sample_rate + 2.0))
                return
            writer = Thread(target=_write_pacat, args=(process, data), name="pulseaudio-playback", daemon=True)
            writer.start()
            while writer.is_alive() or process.poll() is None:
                if cancel.is_set():
                    process.kill()
                    break
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    continue
            writer.join(timeout=1)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if process is not None and process.poll() is None:
                process.kill()
            raise VoiceAudioError(f"Unable to play through PulseAudio: {exc}") from exc
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.kill()

    def close(self) -> None:
        self.stop()


def _write_pacat(process: subprocess.Popen[bytes], data: bytes) -> None:
    try:
        if process.stdin is None:
            return
        process.stdin.write(data)
        process.stdin.close()
    except Exception:
        return


def _sounddevice_active(sd: Any) -> bool | None:
    get_stream = getattr(sd, "get_stream", None)
    if not callable(get_stream):
        return None
    try:
        return bool(get_stream().active)
    except Exception:
        return False


class InterruptibleOutput:
    """One current utterance. A new play() or stop() drops whatever is still queued."""

    def __init__(self, backend: Any):
        self._backend = backend
        self._state = Lock()
        self._playback = Lock()
        self._generation = 0
        self._cancel = Event()

    def play(self, samples: Sequence[float], sample_rate: int) -> bool:
        """Play one utterance. Returns True when this utterance is replaced or stopped."""
        if not samples:
            return False
        my_cancel = Event()
        with self._state:
            self._generation += 1
            token = self._generation
            previous = self._cancel
            self._cancel = my_cancel
        previous.set()
        _stop_backend(self._backend)
        with self._playback:
            with self._state:
                if token != self._generation:
                    return True
            _invoke_play(self._backend, samples, sample_rate, my_cancel)
        with self._state:
            return token != self._generation or my_cancel.is_set()

    def stop(self) -> None:
        with self._state:
            self._generation += 1
            previous = self._cancel
            self._cancel = Event()
        previous.set()
        _stop_backend(self._backend)

    def close(self) -> None:
        self.stop()
        closer = getattr(self._backend, "close", None)
        if callable(closer):
            closer()


def _stop_backend(backend: Any) -> None:
    stopper = getattr(backend, "stop", None)
    if callable(stopper):
        stopper()


def _invoke_play(backend: Any, samples: Sequence[float], sample_rate: int, cancel: Event) -> None:
    play = backend.play
    try:
        parameters = inspect.signature(play).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "cancel" in parameters:
        play(samples, sample_rate, cancel=cancel)
        return
    play(samples, sample_rate)
