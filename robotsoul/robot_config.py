"""Deployment robot identity and module selection in config/robot.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGED_DATA = Path(__file__).resolve().parent / "data"
_DEFAULT_ROBOT_NAME = "机器人"
_SOUL_FIELDS = frozenset({"personality"})
_ROBOT_CONFIG_FIELDS = frozenset({"schema_version", "robot_module", "profile_id", "robot_name", "soul"})


@dataclass(frozen=True)
class RobotSoul:
    personality: str


@dataclass(frozen=True)
class RobotDeploymentConfig:
    robot_module: str
    profile_id: str | None
    robot_name: str
    soul: RobotSoul


def default_robot_config_path() -> Path:
    project_config = PROJECT_ROOT / "config" / "robot.json"
    packaged_config = PACKAGED_DATA / "robot.json"
    return project_config if project_config.is_file() else packaged_config


def soul_text(soul: RobotSoul) -> str:
    return soul.personality.strip()


def identity_document(config: RobotDeploymentConfig) -> dict[str, str]:
    return {"robot_name": config.robot_name, "soul": soul_text(config.soul)}


def parse_wake_phrases(raw: str, robot_name: str) -> tuple[str, ...]:
    text = raw.strip()
    if not text:
        fallback = robot_name.strip() or _DEFAULT_ROBOT_NAME
        return (fallback,)
    parts = tuple(phrase.strip() for phrase in text.split(",") if phrase.strip())
    if not parts:
        fallback = robot_name.strip() or _DEFAULT_ROBOT_NAME
        return (fallback,)
    return parts


def load_robot_deployment_config(path: str | Path | None = None) -> RobotDeploymentConfig:
    selected_path = Path(path) if path is not None else default_robot_config_path()
    selected_path = selected_path.expanduser().resolve()
    try:
        config_text = selected_path.read_text(encoding="utf-8")
    except OSError as exc:
        if path is not None:
            raise ValueError(f"Could not read robot config: {selected_path}") from exc
        try:
            config_text = files("robotsoul").joinpath("data/robot.json").read_text(encoding="utf-8")
        except OSError as resource_error:
            raise ValueError(f"Could not read robot config: {selected_path}") from resource_error
    try:
        raw = json.loads(config_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Robot config is invalid JSON: {selected_path}") from exc
    return _parse_robot_config(raw)


def save_robot_deployment_config(path: Path, updates: dict[str, Any]) -> RobotDeploymentConfig:
    if not isinstance(updates, dict):
        raise ValueError("robot settings must be an object")
    unknown = sorted(set(updates) - {"robot_name", "soul"})
    if unknown:
        raise ValueError(f"Unsupported robot field: {unknown[0]}")
    current = load_robot_deployment_config(path)
    payload = _robot_config_document(current)
    if "robot_name" in updates:
        payload["robot_name"] = _robot_name(updates["robot_name"])
    if "soul" in updates:
        payload["soul"] = _soul_object(updates["soul"])
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        config = _parse_robot_config(json.loads(temporary.read_text(encoding="utf-8")))
    except ValueError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)
    return config


def robot_settings_view(config: RobotDeploymentConfig) -> dict[str, Any]:
    return {
        "robot_name": config.robot_name,
        "soul": {"personality": config.soul.personality},
        "robot_module": config.robot_module,
        "profile_id": config.profile_id,
    }


def _parse_robot_config(raw: object) -> RobotDeploymentConfig:
    if not isinstance(raw, dict):
        raise ValueError("Robot config must be an object")
    unknown = sorted(set(raw) - _ROBOT_CONFIG_FIELDS)
    if unknown:
        raise ValueError(f"Unsupported robot config field: {unknown[0]}")
    schema_version = raw.get("schema_version", 1)
    if type(schema_version) is not int or schema_version != 1:
        raise ValueError("unsupported robot config schema version")
    robot_module = raw.get("robot_module")
    if not isinstance(robot_module, str) or not robot_module.strip():
        raise ValueError("robot_module must be non-empty text")
    profile_id = raw.get("profile_id")
    if profile_id is not None and (not isinstance(profile_id, str) or not profile_id.strip()):
        raise ValueError("profile_id must be non-empty text when provided")
    robot_name = _robot_name(raw.get("robot_name", _DEFAULT_ROBOT_NAME))
    soul = _soul_object(raw.get("soul", {}))
    return RobotDeploymentConfig(
        robot_module=robot_module.strip(),
        profile_id=profile_id.strip() if isinstance(profile_id, str) else None,
        robot_name=robot_name,
        soul=RobotSoul(personality=soul["personality"]),
    )


def _robot_config_document(config: RobotDeploymentConfig) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "robot_module": config.robot_module,
        "robot_name": config.robot_name,
        "soul": {"personality": config.soul.personality},
    }
    if config.profile_id:
        document["profile_id"] = config.profile_id
    return document


def _robot_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise ValueError("robot_name must contain 1 to 64 characters")
    return value.strip()


def _soul_object(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("soul must be an object with personality")
    unknown = set(value) - _SOUL_FIELDS
    if unknown:
        raise ValueError(f"Unsupported soul field: {sorted(unknown)[0]}")
    personality = value.get("personality", "")
    if not isinstance(personality, str) or len(personality) > 4000:
        raise ValueError("soul.personality must be a string of at most 4000 characters")
    return {"personality": personality.strip()}