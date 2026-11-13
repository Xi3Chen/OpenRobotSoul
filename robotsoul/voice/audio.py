"""Lazy sounddevice adapters. Importing this module never probes or opens hardware."""

from __future__ import annotations

import os
from array import array
import shutil
import subprocess
from threading import Event, Thread
from typing import Any, Callable, Sequence


class VoiceAudioError(RuntimeError):
    pass


def _sounddevice() -> Any:
    try:
        import sounddevice
    except ImportError as exc:
        raise VoiceAudioError("sounddevice is required for local audio I/O") from exc
    return sounddevice


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

    def play(self, samples: Sequence[float], sample_rate: int) -> None:
        if not samples:
            return
        sd = _sounddevice()
        device = _resolve_device(sd, self.device, "output")
        if self.device == "usb-speaker" and device is None:
            raise VoiceAudioError("No USB speaker is available")
        try:
            sd.play(samples, samplerate=sample_rate, device=device, blocking=True)
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
        self.device = device or os.environ.get("ROBOTSOUL_VOICE_PULSE_SOURCE")
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
        command = ["parec", "--raw", "--format=s16le", f"--rate={self.sample_rate}", "--channels=1"]
        if self.device:
            command.append(f"--device={self.device}")
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

    def play(self, samples: Sequence[float], sample_rate: int) -> None:
        if not samples:
            return
        if shutil.which("pacat") is None:
            raise VoiceAudioError("pacat is required for PulseAudio playback")
        values = array("h", (max(-32768, min(32767, int(float(value) * 32767.0))) for value in samples))
        command = ["pacat", "--raw", "--format=s16le", f"--rate={sample_rate}", "--channels=1"]
        if self.device:
            command.append(f"--device={self.device}")
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self._process = process
            process.communicate(values.tobytes(), timeout=max(2.0, len(values) / sample_rate + 2.0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            if self._process is not None:
                self._process.kill()
            raise VoiceAudioError(f"Unable to play through PulseAudio: {exc}") from exc
        finally:
            self._process = None

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            process.kill()

    def close(self) -> None:
        self.stop()
