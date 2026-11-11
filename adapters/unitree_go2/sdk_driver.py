from __future__ import annotations

import ctypes
import json
import os
import platform
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from robotsoul.models import CapabilitySpec, Command, Feedback, FeedbackStatus, Observation, utc_now
from robotsoul.profiles import RobotProfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_TTL_MS = 1500
STATE_BUFFER_SIZE = 8192
ERROR_BUFFER_SIZE = 1024


class UnitreeGo2SdkDriver:
    """Read-only Go2 bridge backed by the pinned Unitree SDK2 C++ SDK."""

    def __init__(
        self,
        profile: RobotProfile,
        network_interface: str,
        bridge_library: str | Path | None = None,
        library_loader: Callable[[str], Any] = ctypes.CDLL,
    ) -> None:
        if not network_interface or network_interface.strip() != network_interface:
            raise ValueError("A non-empty Go2 network interface name is required")
        self.robot_id = profile.robot_id
        self.network_interface = network_interface
        self.bridge_library = Path(bridge_library).expanduser() if bridge_library else _default_bridge_path()
        self._library_loader = library_loader
        self._library: Any | None = None
        self._handle: Any | None = None

    def connect(self) -> None:
        if platform.system() != "Linux":
            raise RuntimeError("The Unitree SDK2 Go2 bridge currently requires Linux")
        if self._handle is not None:
            return
        if not self.bridge_library.is_file():
            raise RuntimeError(
                f"Go2 SDK bridge library was not found: {self.bridge_library}. "
                "Build it on the Linux robot host or set OPENROBOTSOUL_GO2_BRIDGE_LIBRARY."
            )
        library = self._library_loader(str(self.bridge_library.resolve()))
        self._bind_functions(library)
        error = ctypes.create_string_buffer(ERROR_BUFFER_SIZE)
        handle = library.go2_bridge_create(
            self.network_interface.encode("utf-8"), error, len(error)
        )
        if not handle:
            raise RuntimeError(_error_text(error, "Unable to initialize Unitree Go2 DDS bridge"))
        self._library = library
        self._handle = handle
        if not self.health():
            try:
                self.close()
            except RuntimeError:
                pass
            raise RuntimeError("Go2 state is unavailable or the SDK API versions do not match")

    def health(self) -> bool:
        if self._library is None or self._handle is None:
            return False
        error = ctypes.create_string_buffer(ERROR_BUFFER_SIZE)
        try:
            return bool(self._library.go2_bridge_health(self._handle, error, len(error)))
        except (OSError, ValueError):
            return False

    def identity(self) -> tuple[str, str]:
        return "Unitree", "Go2"

    def discover_capabilities(self) -> dict[str, CapabilitySpec]:
        return {}

    def drain_observations(self) -> tuple[Observation, ...]:
        if not self.health():
            return ()
        state = self._read_state()
        received_at = utc_now()
        observed_at = _parse_timestamp(state["observed_at"])
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise RuntimeError("Go2 bridge state timestamp must include a timezone")
        age_ms = (received_at - observed_at).total_seconds() * 1000
        if age_ms < 0 or age_ms > STATE_TTL_MS:
            return ()
        payload = {
            "api_version_client": state["api_version_client"],
            "api_version_server": state["api_version_server"],
            "state_stamp_sec": state["state_stamp_sec"],
            "state_stamp_nanosec": state["state_stamp_nanosec"],
            "mode": state["mode"],
            "velocity": state["velocity"],
            "yaw_speed": state["yaw_speed"],
            "position": state["position"],
            "error_code": state["error_code"],
        }
        return (
            Observation(
                event_id=str(uuid4()),
                robot_id=self.robot_id,
                device_id="unitree-go2-sport-state",
                event_type="robot.sport_state",
                observed_at=observed_at,
                received_at=received_at,
                payload=payload,
                source="unitree_sdk2",
                ttl_ms=STATE_TTL_MS,
                privacy_class="operational",
            ),
        )

    def execute(self, command: Command) -> tuple[Feedback, ...]:
        return (self._feedback(command, FeedbackStatus.FAILED, "Go2 motion is not enabled", "motion_disabled"),)

    def cancel(self, command: Command) -> Feedback:
        return self._feedback(command, FeedbackStatus.FAILED, "No Go2 motion command is active", "no_active_command")

    def close(self) -> None:
        library, handle = self._library, self._handle
        self._handle = None
        self._library = None
        if library is None or handle is None:
            return
        error = ctypes.create_string_buffer(ERROR_BUFFER_SIZE)
        close_error: BaseException | None = None
        try:
            if not library.go2_bridge_close(handle, error, len(error)):
                close_error = RuntimeError(_error_text(error, "Unable to close Go2 bridge"))
        except BaseException as exc:
            close_error = exc
        finally:
            library.go2_bridge_destroy(handle)
        if close_error is not None:
            raise RuntimeError("Unable to close Go2 native bridge cleanly") from close_error

    def _read_state(self) -> dict[str, Any]:
        error = ctypes.create_string_buffer(ERROR_BUFFER_SIZE)
        output = ctypes.create_string_buffer(STATE_BUFFER_SIZE)
        try:
            result = self._library.go2_bridge_read_state(
                self._handle, output, len(output), error, len(error)
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError("Unable to read Go2 state from the native bridge") from exc
        if not result:
            raise RuntimeError(_error_text(error, "Unable to read Go2 state"))
        try:
            state = json.loads(output.value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Go2 bridge returned invalid state JSON") from exc
        _validate_state(state)
        return state

    @staticmethod
    def _bind_functions(library: Any) -> None:
        char_pointer = ctypes.POINTER(ctypes.c_char)
        library.go2_bridge_create.argtypes = [ctypes.c_char_p, char_pointer, ctypes.c_size_t]
        library.go2_bridge_create.restype = ctypes.c_void_p
        library.go2_bridge_health.argtypes = [ctypes.c_void_p, char_pointer, ctypes.c_size_t]
        library.go2_bridge_health.restype = ctypes.c_int
        library.go2_bridge_read_state.argtypes = [
            ctypes.c_void_p,
            char_pointer,
            ctypes.c_size_t,
            char_pointer,
            ctypes.c_size_t,
        ]
        library.go2_bridge_read_state.restype = ctypes.c_int
        library.go2_bridge_close.argtypes = [ctypes.c_void_p, char_pointer, ctypes.c_size_t]
        library.go2_bridge_close.restype = ctypes.c_int
        library.go2_bridge_destroy.argtypes = [ctypes.c_void_p]
        library.go2_bridge_destroy.restype = None

    @staticmethod
    def _feedback(command: Command, status: FeedbackStatus, message: str, error_code: str) -> Feedback:
        return Feedback(
            feedback_id=str(uuid4()),
            command_id=command.command_id,
            task_id=command.task_id,
            trace_id=command.trace_id,
            status=status,
            timestamp=utc_now(),
            message=message,
            error_code=error_code,
        )


def _default_bridge_path() -> Path:
    architecture = platform.machine().lower()
    if architecture in {"x86_64", "amd64"}:
        target_arch = "x86_64"
    elif architecture in {"aarch64", "arm64"}:
        target_arch = "aarch64"
    else:
        target_arch = architecture
    return PROJECT_ROOT / "build" / f"linux-{target_arch}" / "lib" / "libopenrobotsoul_go2_bridge.so"


def _error_text(buffer: ctypes.Array[ctypes.c_char], fallback: str) -> str:
    text = buffer.value.decode("utf-8", errors="replace").strip()
    return text or fallback


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("Go2 bridge state timestamp is missing")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("Go2 bridge state timestamp is invalid") from exc


def _validate_state(state: Any) -> None:
    expected = {
        "api_version_client",
        "api_version_server",
        "observed_at",
        "state_stamp_sec",
        "state_stamp_nanosec",
        "mode",
        "velocity",
        "yaw_speed",
        "position",
        "error_code",
    }
    if not isinstance(state, dict) or set(state) != expected:
        raise RuntimeError("Go2 bridge returned an unexpected state schema")
    if not all(isinstance(state[key], str) and state[key] for key in ("api_version_client", "api_version_server")):
        raise RuntimeError("Go2 bridge API versions are invalid")
    if state["api_version_client"] != state["api_version_server"]:
        raise RuntimeError("Go2 client and server API versions do not match")
    _parse_timestamp(state["observed_at"])
    if isinstance(state["mode"], bool) or not isinstance(state["mode"], int):
        raise RuntimeError("Go2 bridge mode is invalid")
    for key in ("state_stamp_sec", "state_stamp_nanosec"):
        if isinstance(state[key], bool) or not isinstance(state[key], int):
            raise RuntimeError("Go2 bridge state timestamp is invalid")
    if isinstance(state["error_code"], bool) or not isinstance(state["error_code"], int):
        raise RuntimeError("Go2 bridge error code is invalid")
    for key in ("velocity", "position"):
        vector = state[key]
        if not isinstance(vector, list) or len(vector) != 3:
            raise RuntimeError(f"Go2 bridge {key} vector is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)
            for value in vector
        ):
            raise RuntimeError(f"Go2 bridge {key} vector is invalid")
    if (
        isinstance(state["yaw_speed"], bool)
        or not isinstance(state["yaw_speed"], (int, float))
        or not isfinite(state["yaw_speed"])
    ):
        raise RuntimeError("Go2 bridge yaw speed is invalid")


def create_sdk_driver(profile: RobotProfile) -> UnitreeGo2SdkDriver:
    network_interface = os.environ.get("OPENROBOTSOUL_GO2_NETWORK_INTERFACE", "").strip()
    if not network_interface:
        raise RuntimeError(
            "Set OPENROBOTSOUL_GO2_NETWORK_INTERFACE to the Ethernet interface connected to the robot"
        )
    bridge_library = os.environ.get("OPENROBOTSOUL_GO2_BRIDGE_LIBRARY")
    return UnitreeGo2SdkDriver(profile, network_interface, bridge_library)
