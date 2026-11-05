from __future__ import annotations

import json
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any

from robotsoul.models import CapabilitySpec, ParameterSpec


@dataclass(frozen=True)
class RobotProfile:
    profile_id: str
    profile_version: str
    manufacturer: str
    model: str
    adapter_id: str
    robot_id: str
    capabilities: tuple[CapabilitySpec, ...]
    required_capabilities: tuple[str, ...]


def _parse_capability(raw: Any) -> CapabilitySpec:
    if not isinstance(raw, dict):
        raise ValueError("Each capability must be an object")
    allowed = {"name", "parameters", "safety_class", "interruptible", "timeout_ms", "required"}
    if set(raw) - allowed:
        raise ValueError(f"Unknown capability fields: {sorted(set(raw) - allowed)}")
    name = raw.get("name")
    parameters = raw.get("parameters", {})
    if not isinstance(name, str) or not name or not isinstance(parameters, dict):
        raise ValueError("Capability name and parameters are required")
    parsed_parameters: dict[str, ParameterSpec] = {}
    for parameter_name, parameter in parameters.items():
        if not isinstance(parameter_name, str) or not isinstance(parameter, dict):
            raise ValueError(f"Invalid parameter schema in {name}")
        if set(parameter) - {"type", "min", "max", "max_length"}:
            raise ValueError(f"Unknown parameter fields in {name}.{parameter_name}")
        value_type = parameter.get("type")
        if not isinstance(value_type, str) or value_type not in {"number", "string"}:
            raise ValueError(f"Unsupported parameter type in {name}.{parameter_name}")
        minimum = parameter.get("min")
        maximum = parameter.get("max")
        max_length = parameter.get("max_length")
        if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, (int, float))):
            raise ValueError(f"Invalid minimum in {name}.{parameter_name}")
        if maximum is not None and (isinstance(maximum, bool) or not isinstance(maximum, (int, float))):
            raise ValueError(f"Invalid maximum in {name}.{parameter_name}")
        if minimum is not None and not isfinite(minimum):
            raise ValueError(f"Invalid minimum in {name}.{parameter_name}")
        if maximum is not None and not isfinite(maximum):
            raise ValueError(f"Invalid maximum in {name}.{parameter_name}")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"Invalid range in {name}.{parameter_name}")
        if max_length is not None and (
            isinstance(max_length, bool) or not isinstance(max_length, int) or max_length <= 0
        ):
            raise ValueError(f"Invalid max_length in {name}.{parameter_name}")
        parsed_parameters[parameter_name] = ParameterSpec(value_type, minimum, maximum, max_length)
    timeout_ms = raw.get("timeout_ms", 5000)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise ValueError(f"Invalid timeout_ms for {name}")
    safety_class = raw.get("safety_class", "low")
    if safety_class != "low":
        raise ValueError("MVP only enables low-risk skills")
    interruptible = raw.get("interruptible", True)
    required = raw.get("required", False)
    if not isinstance(interruptible, bool) or not isinstance(required, bool):
        raise ValueError(f"Invalid boolean capability field for {name}")
    return CapabilitySpec(
        name=name,
        parameters=parsed_parameters,
        safety_class=safety_class,
        interruptible=interruptible,
        timeout_ms=timeout_ms,
        required=required,
    )


def load_profile(profile_id: str) -> RobotProfile:
    """Load a bundled, non-executable profile selected by its stable ID."""
    if not profile_id or Path(profile_id).name != profile_id:
        raise ValueError("Profile selection must be a profile ID")
    profile_path = Path(__file__).parent / "profiles" / f"{profile_id}.json"
    if not profile_path.is_file():
        raise ValueError(f"Unknown robot profile: {profile_id}")
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    allowed = {
        "profile_schema_version", "profile_id", "profile_version", "manufacturer", "model",
        "adapter_id", "robot_id", "required_capabilities", "capabilities",
    }
    if set(raw) != allowed:
        raise ValueError("Profile fields do not match the supported schema")
    if isinstance(raw["profile_schema_version"], bool) or raw["profile_schema_version"] != 1:
        raise ValueError("Unsupported profile schema version")
    if raw["profile_id"] != profile_id:
        raise ValueError("Profile ID does not match its file name")
    if raw["adapter_id"] != "fake-simulator":
        raise ValueError("Profile references an adapter that is not installed")
    capabilities = tuple(_parse_capability(item) for item in raw["capabilities"])
    names = [capability.name for capability in capabilities]
    if len(names) != len(set(names)):
        raise ValueError("Profile contains duplicate capability names")
    required = raw["required_capabilities"]
    if not isinstance(required, list) or any(name not in names for name in required):
        raise ValueError("Required capabilities must refer to declared capabilities")
    for field_name in ("profile_version", "manufacturer", "model", "robot_id"):
        if not isinstance(raw[field_name], str) or not raw[field_name]:
            raise ValueError(f"Invalid {field_name}")
    return RobotProfile(
        profile_id=profile_id,
        profile_version=raw["profile_version"],
        manufacturer=raw["manufacturer"],
        model=raw["model"],
        adapter_id=raw["adapter_id"],
        robot_id=raw["robot_id"],
        capabilities=capabilities,
        required_capabilities=tuple(required),
    )
