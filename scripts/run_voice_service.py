"""Headless OpenRobotSoul voice service runner for the MiMax2 deployment.

Start the bounded local voice service (passive listening -> wake word -> ASR ->
runtime task -> Chinese TTS reply) under systemd and publish a read-only status
snapshot that a watchdog can inspect.

The runner never kills other audio owners and never writes outside its runtime
directory, the status files it is told to use, and the robot runtime itself.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

from robotsoul.agent import DemoAgent
from robotsoul.audit import JsonlToolCallAuditSink
from robotsoul.deepseek_agent import DeepSeekAgent
from robotsoul.profiles import load_selected_profile
from robotsoul.runtime import RobotRuntime
from robotsoul.voice.factory import create_voice_service


def _build_agent() -> tuple[object, str]:
    """Use DeepSeek Flash when a key is configured, otherwise the demo rules."""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return DemoAgent(), "demo"
    agent = DeepSeekAgent(key)
    return agent, f"deepseek:{agent.model}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dir_mb(path: str) -> float | None:
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    return round(stats.f_bavail * stats.f_frsize / (1024 * 1024), 1)


def _rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return 0.0


def _fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return 0


def _pulse_default_source() -> str | None:
    # Force the C locale: the phone's system locale is zh_CN, and a localized
    # pactl prints "默认源：" instead of "Default Source:", which broke the
    # very first startup of the systemd service.
    env = dict(os.environ)
    env.update({"LC_ALL": "C", "LANG": "C"})
    try:
        result = subprocess.run(["pactl", "info"], capture_output=True, text=True,
                                timeout=5, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.lower().startswith("default source:"):
            return line.split(":", 1)[1].strip()
    return None


def _ensure_pulse() -> str:
    """Return a short backend description, starting a user PulseAudio if needed."""
    if shutil.which("pactl") and _pulse_default_source():
        return "pulse"
    if shutil.which("pulseaudio"):
        subprocess.run(["pulseaudio", "--start", "--log-target=syslog"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        for _ in range(15):
            time.sleep(1)
            if _pulse_default_source():
                return "pulse"
    raise RuntimeError("PulseAudio is unavailable; the voice service cannot capture audio")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_voice_service")
    parser.add_argument("--robot-config", default=os.environ.get("ROBOTSOUL_ROBOT_CONFIG"))
    parser.add_argument("--profile")
    parser.add_argument("--resource-root", default=os.environ.get("ROBOTSOUL_VOICE_RESOURCE_ROOT"))
    parser.add_argument("--runtime-dir", default=os.environ.get("ROBOTSOUL_VOICE_RUNTIME_DIR", "/run/mimax2-voice"))
    parser.add_argument("--period", type=float, default=10.0, help="status/heartbeat period in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.period <= 0:
        print("period must be positive", file=sys.stderr)
        return 2
    runtime_dir = Path(args.runtime_dir)
    status_path = runtime_dir / "status.json"
    heartbeat_path = runtime_dir / "heartbeat"
    pid_path = runtime_dir / "pid"
    stop = threading.Event()
    _write_atomic(pid_path, f"{os.getpid()}\n")
    _write_atomic(status_path, json.dumps({
        "timestamp": _now_iso(), "pid": os.getpid(), "phase": "starting",
    }, ensure_ascii=False) + "\n")

    def handle_signal(signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        backend = _ensure_pulse()
    except RuntimeError as exc:
        _write_atomic(status_path, json.dumps({
            "timestamp": _now_iso(), "pid": os.getpid(), "phase": "startup-failed",
            "error": str(exc),
        }, ensure_ascii=False) + "\n")
        print(f"voice service failed: {exc}", file=sys.stderr)
        return 1

    profile = load_selected_profile(args.profile, args.robot_config)
    agent, agent_label = _build_agent()
    runtime = RobotRuntime.from_profile(
        profile, agent=agent, audit_sink=JsonlToolCallAuditSink())
    print(f"agent: {agent_label}", flush=True)
    service = None
    started_at = time.monotonic()
    exit_code = 0
    events: dict[str, object] = {"wake": None, "transcript": None, "last_result": None}

    def on_wake(text: object) -> None:
        events["wake"] = {"text": str(text), "at": _now_iso()}
        print(f"wake: {text}", flush=True)

    def on_transcript(text: object) -> None:
        events["transcript"] = {"text": str(text), "at": _now_iso()}
        print(f"transcript: {text}", flush=True)

    def run_text_logged(text: str):
        result = runtime.run_text(text)
        task = getattr(result, "task", None)
        evaluation = getattr(result, "evaluation", None)
        events["last_result"] = {
            "input": str(text),
            "status": getattr(getattr(task, "status", None), "value", None),
            "error": getattr(task, "error", None),
            "success": getattr(evaluation, "success", None),
            "conclusion": getattr(evaluation, "conclusion", None),
            "at": _now_iso(),
        }
        print("runtime result: " + json.dumps(events["last_result"], ensure_ascii=False), flush=True)
        return result

    try:
        service, _config = create_voice_service(
            runtime,
            resource_root=args.resource_root,
            wake_callback=on_wake,
            transcript_callback=on_transcript,
            run_text=run_text_logged,
        )
        state = service.start()
        print(f"voice service started: state={state} backend={backend} pid={os.getpid()}", flush=True)
        last_wake = None
        last_transcript = None
        while not stop.wait(args.period):
            health = service.health()
            now = time.monotonic()
            if health.last_wake_at is not None:
                last_wake = round(now - health.last_wake_at, 1)
            if health.last_transcript_at is not None:
                last_transcript = round(now - health.last_transcript_at, 1)
            status = {
                "timestamp": _now_iso(),
                "pid": os.getpid(),
                "uptime_seconds": round(now - started_at, 1),
                "phase": "running",
                "agent": agent_label,
                "audio_backend": backend,
                "pulse_default_source": _pulse_default_source(),
                "voice_state": health.state.value,
                "available": health.available,
                "error": health.error,
                "model_loaded": service.engine_loaded,
                "queue_depth": service.queue_depth,
                "dropped_frames": health.dropped_frames,
                "last_wake_seconds_ago": last_wake,
                "last_transcript_seconds_ago": last_transcript,
                "last_wake": events["wake"],
                "last_transcript": events["transcript"],
                "last_result": events["last_result"],
                "rss_mb": _rss_mb(),
                "open_fds": _fd_count(),
                "root_free_mb": _dir_mb("/"),
                "data_free_mb": _dir_mb("/home/user/mimax2-data"),
            }
            _write_atomic(status_path, json.dumps(status, ensure_ascii=False) + "\n")
            _write_atomic(heartbeat_path, f"timestamp={status['timestamp']} pid={os.getpid()} state={status['voice_state']}\n")
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        print(f"voice service crashed: {type(exc).__name__}: {exc}", file=sys.stderr)
        _write_atomic(status_path, json.dumps({
            "timestamp": _now_iso(), "pid": os.getpid(), "phase": "crashed",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False) + "\n")
    finally:
        if service is not None:
            service.stop()
        runtime.close()
        _write_atomic(heartbeat_path, f"timestamp={_now_iso()} pid={os.getpid()} state=stopped\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
