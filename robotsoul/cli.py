from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from robotsoul.audit import JsonlToolCallAuditSink
from robotsoul.agent import DemoAgent
from robotsoul.dashboard import run_dashboard
from robotsoul.profiles import list_profiles, load_selected_profile
from robotsoul.picoclaw import PicoClawCodingAgent, PicoClawError
from robotsoul.language_session import (
    CodingDispatcher,
    LanguageAgent,
    LanguageGatewayError,
    LanguageSafetyReviewer,
    LanguageSession,
    report_startup_failure,
)
from robotsoul.adapters.registry import create_adapter
from robotsoul.runlog import log as runtime_log
from robotsoul.runtime import RobotRuntime


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    return value


def _load_local_env() -> None:
    """Fill unset variables from the machine env file used by start-dashboard.sh."""
    path = Path.home() / ".openrobotsoul-go2.env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'")


def main() -> None:
    _load_local_env()
    parser = argparse.ArgumentParser(prog="robotsoul", description="OpenRobotSoul Python MVP")
    parser.add_argument("--profile", help="Override the profile selected in config/robot.json")
    parser.add_argument("--robot-module", help="Override the robot_module selected in config/robot.json")
    parser.add_argument("--robot-config", help="Path to deployment robot selection JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("profiles", help="List profiles registered by robot modules")
    subparsers.add_parser("status", help="Check the selected robot connection and read-only state")
    run_parser = subparsers.add_parser("run", help="Run a text task using the selected robot module")
    run_parser.add_argument("--llm-config", help="Local cloud model settings file")
    run_parser.add_argument("--demo-agent", action="store_true", help="Skip the language process. Free text is not matched locally")
    run_parser.add_argument("text", nargs="+", help="Task text. Ordinary sentences go to the language process; /skill runs locally")
    subparsers.add_parser("demo", help="Run the built-in /head.look demo without a language process")
    dashboard_parser = subparsers.add_parser("dashboard", help="Run the local operator Dashboard")
    dashboard_parser.add_argument("--operator-user-env", default="ROBOTSOUL_DASHBOARD_USER", help="Environment variable for the initial operator username")
    dashboard_parser.add_argument("--operator-password-env", default="ROBOTSOUL_DASHBOARD_PASSWORD", help="Environment variable for the initial operator password; prompted securely when unset")
    dashboard_parser.add_argument("--operators", help="Operator account file; usernames are plaintext and passwords are PBKDF2 digests")
    dashboard_parser.add_argument("--llm-config", help="Local cloud model settings file")
    dashboard_parser.add_argument("--voice-config", help="Voice configuration file edited by Dashboard audio settings")
    dashboard_parser.add_argument("--host", default="127.0.0.1", help="Bind address; LAN addresses require TLS")
    dashboard_parser.add_argument("--port", type=int, default=8765, help="Dashboard port")
    dashboard_parser.add_argument("--tls-cert", help="PEM certificate for LAN HTTPS")
    dashboard_parser.add_argument("--tls-key", help="PEM private key for LAN HTTPS")
    dashboard_parser.add_argument("--no-voice", action="store_true", help="Do not start local passive voice listening")
    dashboard_parser.add_argument("--demo-agent", action="store_true", help="Skip the language process. Free text is not matched locally")
    resource_parser = subparsers.add_parser("voice-resources", help="Download, install, verify, or package pinned voice resources")
    resource_parser.add_argument("action", choices=("download", "install", "verify", "pack", "import"))
    resource_parser.add_argument("--root")
    resource_parser.add_argument("--manifest")
    resource_parser.add_argument("--id", dest="ids", action="append")
    resource_parser.add_argument("--output")
    resource_parser.add_argument("--bundle")
    coding_parser = subparsers.add_parser("coding-agent", help="Run a coding task with PicoClaw")
    coding_subparsers = coding_parser.add_subparsers(dest="coding_command", required=True)
    coding_check = coding_subparsers.add_parser("check", help="Check PicoClaw binary and configuration")
    coding_check.add_argument("--binary", help="Path to the PicoClaw executable")
    coding_check.add_argument("--json", action="store_true", help="Print a structured setup report")
    coding_run = coding_subparsers.add_parser("run", help="Run one coding task in a workspace")
    coding_run.add_argument("--workspace", required=True, help="Isolated coding worktree or workspace")
    coding_run.add_argument("--binary", help="Path to the PicoClaw executable")
    coding_run.add_argument("--model", help="PicoClaw model name override")
    coding_run.add_argument("--timeout", type=int, default=300, help="Task timeout in seconds")
    coding_run.add_argument(
        "--allow-shell",
        action="store_true",
        help="Enable PicoClaw shell commands; use only in a separately sandboxed environment",
    )
    coding_run.add_argument("--json", action="store_true", help="Print a structured task result")
    coding_run.add_argument("prompt", nargs="+", help="Coding task instructions")
    args = parser.parse_args()

    if args.command == "voice-resources":
        from robotsoul.resources import main as resources_main
        resource_args = [args.action]
        for option, value in (("--root", args.root), ("--manifest", args.manifest), ("--output", args.output), ("--bundle", args.bundle)):
            if value:
                resource_args.extend((option, value))
        for resource_id in args.ids or []:
            resource_args.extend(("--id", resource_id))
        resources_main(resource_args)
        return

    if args.command == "profiles":
        print("\n".join(list_profiles()))
        return
    if args.command == "coding-agent":
        if args.coding_command == "check":
            report = PicoClawCodingAgent(binary=args.binary).check_setup()
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(f"PicoClaw: {report['version']}")
                print(f"Config found: {report['config_found']}")
                print(f"Models configured: {report['model_count']}")
                print(f"Hooks disabled: {report['hooks_disabled']}")
                print(f"Evolution disabled: {report['evolution_disabled']}")
                print("Credentials validated: no (check does not contact a model provider)")
                for issue in report["issues"]:
                    print(f"- {issue}", file=sys.stderr)
            if not report["ready_to_launch"]:
                raise SystemExit(1)
            return
        agent = PicoClawCodingAgent(
            binary=args.binary,
            timeout_seconds=args.timeout,
            model=args.model,
            allow_shell=args.allow_shell,
        )
        try:
            result = agent.run_task(" ".join(args.prompt), args.workspace)
        except (PicoClawError, ValueError) as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(_json_value(result), ensure_ascii=False, indent=2))
        else:
            if result.output:
                print(result.output, end="" if result.output.endswith("\n") else "\n")
            if result.stderr:
                print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
            if result.cancelled:
                print(f"PicoClaw task {result.task_id} cancelled", file=sys.stderr)
            elif result.timed_out:
                print(f"PicoClaw task {result.task_id} timed out", file=sys.stderr)
            elif result.returncode != 0:
                print(
                    f"PicoClaw task {result.task_id} failed with exit code {result.returncode}",
                    file=sys.stderr,
                )
        if not result.succeeded:
            raise SystemExit(1)
        return
    if args.command == "status":
        profile = load_selected_profile(args.profile, args.robot_config, args.robot_module)
        adapter = None
        try:
            adapter = create_adapter(profile)
            adapter.connect()
            result = {
                "profile_id": profile.profile_id,
                "robot_id": profile.robot_id,
                "manufacturer": profile.manufacturer,
                "model": profile.model,
                "connected": adapter.health(),
                "capabilities": sorted(adapter.discover_capabilities()),
                "observations": _json_value(adapter.drain_observations()),
            }
            if not result["connected"]:
                result["error"] = "Robot adapter is not healthy"
        except Exception as exc:
            result = {
                "profile_id": profile.profile_id,
                "robot_id": profile.robot_id,
                "manufacturer": profile.manufacturer,
                "model": profile.model,
                "connected": False,
                "capabilities": [],
                "observations": [],
                "error": str(exc),
            }
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception as exc:
                    result["connected"] = False
                    result["error"] = f"Could not close robot adapter: {exc}"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["connected"]:
            raise SystemExit(1)
        return
    language_session = None
    coding_dispatcher = CodingDispatcher()
    use_language = args.command in {"dashboard", "run"} and not getattr(args, "demo_agent", False)
    agent = DemoAgent() if getattr(args, "demo_agent", False) else None
    reviewer = None
    if use_language:
        from robotsoul.settings import default_llm_path

        llm_path = Path(args.llm_config) if getattr(args, "llm_config", None) else default_llm_path()
        from robotsoul.robot_config import default_robot_config_path

        robot_config_path = (
            Path(args.robot_config) if getattr(args, "robot_config", None) else default_robot_config_path()
        )
        try:
            language_session = LanguageSession.start_resident(llm_path, robot_config_path)
        except LanguageGatewayError as exc:
            report_startup_failure(coding_dispatcher, str(exc))
            runtime_log("Runtime", "Error", f"语言进程启动失败：{exc}")
            parser.error(str(exc))
        agent = LanguageAgent(language_session, coding_dispatcher, robot_config_path=robot_config_path)
        reviewer = LanguageSafetyReviewer(language_session, robot_config_path=robot_config_path)
    try:
        profile = load_selected_profile(args.profile, args.robot_config, args.robot_module)
        runtime = RobotRuntime.from_profile(
            profile,
            agent=agent,
            audit_sink=JsonlToolCallAuditSink(),
            safety_reviewer=reviewer,
        )
    except (RuntimeError, ValueError) as exc:
        if use_language:
            report_startup_failure(coding_dispatcher, str(exc))
        if language_session is not None:
            language_session.close()
        runtime_log("Runtime", "Error", f"运行时启动失败：{exc}")
        parser.error(str(exc))
    _log_runtime_connection(runtime)

    def _close_started_processes() -> None:
        if language_session is not None:
            language_session.close()
        runtime.close()

    if args.command == "dashboard":
        from robotsoul.settings import OperatorDirectory, default_llm_path, default_operators_path, default_voice_config_path

        operators_path = Path(args.operators) if args.operators else default_operators_path()
        llm_path = Path(args.llm_config) if args.llm_config else default_llm_path()
        voice_config_path = Path(args.voice_config) if args.voice_config else default_voice_config_path()
        try:
            directory = OperatorDirectory(operators_path)
            if not directory.usernames():
                username = os.environ.get(args.operator_user_env)
                password = os.environ.get(args.operator_password_env)
                if not username:
                    if not sys.stdin.isatty():
                        _close_started_processes()
                        parser.error(f"Set {args.operator_user_env} or create {operators_path}")
                    username = input("Dashboard operator username: ")
                if not password:
                    if not sys.stdin.isatty():
                        _close_started_processes()
                        parser.error(f"Set {args.operator_password_env} or create {operators_path}")
                    password = getpass.getpass("Dashboard operator password: ")
                directory.upsert(username, password)
        except (OSError, ValueError) as exc:
            runtime_log("Dashboard", "Error", f"操作员目录不可用：{exc}")
            _close_started_processes()
            parser.error(str(exc))
        runtime_log("Dashboard", "Info", f"操作员目录已就绪 accounts={len(directory.usernames())}")
        voice_service = None
        voice_media = None
        try:
            if not args.no_voice:
                from robotsoul.voice.factory import create_voice_service
                from robotsoul.voice.media import VoiceMediaBridge

                voice_service, _voice_config = create_voice_service(
                    runtime, config_path=voice_config_path
                )
                voice_service.start()
                voice_media = VoiceMediaBridge(voice_service)
            else:
                runtime_log("Voice", "Warning", "语音服务未启用")
            run_dashboard(
                runtime,
                operators_path=operators_path,
                llm_path=llm_path,
                voice_config_path=voice_config_path,
                host=args.host,
                port=args.port,
                tls_cert=args.tls_cert,
                tls_key=args.tls_key,
                voice_service=voice_service,
                call_media=voice_media,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            if use_language:
                report_startup_failure(coding_dispatcher, str(exc))
            runtime_log("Dashboard", "Error", f"启动失败：{exc}")
            parser.error(str(exc))
        finally:
            if voice_media is not None:
                voice_media.shutdown()
            if voice_service is not None:
                voice_service.stop()
            runtime.close()
            if language_session is not None:
                language_session.close()
        return
    try:
        text = "/head.look yaw_deg=0 pitch_deg=10" if args.command == "demo" else " ".join(args.text)
        result = runtime.run_text(text)
        print(json.dumps(_json_value(result), ensure_ascii=False, indent=2))
        if result.task.status.value != "completed":
            raise SystemExit(1)
    finally:
        runtime.close()
        if language_session is not None:
            language_session.close()


def _log_runtime_connection(runtime: RobotRuntime) -> None:
    profile = runtime.profile
    agent_name = type(runtime.agent).__name__
    runtime_log(
        "Runtime",
        "Info",
        "运行时已创建 "
        f"profile={profile.profile_id} robot={profile.robot_id} "
        f"module={profile.robot_module} agent={agent_name} "
        f"capabilities={len(runtime.capabilities)}",
    )
    try:
        connected = bool(runtime.adapter.health())
    except Exception as exc:
        runtime_log("Runtime", "Error", f"连接检查失败 profile={profile.profile_id} error={exc}")
        return
    if connected:
        runtime_log("Runtime", "Info", f"机器人连接完成 profile={profile.profile_id}")
    else:
        runtime_log("Runtime", "Warning", f"机器人未连接 profile={profile.profile_id}")


if __name__ == "__main__":
    main()
