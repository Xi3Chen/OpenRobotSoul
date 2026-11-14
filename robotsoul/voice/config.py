from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from robotsoul.voice.speech_api import SpeechConfig, SpeechEndpoint


MODEL_IDS = {
    "kws": "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20",
    "vad": "silero-vad-int8",
}
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
INPUT_DEVICE_IDS = frozenset({"default", "usb-microphone", "usb-speaker"})
OUTPUT_DEVICE_IDS = INPUT_DEVICE_IDS | {"unitree-go2"}
DEVICE_IDS = INPUT_DEVICE_IDS


@dataclass(frozen=True)
class VoiceConfig:
    wake_phrase: str
    language: str
    models: dict[str, str]
    input_device: str
    output_device: str
    follow_up_seconds: int
    session_max_seconds: int
    call_reconnect_grace_seconds: int
    speech: SpeechConfig


def load_voice_config(path: str | Path) -> VoiceConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "schema_version", "wake_phrase", "language", "models", "speech", "input_device",
        "output_device", "follow_up_seconds", "session_max_seconds",
        "call_reconnect_grace_seconds",
    }
    if not isinstance(raw, dict) or raw.keys() != required:
        raise ValueError("voice config has missing or unsupported fields")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("unsupported voice config schema version")
    raw = {key: value for key, value in raw.items() if key != "schema_version"}
    if not isinstance(raw["wake_phrase"], str):
        raise ValueError("wake_phrase must be a string")
    if not isinstance(raw["language"], str) or not raw["language"].strip():
        raise ValueError("language must be a non-empty string")
    models = raw["models"]
    if not isinstance(models, dict) or set(models) != {"kws", "vad"}:
        raise ValueError("models must define kws and vad")
    if models != MODEL_IDS:
        raise ValueError("models must match the statically registered KWS and VAD IDs")
    speech = _speech_config(raw["speech"])
    if raw["input_device"] not in INPUT_DEVICE_IDS:
        raise ValueError("input_device must reference a statically registered device")
    if raw["output_device"] not in OUTPUT_DEVICE_IDS:
        raise ValueError("output_device must reference a statically registered device")
    durations = ("follow_up_seconds", "session_max_seconds", "call_reconnect_grace_seconds")
    if any(type(raw[field]) is not int or raw[field] <= 0 for field in durations):
        raise ValueError("voice timing values must be positive integers")
    raw["speech"] = speech
    return VoiceConfig(**raw)


def speech_document(speech: SpeechConfig) -> dict:
    return {
        "asr": _endpoint_document(speech.asr, include_voice=False),
        "tts": _endpoint_document(speech.tts, include_voice=True),
    }


def _speech_config(value: object) -> SpeechConfig:
    if not isinstance(value, dict) or set(value) != {"asr", "tts"}:
        raise ValueError("speech must define asr and tts endpoints")
    return SpeechConfig(
        asr=_endpoint(value["asr"], "asr", include_voice=False),
        tts=_endpoint(value["tts"], "tts", include_voice=True),
    )


def _endpoint(value: object, name: str, *, include_voice: bool) -> SpeechEndpoint:
    required = {"base_url", "model", "timeout_seconds", "api_key_env"}
    if include_voice:
        required = required | {"voice"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError(f"speech.{name} has missing or unsupported fields")
    voice = _short_text(value["voice"], f"speech.{name}.voice") if include_voice else ""
    api_key_env = value["api_key_env"]
    if not isinstance(api_key_env, str) or not _ENV_NAME.fullmatch(api_key_env):
        raise ValueError(f"speech.{name}.api_key_env must be an uppercase environment variable name")
    return SpeechEndpoint(
        base_url=_http_url(value["base_url"], f"speech.{name}.base_url"),
        model=_short_text(value["model"], f"speech.{name}.model"),
        timeout_seconds=_timeout(value["timeout_seconds"], f"speech.{name}.timeout_seconds"),
        voice=voice,
        api_key_env=api_key_env,
    )


def _endpoint_document(endpoint: SpeechEndpoint, *, include_voice: bool) -> dict:
    document = {
        "base_url": endpoint.base_url,
        "model": endpoint.model,
        "timeout_seconds": endpoint.timeout_seconds,
        "api_key_env": endpoint.api_key_env,
    }
    if include_voice:
        document["voice"] = endpoint.voice
    return document


def _http_url(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError(f"{field} must be an http(s) URL or empty")
    text = value.strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{field} must be an http(s) URL without embedded credentials")
    return text


def _short_text(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value.strip()) > 128:
        raise ValueError(f"{field} must contain at most 128 characters")
    return value.strip()


def _timeout(value: object, field: str) -> int:
    if type(value) is not int or isinstance(value, bool) or not 1 <= value <= 180:
        raise ValueError(f"{field} must be an integer from 1 to 180")
    return value
