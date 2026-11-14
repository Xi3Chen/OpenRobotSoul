"""Process-local audio output registry.

`system` is the host speaker. `unitree-go2` appears only while a connected
Go2 adapter has registered its speaker transport.
"""

from __future__ import annotations

from threading import Lock
from typing import Any


SYSTEM_OUTPUT = "system"
GO2_OUTPUT = "unitree-go2"

_lock = Lock()
_outputs: dict[str, Any] = {}


def register_output(name: str, output: Any) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("audio output name must be non-empty")
    with _lock:
        _outputs[name] = output


def unregister_output(name: str, output: Any | None = None) -> None:
    with _lock:
        current = _outputs.get(name)
        if output is None or current is output:
            _outputs.pop(name, None)


def get_output(name: str) -> Any | None:
    with _lock:
        return _outputs.get(name)
