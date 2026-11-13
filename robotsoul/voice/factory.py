from __future__ import annotations

import os
import shutil
from pathlib import Path

from robotsoul.resources import default_root
from robotsoul.runlog import log as runtime_log
from robotsoul.runtime import RobotRuntime
from robotsoul.voice.audio import PulseAudioCapture, PulseAudioOutput, SoundDeviceCapture, SoundDeviceOutput
from robotsoul.voice.config import VoiceConfig, load_voice_config
from robotsoul.voice.engine import SherpaVoiceEngine
from robotsoul.voice.service import VoiceService


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGED_DATA = Path(__file__).resolve().parents[1] / "data"


def _default_voice_config() -> Path:
    project_config = PROJECT_ROOT / "config" / "voice.json"
    packaged_config = PACKAGED_DATA / "voice.json"
    return project_config if project_config.is_file() else packaged_config


def _device_id(configured: str, environment: str) -> str | int | None:
    override = os.environ.get(environment)
    if override:
        return int(override) if override.isdecimal() else override
    return None if configured == "default" else configured


def create_voice_service(
    runtime: RobotRuntime,
    *,
    config_path: str | Path | None = None,
    resource_root: str | Path | None = None,
    wake_callback=None,
    transcript_callback=None,
    run_text=None,
) -> tuple[VoiceService, VoiceConfig]:
    config = load_voice_config(config_path or _default_voice_config())
    root = Path(resource_root or os.environ.get("ROBOTSOUL_VOICE_RESOURCE_ROOT") or default_root())
    callback = lambda _samples, _rate: None
    configured_input = _device_id(config.input_device, "ROBOTSOUL_VOICE_INPUT_DEVICE")
    configured_output = _device_id(config.output_device, "ROBOTSOUL_VOICE_OUTPUT_DEVICE")
    if os.name == "posix" and shutil.which("parec") and shutil.which("pacat"):
        capture = PulseAudioCapture(callback)
        speaker = PulseAudioOutput()
        runtime_log("Voice", "Info", f"音频采集后端已选择 PulseAudio source={capture.device or 'default'}")
    else:
        capture = SoundDeviceCapture(callback, device=configured_input)
        speaker = SoundDeviceOutput(configured_output)
        runtime_log("Voice", "Info", f"音频采集后端已选择 sounddevice input={configured_input or 'default'}")
    engine = SherpaVoiceEngine(root, config.wake_phrase)
    service = VoiceService(
        capture=capture,
        engine=engine,
        run_text=run_text or runtime.run_text,
        robot_name=config.robot_name,
        speaker=speaker,
        follow_up_seconds=config.follow_up_seconds,
        session_max_seconds=config.session_max_seconds,
        call_reconnect_grace_seconds=config.call_reconnect_grace_seconds,
        wake_callback=wake_callback,
        transcript_callback=transcript_callback,
    )
    return service, config
