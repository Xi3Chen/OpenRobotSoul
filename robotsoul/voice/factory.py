from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from robotsoul.resources import default_root
from robotsoul.runlog import log as runtime_log
from robotsoul.runtime import RobotRuntime
from robotsoul.voice.audio import (
    InterruptibleOutput,
    PulseAudioCapture,
    PulseAudioOutput,
    SoundDeviceCapture,
    SoundDeviceOutput,
)
from robotsoul.voice.outputs import GO2_OUTPUT, SYSTEM_OUTPUT, get_output, register_output
from robotsoul.robot_config import default_robot_config_path, load_robot_deployment_config, parse_wake_phrases
from robotsoul.voice.config import VoiceConfig, load_voice_config
from robotsoul.voice.commands import match_closed_command
from robotsoul.voice.engine import SherpaVoiceEngine
from robotsoul.voice.speech_api import SpeechClient
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


def resolve_playback_output(configured_output: Any, system: Any) -> Any:
    """Register the host speaker and return the output selected by voice config."""
    register_output(SYSTEM_OUTPUT, system)
    if configured_output != GO2_OUTPUT:
        return system
    speaker = get_output(GO2_OUTPUT)
    if speaker is None:
        raise RuntimeError(
            "unitree-go2 audio output is not registered. "
            "Set OPENROBOTSOUL_GO2_WEBRTC_IP and connect the Go2 adapter before starting voice."
        )
    return speaker


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
    robot_config = load_robot_deployment_config(default_robot_config_path())
    wake_phrases = parse_wake_phrases(config.wake_phrase, robot_config.robot_name)
    root = Path(resource_root or os.environ.get("ROBOTSOUL_VOICE_RESOURCE_ROOT") or default_root())
    callback = lambda _samples, _rate: None
    configured_input = _device_id(config.input_device, "ROBOTSOUL_VOICE_INPUT_DEVICE")
    configured_output = _device_id(config.output_device, "ROBOTSOUL_VOICE_OUTPUT_DEVICE")
    if os.name == "posix" and shutil.which("parec") and shutil.which("pacat"):
        capture = PulseAudioCapture(callback, device=None if configured_input is None else str(configured_input))
        system_backend = PulseAudioOutput()
        runtime_log("Voice", "Info", f"音频采集后端已选择 PulseAudio source={capture.device or 'default'}")
    else:
        capture = SoundDeviceCapture(callback, device=configured_input)
        device = None if configured_output == GO2_OUTPUT else configured_output
        system_backend = SoundDeviceOutput(device)
        runtime_log("Voice", "Info", f"音频采集后端已选择 sounddevice input={configured_input or 'default'}")
    speaker = resolve_playback_output(configured_output, InterruptibleOutput(system_backend))
    selected = GO2_OUTPUT if configured_output == GO2_OUTPUT else SYSTEM_OUTPUT
    runtime_log("Voice", "Info", f"音频播放出口已选择 {selected}")
    engine = SherpaVoiceEngine(
        root, wake_phrases, speech=SpeechClient(config.speech), language=config.language,
    )
    def run_voice_text(text: str, task_id: str | None = None, intent: Any = None):
        return runtime.run_text(text, task_id=task_id, _intent=intent)

    service = VoiceService(
        capture=capture,
        engine=engine,
        run_text=run_text or run_voice_text,
        robot_name=robot_config.robot_name,
        speaker=speaker,
        follow_up_seconds=config.follow_up_seconds,
        session_max_seconds=config.session_max_seconds,
        call_reconnect_grace_seconds=config.call_reconnect_grace_seconds,
        wake_callback=wake_callback,
        transcript_callback=transcript_callback,
        command_matcher=lambda text: match_closed_command(
            text, runtime.capabilities, wake_phrase=wake_phrases
        ),
        cancel_task=runtime.preempt_task,
    )
    return service, config
