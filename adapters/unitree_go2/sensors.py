"""Turn a Go2 sensor snapshot into language-model readings.

The native bridge publishes every subscribed stream. Observations keep only the
small readings the language model can use. Full values stay on the snapshot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from robotsoul.models import Observation


SENSOR_TTL_MS = 2000

_SPORT_FIELDS = (
    "mode",
    "progress",
    "gait_type",
    "position",
    "velocity",
    "yaw_speed",
    "body_height",
    "error_code",
    "range_obstacle",
    "foot_force",
    "foot_raise_height",
)
_CLOUD_IDS = {"cloud", "cloud_deskewed", "voxel_map"}
_CLOUD_FIELDS = ("frame_id", "width", "height", "data_bytes", "point_step")
_UWB_FIELDS = (
    "distance_est",
    "orientation_est",
    "yaw_est",
    "pitch_est",
    "error_state",
    "enabled_from_app",
)
_LIDAR_FIELDS = (
    "error_state",
    "dirty_percentage",
    "cloud_frequency",
    "cloud_size",
    "cloud_packet_loss_rate",
    "imu_rpy",
    "imu_frequency",
)
_ODOMETRY_FIELDS = ("position", "orientation", "linear", "angular")
_HEIGHT_FIELDS = (
    "frame_id",
    "resolution",
    "width",
    "height",
    "origin",
    "data_count",
    "minimum",
    "maximum",
    "data_omitted",
)


def language_reading(stream_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if stream_id == "sport":
        return _sport_reading(payload)
    if stream_id == "low":
        return _low_reading(payload)
    if stream_id == "lidar_state":
        return _pick(payload, _LIDAR_FIELDS)
    if stream_id == "uwb":
        return _pick(payload, _UWB_FIELDS)
    if stream_id == "odometry":
        return _pick(payload, _ODOMETRY_FIELDS)
    if stream_id == "height_map":
        return _pick(payload, _HEIGHT_FIELDS)
    if stream_id in _CLOUD_IDS:
        return _pick(payload, _CLOUD_FIELDS)
    return None


def sport_language_fields(snapshot: dict[str, Any]) -> dict[str, Any]:
    stream = _available_stream(snapshot, "sport")
    if stream is None:
        return {}
    return language_reading("sport", stream["payload"]) or {}


def sensor_observations(
    robot_id: str,
    snapshot: dict[str, Any],
    *,
    received_at: datetime,
) -> tuple[Observation, ...]:
    streams = snapshot.get("streams")
    if not isinstance(streams, list):
        return ()
    observations: list[Observation] = []
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("id") == "sport":
            continue
        if stream.get("available") is not True or stream.get("language_summary") is not True:
            continue
        payload = stream.get("payload")
        event_type = stream.get("event_type")
        stream_id = stream.get("id")
        observed_at = _observed_at(stream.get("observed_at"))
        if (
            not isinstance(payload, dict)
            or not isinstance(event_type, str)
            or not event_type
            or not isinstance(stream_id, str)
            or not stream_id
            or observed_at is None
        ):
            continue
        reading = language_reading(stream_id, payload)
        if not reading:
            continue
        observations.append(
            Observation(
                event_id=str(uuid4()),
                robot_id=robot_id,
                device_id=f"unitree-go2-{stream_id.replace('_', '-')}",
                event_type=event_type,
                observed_at=observed_at,
                received_at=received_at,
                payload=reading,
                source="unitree_sdk2",
                ttl_ms=SENSOR_TTL_MS,
                privacy_class="operational",
            )
        )
    return tuple(observations)


def _available_stream(snapshot: dict[str, Any], stream_id: str) -> dict[str, Any] | None:
    streams = snapshot.get("streams")
    if not isinstance(streams, list):
        return None
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("id") != stream_id:
            continue
        payload = stream.get("payload")
        if stream.get("available") is True and isinstance(payload, dict):
            return stream
    return None


def _sport_reading(payload: dict[str, Any]) -> dict[str, Any] | None:
    reading = _pick(payload, _SPORT_FIELDS) or {}
    imu = payload.get("imu")
    if isinstance(imu, dict) and "rpy" in imu:
        reading["imu_rpy"] = imu["rpy"]
    return reading or None


def _low_reading(payload: dict[str, Any]) -> dict[str, Any] | None:
    reading: dict[str, Any] = {}
    imu = payload.get("imu")
    if isinstance(imu, dict):
        if "rpy" in imu:
            reading["imu_rpy"] = imu["rpy"]
        if "temperature" in imu:
            reading["imu_temperature"] = imu["temperature"]
    bms = payload.get("bms")
    if isinstance(bms, dict):
        for key in ("soc", "status", "current"):
            if key in bms:
                reading[f"bms_{key}"] = bms[key]
    for key in ("foot_force", "power_v", "power_a", "temperature_ntc1", "temperature_ntc2"):
        if key in payload:
            reading[key] = payload[key]
    motors = payload.get("motors")
    temperatures: list[int] = []
    if isinstance(motors, list):
        for motor in motors:
            temperature = motor.get("temperature") if isinstance(motor, dict) else None
            if isinstance(temperature, int) and not isinstance(temperature, bool):
                temperatures.append(temperature)
    if temperatures:
        reading["motor_temperature_max"] = max(temperatures)
    return reading or None


def _pick(payload: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any] | None:
    reading = {field: payload[field] for field in fields if field in payload}
    return reading or None


def _observed_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        observed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return None
    return observed_at
