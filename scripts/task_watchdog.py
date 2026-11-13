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
    parser.add_argument("--pid", type=int)
    parser.add_argument("--pid-file", type=Path, help="Read the watched PID from this file")
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--heartbeat", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--stale-after", type=float, default=180.0)
    args = parser.parse_args()
    if args.interval <= 0 or args.stale_after <= 0:
        parser.error("interval and stale-after must be positive")
    pid = None
    for attempt in range(13):
        try:
            pid = _resolve_pid(args.pid, args.pid_file)
            break
        except ValueError as exc:
            if attempt == 12:
                parser.error(str(exc))
            # The watched service may still be restarting and have removed its
            # runtime directory; wait for the PID file instead of crash-looping.
            time.sleep(5)
    args.heartbeat.parent.mkdir(parents=True, exist_ok=True)
    while True:
        now = time.time()
        alive = _pid_alive(pid)
        progress_age = None
        if args.progress.exists():
            progress_age = max(0.0, now - args.progress.stat().st_mtime)
        state = "running" if alive and (progress_age is None or progress_age <= args.stale_after) else "stale"
        if not alive:
            state = "stopped"
        line = (
            f"timestamp={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))} "
            f"pid={pid} state={state} "
            f"progress_age_seconds={'' if progress_age is None else round(progress_age, 1)}\n"
        )
        args.heartbeat.write_text(line, encoding="utf-8")
        if state == "stopped":
            return
        time.sleep(args.interval)


def _resolve_pid(pid: int | None, pid_file: Path | None) -> int:
    """Resolve the watched PID, preferring --pid-file so service restarts are picked up."""
    if pid_file is not None:
        try:
            text = Path(pid_file).read_text(encoding="utf-8").strip()
            return int(text.split()[0])
        except (OSError, ValueError, IndexError) as exc:
            raise ValueError(f"could not read a PID from {pid_file}: {exc}") from exc
    if pid is None:
        raise ValueError("either --pid or --pid-file is required")
    return pid


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # On Windows os.kill(pid, 0) terminates the target process instead of
        # probing it (POSIX semantics only). Use OpenProcess as a pure probe.
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


if __name__ == "__main__":
    main()
