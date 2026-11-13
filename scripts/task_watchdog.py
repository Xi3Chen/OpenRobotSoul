"""Small process-progress watchdog for long-running deployments.

It writes a heartbeat and reports stale progress; it never kills the target.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--heartbeat", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--stale-after", type=float, default=180.0)
    args = parser.parse_args()
    if args.interval <= 0 or args.stale_after <= 0:
        parser.error("interval and stale-after must be positive")
    args.heartbeat.parent.mkdir(parents=True, exist_ok=True)
    while True:
        now = time.time()
        alive = _pid_alive(args.pid)
        progress_age = None
        if args.progress.exists():
            progress_age = max(0.0, now - args.progress.stat().st_mtime)
        state = "running" if alive and (progress_age is None or progress_age <= args.stale_after) else "stale"
        if not alive:
            state = "stopped"
        line = (
            f"timestamp={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))} "
            f"pid={args.pid} state={state} "
            f"progress_age_seconds={'' if progress_age is None else round(progress_age, 1)}\n"
        )
        args.heartbeat.write_text(line, encoding="utf-8")
        if state == "stopped":
            return
        time.sleep(args.interval)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


if __name__ == "__main__":
    main()
