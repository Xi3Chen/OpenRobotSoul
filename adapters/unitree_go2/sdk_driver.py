from __future__ import annotations

import ctypes
import json
import os
import platform
import threading
import time
from datetime import datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from adapters.unitree_go2.actions import native_arguments, sport_action, sport_capabilities, validate_parameters
from robotsoul.adapters.base import BatteryStatus
from robotsoul.models import CapabilitySpec, Command, Feedback, FeedbackStatus, Observation, utc_now
from robotsoul.profiles import RobotProfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_TTL_MS = 1500
STATE_BUFFER_SIZE = 8192
ERROR_BUFFER_SIZE = 1024
# Unitree Go2 BmsState status: 6 PRECHG and 7 CHG are charging.
# 0 SAFE, 1 WAKE_UP, 8 DCHG, 9 SELF_DCHG, 11 ALARM, 12 RESET_ALARM, and 13 AUTO_RECOVERY are not.
_CHARGING_STATUSES = frozenset({6, 7})
_KNOWN_BATTERY_STATUSES = frozenset({0, 1, 6, 7, 8, 9, 11, 12, 13})


class UnitreeGo2SdkDriver:
    """Go2 bridge backed by the pinned Unitree SDK2 SportClient.

    A zero sport API code means the service accepted the request. It does not
    prove that the body finished the pose. Velocity commands are locally bounded
    and always followed by StopMove on completion, cancellation, or close.
    """

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
        self._call_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._active_command_id: str | None = None
        self._closing = False

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
        with self._state_lock:
            self._closing = False

    def health(self) -> bool:
        if self._library is None or self._handle is None:
            return False
        error = ctypes.create_string_buffer(ERROR_BUFFER_SIZE)
        try:
            return bool(self._library.go2_bridge_health(self._handle, error, len(error)))
        except (OSError, ValueError):
            return False

    def battery(self) -> BatteryStatus | None:
        if self._library is None or self._handle is None:
            return None
        try:
            return _battery_from_state(self._read_battery())
        except Exception:
            return None

    def identity(self) -> tuple[str, str]:
        return "Unitree", "Go2"

    def discover_capabilities(self) -> dict[str, CapabilitySpec]:
        if not self.health():
            return {}
        return {spec.name: spec for spec in sport_capabilities()}

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
        action = sport_action(command.skill)
        if action is None:
            return self._failed(command, f"Unsupported Go2 sport action: {command.skill}", "unknown_action")
        if command.robot_id != self.robot_id:
            return self._failed(command, "Robot ID mismatch", "robot_mismatch")
        if command.risk_level != action.risk_level:
            return self._failed(command, "Command risk does not match the Go2 sport action", "risk_mismatch")
        if command.deadline <= utc_now():
            return self._failed(command, "Command deadline has expired", "deadline_expired")
        parameter_error = validate_parameters(action, command.parameters)
        if parameter_error:
            return self._failed(command, parameter_error, "invalid_parameters")
        if not self.health():
            return self._failed(command, "Go2 bridge is disconnected or sport state is stale", "bridge_unhealthy")
        if action.kind == "move":
            duration_ms = int(command.parameters["duration_ms"])
            if utc_now() + timedelta(milliseconds=duration_ms + 4500) > command.deadline:
                return self._failed(command, "Command deadline is too soon for the bounded move", "deadline_expired")
        if not self._begin(command):
            return self._failed(command, "Another Go2 sport action is active", "action_busy")
        try:
            if action.kind == "move":
                return self._execute_move(command, action)
            return self._execute_request(command, action)
        finally:
            self._end(command)

    def cancel(self, command: Command) -> Feedback:
        with self._state_lock:
            if self._active_command_id != command.command_id:
                return self._feedback(
                    command, FeedbackStatus.FAILED, "No Go2 motion command is active", "no_active_command"
                )
            self._cancel_event.set()
        try:
            code, _body = self._invoke("stop_move", [], None)
        except Exception as exc:
            return self._feedback(command, FeedbackStatus.FAILED, str(exc), "stop_failed")
        if code != 0:
            return self._feedback(
                command,
                FeedbackStatus.FAILED,
                f"Sport service rejected stop_move with code {code}",
                "stop_failed",
            )
        return self._feedback(command, FeedbackStatus.CANCELLED, "Go2 StopMove was accepted", "")

    def close(self) -> None:
        should_stop = False
        with self._state_lock:
            self._closing = True
            self._cancel_event.set()
            should_stop = self._active_command_id is not None
        if should_stop and self._library is not None and self._handle is not None:
            try:
                self._invoke("stop_move", [], None)
            except Exception:
                pass
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

    def _read_battery(self) -> dict[str, Any]:
        reader = getattr(self._library, "go2_bridge_read_battery", None)
        if not callable(reader):
            raise RuntimeError("Go2 bridge does not report battery state")
        error = ctypes.create_string_buffer(ERROR_BUFFER_SIZE)
        output = ctypes.create_string_buffer(STATE_BUFFER_SIZE)
        try:
            result = reader(self._handle, output, len(output), error, len(error))
        except (OSError, ValueError) as exc:
            raise RuntimeError("Unable to read Go2 battery state from the native bridge") from exc
        if not result:
            raise RuntimeError(_error_text(error, "Unable to read Go2 battery state"))
        try:
            state = json.loads(output.value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Go2 bridge returned invalid battery JSON") from exc
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
        read_battery = getattr(library, "go2_bridge_read_battery", None)
        if callable(read_battery):
            read_battery.argtypes = [
                ctypes.c_void_p,
                char_pointer,
                ctypes.c_size_t,
                char_pointer,
                ctypes.c_size_t,
            ]
            read_battery.restype = ctypes.c_int
        library.go2_bridge_call.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_size_t,
            ctypes.c_int,
            ctypes.c_int,
            char_pointer,
            ctypes.c_size_t,
            char_pointer,
            ctypes.c_size_t,
        ]
        library.go2_bridge_call.restype = ctypes.c_int
        library.go2_bridge_close.argtypes = [ctypes.c_void_p, char_pointer, ctypes.c_size_t]
        library.go2_bridge_close.restype = ctypes.c_int
        library.go2_bridge_destroy.argtypes = [ctypes.c_void_p]
        library.go2_bridge_destroy.restype = None

    def _begin(self, command: Command) -> bool:
        with self._state_lock:
            if self._closing or self._active_command_id is not None:
                return False
            self._active_command_id = command.command_id
            self._cancel_event.clear()
            return True

    def _end(self, command: Command) -> None:
        with self._state_lock:
            if self._active_command_id == command.command_id:
                self._active_command_id = None

    def _execute_request(self, command: Command, action: Any) -> tuple[Feedback, ...]:
        if self._cancel_event.is_set():
            return self._cancelled(command)
        try:
            code, body = self._invoke(action.bridge_name, *native_arguments(action, command.parameters))
        except Exception as exc:
            return self._failed(command, str(exc), "sport_call_failed")
        if self._cancel_event.is_set():
            self._best_effort_stop()
            return self._cancelled(command)
        if code != 0:
            return self._failed(command, f"Sport service rejected {action.bridge_name} with code {code}", "sport_rejected")
        evidence = {"condition_met": True, "api_code": code, "bridge_action": action.bridge_name}
        if "enabled" in body:
            evidence["enabled"] = body["enabled"]
        evidence.update(self._state_evidence())
        return self._accepted(
            command,
            "Sport service accepted the request; body-pose completion is not verified",
            evidence,
        )

    def _execute_move(self, command: Command, action: Any) -> tuple[Feedback, ...]:
        duration_ms = int(command.parameters["duration_ms"])
        numbers, flag = native_arguments(action, command.parameters)
        moved = False
        try:
            if self._cancel_event.is_set():
                return self._cancelled(command)
            code, _body = self._invoke(action.bridge_name, numbers, flag)
            if code != 0:
                return self._failed(command, f"Sport service rejected move with code {code}", "sport_rejected")
            moved = True
            if self._cancel_event.is_set():
                stop_error = self._stop_failure(command)
                if stop_error is not None:
                    return stop_error
                return self._cancelled(command)
            reason = self._wait_for_duration(command, duration_ms)
            stop_error = self._stop_failure(command)
            if stop_error is not None:
                return stop_error
            if reason == "cancelled":
                return self._cancelled(command)
            if reason == "deadline":
                return self._failed(command, "Command deadline expired during the bounded move", "deadline_expired")
        except Exception as exc:
            if moved:
                self._best_effort_stop()
            return self._failed(command, str(exc), "sport_call_failed")
        evidence = {
            "condition_met": True,
            "api_code": 0,
            "bridge_action": "move",
            "duration_ms": duration_ms,
            "stop_api_code": 0,
        }
        evidence.update(self._state_evidence())
        return self._accepted(
            command,
            "Bounded Move finished and StopMove was accepted; body-pose completion is not verified",
            evidence,
        )

    def _wait_for_duration(self, command: Command, duration_ms: int) -> str | None:
        deadline = time.monotonic() + (duration_ms / 1000.0)
        while True:
            if self._cancel_event.is_set():
                return "cancelled"
            if utc_now() >= command.deadline:
                return "deadline"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._cancel_event.wait(min(0.05, remaining))

    def _invoke(self, action: str, numbers: list[float], flag: bool | None) -> tuple[int, dict[str, Any]]:
        with self._call_lock:
            if self._library is None or self._handle is None:
                raise RuntimeError("Go2 bridge is closed")
            error = ctypes.create_string_buffer(ERROR_BUFFER_SIZE)
            output = ctypes.create_string_buffer(STATE_BUFFER_SIZE)
            count = len(numbers)
            native_numbers = (ctypes.c_double * count)(*numbers) if count else None
            try:
                result = self._library.go2_bridge_call(
                    self._handle,
                    action.encode("utf-8"),
                    native_numbers,
                    count,
                    1 if flag is not None else 0,
                    1 if flag else 0,
                    output,
                    len(output),
                    error,
                    len(error),
                )
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"Unable to call Go2 sport action {action}") from exc
            if not result:
                raise RuntimeError(_error_text(error, f"Unable to call Go2 sport action {action}"))
            try:
                body = json.loads(output.value.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Go2 bridge returned invalid action JSON") from exc
        if not isinstance(body, dict) or isinstance(body.get("api_code"), bool) or not isinstance(body.get("api_code"), int):
            raise RuntimeError("Go2 bridge returned an invalid sport result")
        if "enabled" in body and not isinstance(body["enabled"], bool):
            raise RuntimeError("Go2 bridge returned an invalid sport result")
        return body["api_code"], body

    def _best_effort_stop(self) -> int | None:
        try:
            code, _body = self._invoke("stop_move", [], None)
        except Exception:
            return None
        return code

    def _stop_failure(self, command: Command) -> tuple[Feedback, ...] | None:
        stop_code = self._best_effort_stop()
        if stop_code == 0:
            return None
        if stop_code is None:
            return self._failed(command, "StopMove failed after the Go2 motion", "stop_failed")
        return self._failed(
            command,
            f"Sport service rejected stop_move with code {stop_code}",
            "stop_failed",
        )

    def _state_evidence(self) -> dict[str, Any]:
        try:
            state = self._read_state()
        except Exception:
            return {}
        return {
            "mode": state["mode"],
            "error_code": state["error_code"],
            "velocity": state["velocity"],
            "yaw_speed": state["yaw_speed"],
        }

    def _failed(self, command: Command, message: str, error_code: str) -> tuple[Feedback, ...]:
        return (self._feedback(command, FeedbackStatus.FAILED, message, error_code),)

    def _cancelled(self, command: Command) -> tuple[Feedback, ...]:
        return (self._feedback(command, FeedbackStatus.CANCELLED, "Go2 StopMove was accepted", ""),)

    def _accepted(self, command: Command, message: str, evidence: dict[str, Any]) -> tuple[Feedback, ...]:
        accepted_at = utc_now()
        running_at = utc_now()
        finished_at = utc_now()
        return (
            self._feedback(command, FeedbackStatus.ACCEPTED, "Sport request accepted", "", accepted_at),
            self._feedback(command, FeedbackStatus.RUNNING, "Sport request in progress", "", running_at),
            self._feedback(command, FeedbackStatus.SUCCEEDED, message, "", finished_at, evidence),
        )

    def _feedback(
        self,
        command: Command,
        status: FeedbackStatus,
        message: str,
        error_code: str,
        timestamp: datetime | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> Feedback:
        return Feedback(
            feedback_id=str(uuid4()),
            command_id=command.command_id,
            task_id=command.task_id,
            trace_id=command.trace_id,
            status=status,
            timestamp=timestamp or utc_now(),
            message=message,
            error_code=error_code or None,
            evidence=evidence or {},
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


def _battery_from_state(state: Any, *, now: datetime | None = None) -> BatteryStatus:
    expected = {"soc", "status", "current_ma", "observed_at"}
    if not isinstance(state, dict) or set(state) != expected:
        raise RuntimeError("Go2 bridge returned an unexpected battery schema")
    soc = state["soc"]
    status = state["status"]
    current = state["current_ma"]
    if isinstance(soc, bool) or not isinstance(soc, int) or not 0 <= soc <= 100:
        raise RuntimeError("Go2 battery level is invalid")
    if isinstance(status, bool) or not isinstance(status, int) or not 0 <= status <= 255:
        raise RuntimeError("Go2 battery status is invalid")
    if isinstance(current, bool) or not isinstance(current, int):
        raise RuntimeError("Go2 battery current is invalid")
    observed_at = _parse_timestamp(state["observed_at"])
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise RuntimeError("Go2 battery timestamp must include a timezone")
    age_ms = ((now or utc_now()) - observed_at).total_seconds() * 1000
    if age_ms < 0 or age_ms > STATE_TTL_MS:
        raise RuntimeError("Go2 battery sample is stale")
    if status in _CHARGING_STATUSES:
        charging: bool | None = True
    elif status in _KNOWN_BATTERY_STATUSES:
        charging = False
    else:
        charging = None
    return BatteryStatus(level_percent=soc, charging=charging)


def create_sdk_driver(profile: RobotProfile) -> UnitreeGo2SdkDriver:
    network_interface = os.environ.get("OPENROBOTSOUL_GO2_NETWORK_INTERFACE", "").strip()
    if not network_interface:
        raise RuntimeError(
            "Set OPENROBOTSOUL_GO2_NETWORK_INTERFACE to the Ethernet interface connected to the robot"
        )
    bridge_library = os.environ.get("OPENROBOTSOUL_GO2_BRIDGE_LIBRARY")
    return UnitreeGo2SdkDriver(profile, network_interface, bridge_library)
