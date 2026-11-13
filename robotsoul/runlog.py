"""Daily text runtime logs. One line carries time, module and severity."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from threading import Lock


DEFAULT_LOG_DIRECTORY = Path(__file__).resolve().parents[1] / "logs"
_LEVELS = {"Info", "Warning", "Error"}
_MODULE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,31}$")
_PREFIX = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}-\d{1,2}:\d{2}:\d{2}\.\d{3}-[A-Za-z][A-Za-z0-9]{0,31}-(Info|Warning|Error)\] "
)
_SECRET = re.compile(
    r"(?i)\b(password|passwd|secret|token|credential|api[_-]?key|pin)\b\s*[:=]\s*\S+"
)
_MAX_MESSAGE = 1000


class RunLog:
    def __init__(self, directory: str | Path = DEFAULT_LOG_DIRECTORY) -> None:
        self.directory = Path(directory)
        self._write_lock = Lock()

    def write(self, module: str, level: str, message: str) -> str:
        if not isinstance(module, str) or not _MODULE.fullmatch(module):
            raise ValueError("Log module must be a short alphabetic name")
        if level not in _LEVELS:
            raise ValueError("Log level must be Info, Warning or Error")
        text = _message(message)
        now = datetime.now()
        stamp = (
            f"{now.year:04d}-{now.month:02d}-{now.day:02d}-"
            f"{now.hour}:{now.minute:02d}:{now.second:02d}.{now.microsecond // 1000:03d}"
        )
        line = f"[{stamp}-{module}-{level}] {text}"
        payload = (line + "\n").encode("utf-8")
        path = self.directory / f"{now.year:04d}-{now.month:02d}-{now.day:02d}.log"
        with self._write_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("Runtime log write made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return line

    def recent(self, limit: int = 100, level: str | None = None) -> list[str]:
        size = _limit(limit)
        if level is not None and level not in _LEVELS:
            raise ValueError("Log level must be Info, Warning or Error")
        now = datetime.now()
        path = self.directory / f"{now.year:04d}-{now.month:02d}-{now.day:02d}.log"
        with self._write_lock:
            if not path.is_file():
                return []
            raw = path.read_bytes()
        selected: list[str] = []
        for item in raw.splitlines():
            try:
                text = item.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if not text.strip():
                continue
            if level is not None:
                matched = _PREFIX.match(text)
                if matched is None or matched.group(1) != level:
                    continue
            selected.append(text)
        return list(reversed(selected[-size:]))


_log = RunLog()


def configure(directory: str | Path) -> RunLog:
    """Point later log() calls at a directory. Tests use this to avoid the project logs."""
    global _log
    _log = RunLog(directory)
    return _log


def current() -> RunLog:
    return _log


def log(module: str, level: str, message: str) -> None:
    try:
        _log.write(module, level, message)
    except Exception:
        return


def _message(message: str) -> str:
    if not isinstance(message, str):
        message = str(message)
    text = " ".join(message.split())
    text = _SECRET.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return text[:_MAX_MESSAGE]


def _limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("Log limit must be an integer")
    if limit < 1 or limit > 200:
        raise ValueError("Log limit must be between 1 and 200")
    return limit
