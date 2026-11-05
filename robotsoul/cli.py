from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Any

from robotsoul.profiles import load_profile
from robotsoul.picoclaw import PicoClawCodingAgent, PicoClawError
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
    parser.add_argument("--profile", default="simulator-basic", help="Bundled robot profile ID")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("profiles", help="List bundled simulator profiles")
    run_parser = subparsers.add_parser("run", help="Run a text task against the simulator")
    run_parser.add_argument("text", nargs="+", help="Task text, for example: 抬头")
    subparsers.add_parser("demo", help="Run the built-in Chinese head-look demo")
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
        print("simulator-basic\nsimulator-compact")
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
    profile = load_profile(args.profile)
    runtime = RobotRuntime.from_profile(profile)
    text = "抬头" if args.command == "demo" else " ".join(args.text)
    result = runtime.run_text(text)
    print(json.dumps(_json_value(result), ensure_ascii=False, indent=2))
    if result.task.status.value != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
