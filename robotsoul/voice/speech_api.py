"""OpenAI-compatible speech HTTP API.

Cloud hosts and a locally deployed server use the same two routes:
``POST {base_url}/audio/transcriptions`` and ``POST {base_url}/audio/speech``.
The base URL selects which one. Keys stay in environment variables named by
the config, and are not written into the voice file.
"""

from __future__ import annotations

import array
import io
import json
import os
import wave
from dataclasses import dataclass
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SpeechApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpeechEndpoint:
    base_url: str
    model: str
    timeout_seconds: int
    voice: str = ""
    api_key_env: str = ""


@dataclass(frozen=True)
class SpeechConfig:
    asr: SpeechEndpoint
    tts: SpeechEndpoint


class SpeechClient:
    def __init__(self, config: SpeechConfig):
        self.config = config

    def transcribe(self, samples: Sequence[float], sample_rate: int, *, language: str = "") -> str:
        base_url, model, _voice, api_key = _resolved(
            self.config.asr,
            "ROBOTSOUL_ASR_BASE_URL",
            "ROBOTSOUL_ASR_MODEL",
        )
        if not base_url or not model:
            raise SpeechApiError("ASR API base_url and model are not configured")
        payload, content_type = _multipart(
            {"model": model, "language": _language_code(language)},
            "audio.wav",
            _wav_bytes(samples, sample_rate),
        )
        body = _request(
            f"{base_url.rstrip('/')}/audio/transcriptions",
            payload,
            content_type,
            api_key,
            self.config.asr.timeout_seconds,
        )
        return _transcript(body)

    def synthesize(self, text: str) -> tuple[tuple[float, ...], int]:
        base_url, model, voice, api_key = _resolved(
            self.config.tts,
            "ROBOTSOUL_TTS_BASE_URL",
            "ROBOTSOUL_TTS_MODEL",
            "ROBOTSOUL_TTS_VOICE",
        )
        if not base_url or not model:
            raise SpeechApiError("TTS API base_url and model are not configured")
        if not voice:
            raise SpeechApiError("TTS API voice is not configured")
        payload = json.dumps(
            {"model": model, "input": text, "voice": voice, "response_format": "wav"},
            ensure_ascii=False,
        ).encode("utf-8")
        body = _request(
            f"{base_url.rstrip('/')}/audio/speech",
            payload,
            "application/json",
            api_key,
            self.config.tts.timeout_seconds,
        )
        return _wav_samples(body)


def _resolved(endpoint: SpeechEndpoint, url_env: str, model_env: str, voice_env: str = "") -> tuple[str, str, str, str]:
    base_url = os.environ.get(url_env, "").strip() or endpoint.base_url
    model = os.environ.get(model_env, "").strip() or endpoint.model
    voice = os.environ.get(voice_env, "").strip() or endpoint.voice if voice_env else endpoint.voice
    api_key = os.environ.get(endpoint.api_key_env, "").strip() if endpoint.api_key_env else ""
    return base_url, model, voice, api_key


def _language_code(language: str) -> str:
    value = language.strip().lower()
    if value.startswith("zh"):
        return "zh"
    return value.split("-", 1)[0] if value else ""


def _request(url: str, payload: bytes, content_type: str, api_key: str, timeout: int) -> bytes:
    headers = {"Content-Type": content_type}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(url, data=payload, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        detail = exc.read(300).decode("utf-8", errors="replace")
        raise SpeechApiError(f"Speech API {exc.code}: {detail}") from exc
    except URLError as exc:
        raise SpeechApiError(f"Speech API is unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SpeechApiError("Speech API timed out") from exc


def _transcript(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return text
        transcript = value.get("text", "")
        if not isinstance(transcript, str):
            raise SpeechApiError("Speech API transcript is not text")
        return transcript.strip()
    return text


def _multipart(fields: dict[str, str], filename: str, wav_bytes: bytes) -> tuple[bytes, str]:
    boundary = "----openrobotsoul-speech"
    chunks: list[bytes] = []
    for name, value in fields.items():
        if not value:
            continue
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(wav_bytes)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _wav_bytes(samples: Sequence[float], sample_rate: int) -> bytes:
    if sample_rate <= 0:
        raise SpeechApiError("audio sample rate must be positive")
    pcm = array.array("h", (max(-32768, min(32767, int(float(value) * 32767.0))) for value in samples))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _wav_samples(body: bytes) -> tuple[tuple[float, ...], int]:
    try:
        with wave.open(io.BytesIO(body), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError) as exc:
        raise SpeechApiError("TTS API did not return a WAV file") from exc
    if channels != 1 or width != 2 or rate <= 0:
        raise SpeechApiError("TTS API WAV must be mono 16-bit PCM")
    pcm = array.array("h")
    pcm.frombytes(frames[: len(frames) - (len(frames) % 2)])
    return tuple(value / 32768.0 for value in pcm), rate
