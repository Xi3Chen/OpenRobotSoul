from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Any

from robotsoul.audit import JsonlToolCallAuditSink
from robotsoul.dashboard import run_dashboard
from robotsoul.profiles import list_profiles, load_selected_profile
from robotsoul.picoclaw import PicoClawCodingAgent, PicoClawError
from robotsoul.adapters.registry import create_adapter
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="robotsoul", description="OpenRobotSoul Python MVP")
    parser.add_argument("--profile", help="Override the profile selected in config/robot.json")
    parser.add_argument("--robot-module", help="Override the robot_module selected in config/robot.json")
    parser.add_argument("--robot-config", help="Path to deployment robot selection JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("profiles", help="List profiles registered by robot modules")
    subparsers.add_parser("status", help="Check the selected robot connection and read-only state")
    run_parser = subparsers.add_parser("run", help="Run a text task using the selected robot module")
    run_parser.add_argument("text", nargs="+", help="Task text, for example: 抬头")
    subparsers.add_parser("demo", help="Run the built-in Chinese head-look demo")
    dashboard_parser = subparsers.add_parser("dashboard", help="Run the local operator Dashboard")
    dashboard_parser.add_argument("--operator-pin-env", default="ROBOTSOUL_DASHBOARD_PIN", help="Environment variable for the operator PIN; prompted securely when unset")
    dashboard_parser.add_argument("--host", default="127.0.0.1", help="Bind address; LAN addresses require TLS")
    dashboard_parser.add_argument("--port", type=int, default=8765, help="Dashboard port")
    dashboard_parser.add_argument("--tls-cert", help="PEM certificate for LAN HTTPS")
    dashboard_parser.add_argument("--tls-key", help="PEM private key for LAN HTTPS")
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
    try:
        profile = load_selected_profile(args.profile, args.robot_config, args.robot_module)
        runtime = RobotRuntime.from_profile(profile, audit_sink=JsonlToolCallAuditSink())
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    if args.command == "dashboard":
        operator_pin = os.environ.get(args.operator_pin_env) or getpass.getpass("Dashboard operator PIN: ")
        try:
            run_dashboard(
                runtime,
                operator_pin=operator_pin,
                host=args.host,
                port=args.port,
                tls_cert=args.tls_cert,
                tls_key=args.tls_key,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            parser.error(str(exc))
        finally:
            runtime.close()
        return
    try:
        text = "抬头" if args.command == "demo" else " ".join(args.text)
        result = runtime.run_text(text)
        print(json.dumps(_json_value(result), ensure_ascii=False, indent=2))
        if result.task.status.value != "completed":
            raise SystemExit(1)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
