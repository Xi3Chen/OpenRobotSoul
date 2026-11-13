from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


MODEL_IDS = {
    "kws": "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20",
    "asr": "sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30",
    "vad": "silero-vad-int8",
    "tts": "vits-piper-zh_CN-xiao_ya-medium-int8",
}
DEVICE_IDS = frozenset({"default", "usb-microphone", "usb-speaker"})


@dataclass(frozen=True)
class VoiceConfig:
    robot_name: str
    wake_phrase: str
    language: str
    models: dict[str, str]
    input_device: str
    output_device: str
    follow_up_seconds: int
    session_max_seconds: int
    call_reconnect_grace_seconds: int


def load_voice_config(path: str | Path) -> VoiceConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "schema_version", "robot_name", "wake_phrase", "language", "models", "input_device",
        "output_device", "follow_up_seconds", "session_max_seconds",
        "call_reconnect_grace_seconds",
    }
    if not isinstance(raw, dict) or raw.keys() != required:
        raise ValueError("voice config has missing or unsupported fields")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("unsupported voice config schema version")
    raw = {key: value for key, value in raw.items() if key != "schema_version"}
    for field in ("robot_name", "wake_phrase", "language"):
        if not isinstance(raw[field], str) or not raw[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    models = raw["models"]
    if not isinstance(models, dict) or set(models) != {"kws", "asr", "vad", "tts"}:
        raise ValueError("models must define kws, asr, vad and tts")
    if models != MODEL_IDS:
        raise ValueError("models must match the statically registered KWS, ASR, VAD and TTS IDs")
    for field in ("input_device", "output_device"):
        if raw[field] not in DEVICE_IDS:
            raise ValueError(f"{field} must reference a statically registered device")
    durations = ("follow_up_seconds", "session_max_seconds", "call_reconnect_grace_seconds")
    if any(type(raw[field]) is not int or raw[field] <= 0 for field in durations):
        raise ValueError("voice timing values must be positive integers")
    return VoiceConfig(**raw)
