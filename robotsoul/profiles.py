from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from math import isfinite
from pathlib import Path
from typing import Any

from robotsoul.models import CapabilitySpec, ParameterSpec, RiskLevel


@dataclass(frozen=True)
class RobotProfile:
    profile_id: str
    robot_module: str
    profile_version: str
    manufacturer: str
    model: str
    adapter_id: str
    robot_id: str
    capabilities: tuple[CapabilitySpec, ...]
    required_capabilities: tuple[str, ...]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIRECTORIES = {
    "simulator": PROJECT_ROOT / "adapters" / "simulator",
    "unitree_go2": PROJECT_ROOT / "adapters" / "unitree_go2",
    "mimax2": PROJECT_ROOT / "adapters" / "mimax2",
}
DEFAULT_PROFILE_MODULES = {
    "simulator-basic": "simulator",
    "simulator-compact": "simulator",
    "unitree-go2-development": "unitree_go2",
    "mimax2-basic": "mimax2",
}


def _parse_capability(raw: Any) -> CapabilitySpec:
    if not isinstance(raw, dict):
        raise ValueError("Each capability must be an object")
    allowed = {
        "name", "parameters", "risk_level", "allowed_callers", "requires_operator_confirmation",
        "interruptible", "timeout_ms", "required", "description",
        "estimate_ms", "completion",
    }
    if set(raw) - allowed:
        raise ValueError(f"Unknown capability fields: {sorted(set(raw) - allowed)}")
    required_fields = {"name", "parameters", "risk_level", "allowed_callers", "requires_operator_confirmation"}
    if required_fields - set(raw):
        raise ValueError(f"Missing capability fields: {sorted(required_fields - set(raw))}")
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
        if not isinstance(value_type, str) or value_type not in {"number", "string", "boolean"}:
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
        if value_type == "boolean" and any(value is not None for value in (minimum, maximum, max_length)):
            raise ValueError(f"Boolean parameter {name}.{parameter_name} cannot declare numeric or length bounds")
        parsed_parameters[parameter_name] = ParameterSpec(value_type, minimum, maximum, max_length)
    timeout_ms = raw.get("timeout_ms", 5000)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise ValueError(f"Invalid timeout_ms for {name}")
    try:
        risk_level = RiskLevel(raw["risk_level"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid risk_level for {name}") from exc
    allowed_callers = raw["allowed_callers"]
    if (
        not isinstance(allowed_callers, list)
        or not allowed_callers
        or any(not isinstance(caller, str) or caller not in {"agent", "dashboard", "maintenance"} for caller in allowed_callers)
        or len(allowed_callers) != len(set(allowed_callers))
    ):
        raise ValueError(f"Invalid allowed_callers for {name}")
    if risk_level == RiskLevel.HIGH and ("agent" in allowed_callers or "dashboard" not in allowed_callers):
        raise ValueError(f"High-risk capability {name} must allow Dashboard and cannot allow Agent callers")
    requires_operator_confirmation = raw["requires_operator_confirmation"]
    if not isinstance(requires_operator_confirmation, bool):
        raise ValueError(f"Invalid requires_operator_confirmation for {name}")
    if risk_level == RiskLevel.HIGH and not requires_operator_confirmation:
        raise ValueError(f"High-risk capability {name} must require operator confirmation")
    interruptible = raw.get("interruptible", True)
    required = raw.get("required", False)
    description = raw.get("description", "")
    if not isinstance(interruptible, bool) or not isinstance(required, bool):
        raise ValueError(f"Invalid boolean capability field for {name}")
    if not isinstance(description, str):
        raise ValueError(f"Invalid description for {name}")
    estimate_ms = raw.get("estimate_ms", 0)
    if isinstance(estimate_ms, bool) or not isinstance(estimate_ms, int) or estimate_ms < 0:
        raise ValueError(f"Invalid estimate_ms for {name}")
    completion = raw.get("completion", "accepted")
    if completion not in {"accepted", "nominal", "timed", "measured"}:
        raise ValueError(f"Invalid completion for {name}")
    return CapabilitySpec(
        name=name,
        parameters=parsed_parameters,
        risk_level=risk_level,
        allowed_callers=tuple(allowed_callers),
        requires_operator_confirmation=requires_operator_confirmation,
        interruptible=interruptible,
        timeout_ms=timeout_ms,
        required=required,
        description=description,
        estimate_ms=estimate_ms,
        completion=completion,
    )


def _module_manifest(robot_module: str) -> tuple[Path, str]:
    module_dir = MODULE_DIRECTORIES.get(robot_module)
    if module_dir is None:
        raise ValueError(f"Unknown robot module: {robot_module}")
    manifest_path = module_dir / "module.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Robot module manifest is missing: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Robot module manifest contains invalid JSON: {manifest_path}") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"robot_module", "default_profile"}
        or manifest["robot_module"] != robot_module
        or not isinstance(manifest["default_profile"], str)
        or not manifest["default_profile"]
    ):
        raise ValueError(f"Robot module manifest is invalid: {manifest_path}")
    return module_dir, manifest["default_profile"]


def load_profile(profile_id: str | None, robot_module: str | None = None) -> RobotProfile:
    """Load a profile from a statically registered robot module."""
    if robot_module is None:
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError("Profile selection must be a profile ID")
        robot_module = DEFAULT_PROFILE_MODULES.get(profile_id)
        if robot_module is None:
            raise ValueError(f"Unknown robot profile: {profile_id}")
    module_dir, default_profile = _module_manifest(robot_module)
    selected_profile = profile_id or default_profile
    if not isinstance(selected_profile, str) or not selected_profile:
        raise ValueError("Profile selection must be a profile ID")
    if Path(selected_profile).name != selected_profile:
        raise ValueError("Profile selection must be a profile ID")
    profile_path = module_dir / "profiles" / f"{selected_profile}.json"
    if not profile_path.is_file():
        raise ValueError(f"Unknown profile {selected_profile!r} in robot module {robot_module!r}")
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    allowed = {
        "profile_schema_version", "profile_id", "robot_module", "profile_version", "manufacturer", "model",
        "adapter_id", "robot_id", "required_capabilities", "capabilities",
    }
    if set(raw) != allowed:
        raise ValueError("Profile fields do not match the supported schema")
    if isinstance(raw["profile_schema_version"], bool) or raw["profile_schema_version"] != 2:
        raise ValueError("Unsupported profile schema version")
    if raw["profile_id"] != selected_profile:
        raise ValueError("Profile ID does not match its file name")
    if raw["robot_module"] != robot_module:
        raise ValueError("Profile robot_module does not match its owning module")
    if not isinstance(raw["adapter_id"], str) or not raw["adapter_id"]:
        raise ValueError("Profile must declare an adapter_id")
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
        profile_id=selected_profile,
        robot_module=robot_module,
        profile_version=raw["profile_version"],
        manufacturer=raw["manufacturer"],
        model=raw["model"],
        adapter_id=raw["adapter_id"],
        robot_id=raw["robot_id"],
        capabilities=capabilities,
        required_capabilities=tuple(required),
    )


def list_profiles() -> tuple[str, ...]:
    profiles = []
    for module_name in MODULE_DIRECTORIES:
        module_dir, default_profile = _module_manifest(module_name)
        profile_dir = module_dir / "profiles"
        module_profiles = [path.stem for path in sorted(profile_dir.glob("*.json"))]
        if default_profile not in module_profiles:
            raise ValueError(f"Default profile {default_profile!r} is missing from module {module_name!r}")
        profiles.extend(module_profiles)
    return tuple(profiles)


def load_selected_profile(
    profile_id: str | None = None,
    config_path: str | Path | None = None,
    robot_module: str | None = None,
) -> RobotProfile:
    """Select a registered robot profile from deployment config or CLI override."""
    if robot_module is not None:
        return load_profile(profile_id, robot_module)
    from robotsoul.robot_config import load_robot_deployment_config

    selected_path = Path(config_path) if config_path is not None else PROJECT_ROOT / "config" / "robot.json"
    deployment = load_robot_deployment_config(selected_path.expanduser().resolve())
    selected_module = deployment.robot_module
    selected_profile = profile_id if profile_id is not None else deployment.profile_id
    return load_profile(selected_profile, selected_module)
