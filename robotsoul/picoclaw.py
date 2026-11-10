from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PicoClawError(RuntimeError):
    """Raised when the PicoClaw coding agent cannot be started."""


@dataclass(frozen=True)
class CodingTaskResult:
    task_id: str
    session_id: str
    workspace: str
    output: str
    stderr: str
    returncode: int | None
    elapsed_seconds: float
    timed_out: bool = False
    cancelled: bool = False

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and not self.cancelled and self.returncode == 0


class PicoClawCodingAgent:
    """Run a single PicoClaw coding task as a bounded CLI subprocess."""

    def __init__(
        self,
        binary: str | Path | None = None,
        timeout_seconds: int = 300,
        model: str | None = None,
        allow_shell: bool = False,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.binary = Path(binary).expanduser() if binary is not None else None
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.allow_shell = allow_shell

    def run_task(self, prompt: str, workspace: str | Path) -> CodingTaskResult:
        if not prompt.strip():
            raise ValueError("Coding task prompt must not be empty")

        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            raise ValueError(f"Coding workspace does not exist or is not a directory: {workspace_path}")
        if workspace_path.is_relative_to(PROJECT_ROOT) or PROJECT_ROOT.is_relative_to(workspace_path):
            raise ValueError("Coding workspace must be outside the OpenRobotSoul repository tree")

        binary = self._resolve_binary()
        task_id = str(uuid4())
        session_id = f"openrobotsoul-{task_id}"
        command = [
            str(binary),
            "agent",
            f"--message={prompt}",
            f"--session={session_id}",
        ]
        if self.model:
            command.append(f"--model={self.model}")

        started = monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="openrobotsoul-picoclaw-") as home_dir:
                env = self._build_environment(workspace_path, Path(home_dir))
                process = subprocess.Popen(
                    command,
                    cwd=workspace_path,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                try:
                    output, stderr = process.communicate(timeout=self.timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._kill_process(process)
                    output, stderr = process.communicate()
                    return CodingTaskResult(
                        task_id=task_id,
                        session_id=session_id,
                        workspace=str(workspace_path),
                        output=output or "",
                        stderr=stderr or "",
                        returncode=process.returncode,
                        elapsed_seconds=monotonic() - started,
                        timed_out=True,
                    )
                except KeyboardInterrupt:
                    self._kill_process(process)
                    output, stderr = process.communicate()
                    return CodingTaskResult(
                        task_id=task_id,
                        session_id=session_id,
                        workspace=str(workspace_path),
                        output=output or "",
                        stderr=stderr or "",
                        returncode=process.returncode,
                        elapsed_seconds=monotonic() - started,
                        cancelled=True,
                    )
        except OSError as exc:
            raise PicoClawError(f"Could not start PicoClaw at {binary}: {exc}") from exc

        return CodingTaskResult(
            task_id=task_id,
            session_id=session_id,
            workspace=str(workspace_path),
            output=output,
            stderr=stderr,
            returncode=process.returncode,
            elapsed_seconds=monotonic() - started,
        )

    def check_setup(self) -> dict[str, object]:
        """Report local readiness without printing configuration values or secrets."""
        issues: list[str] = []
        binary_path: Path | None = None
        version = "unknown"
        try:
            binary_path = self._resolve_binary()
            version_result = subprocess.run(
                [str(binary_path), "version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                env=self._minimal_environment(),
            )
            if version_result.returncode == 0:
                version_lines = [line.strip() for line in version_result.stdout.splitlines() if line.strip()]
                version = next((line for line in version_lines if "picoclaw" in line.lower()), "available")
            else:
                issues.append("PicoClaw version command failed")
        except (PicoClawError, OSError, subprocess.TimeoutExpired):
            issues.append("PicoClaw executable is missing or could not be started")

        config_path = self._config_path()
        config_found = config_path.is_file()
        hooks_disabled: bool | None = None
        evolution_disabled: bool | None = None
        model_count = 0
        security_file_found = False
        if not config_found:
            issues.append("PicoClaw config.json was not found; run picoclaw onboard first")
        else:
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(config, dict):
                    raise ValueError("config is not an object")
                hooks = config.get("hooks")
                evolution = config.get("evolution")
                hooks_disabled = isinstance(hooks, dict) and hooks.get("enabled") is False
                evolution_disabled = isinstance(evolution, dict) and evolution.get("enabled") is False
                model_list = config.get("model_list", [])
                model_count = len(model_list) if isinstance(model_list, list) else 0
                if not hooks_disabled:
                    issues.append("Set hooks.enabled to false in config.json")
                if not evolution_disabled:
                    issues.append("Set evolution.enabled to false in config.json")
                if model_count == 0:
                    issues.append("Configure at least one model in config.json")
                security_file_found = config_path.with_name(".security.yml").is_file()
            except (OSError, json.JSONDecodeError, ValueError):
                issues.append("PicoClaw config.json could not be parsed")

        return {
            "binary": str(binary_path) if binary_path else None,
            "version": version,
            "config": str(config_path),
            "config_found": config_found,
            "model_count": model_count,
            "security_file_found": security_file_found,
            "hooks_disabled": hooks_disabled,
            "evolution_disabled": evolution_disabled,
            "ready_to_launch": not issues,
            "credentials_validated": False,
            "issues": issues,
        }

    def _resolve_binary(self) -> Path:
        configured = self.binary or os.environ.get("OPENROBOTSOUL_PICOCLAW_BINARY")
        if configured:
            candidate = Path(configured).expanduser().resolve()
            if candidate.is_file():
                return candidate
            raise PicoClawError(f"PicoClaw executable was not found: {candidate}")

        executable_name = "picoclaw.exe" if os.name == "nt" else "picoclaw"
        try:
            from robotsoul.build import detect_host_architecture, detect_host_platform

            bundled = (
                PROJECT_ROOT
                / "build"
                / f"{detect_host_platform()}-{detect_host_architecture()}"
                / "bin"
                / executable_name
            )
            if bundled.is_file():
                return bundled
        except (ImportError, RuntimeError):
            bundled = None

        on_path = shutil.which("picoclaw")
        if on_path:
            return Path(on_path).resolve()
        raise PicoClawError(
            "PicoClaw executable was not found. Build it under "
            f"{PROJECT_ROOT / 'build'}/<platform>-<arch>/bin or set OPENROBOTSOUL_PICOCLAW_BINARY."
        )

    def _build_environment(self, workspace: Path, home: Path) -> dict[str, str]:
        env = self._minimal_environment()
        config_path = self._copy_user_config(home)
        temp_dir = home / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        for directory in (home / "AppData" / "Roaming", home / "AppData" / "Local"):
            directory.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "NO_COLOR": "1",
                "TERM": "dumb",
                "HOME": str(home),
                "USERPROFILE": str(home),
                "APPDATA": str(home / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(home / "AppData" / "Local"),
                "TEMP": str(temp_dir),
                "TMP": str(temp_dir),
                "PICOCLAW_HOME": str(home),
                "PICOCLAW_AGENTS_DEFAULTS_WORKSPACE": str(workspace),
                "PICOCLAW_AGENTS_DEFAULTS_RESTRICT_TO_WORKSPACE": "true",
                "PICOCLAW_AGENTS_DEFAULTS_ALLOW_READ_OUTSIDE_WORKSPACE": "false",
                "PICOCLAW_TOOLS_ALLOW_READ_PATHS": "",
                "PICOCLAW_TOOLS_ALLOW_WRITE_PATHS": "",
                "PICOCLAW_TOOLS_CRON_ENABLED": "false",
                "PICOCLAW_TOOLS_MESSAGE_ENABLED": "false",
                "PICOCLAW_TOOLS_MCP_ENABLED": "false",
                "PICOCLAW_TOOLS_SKILLS_ENABLED": "false",
                "PICOCLAW_TOOLS_SPAWN_ENABLED": "false",
                "PICOCLAW_TOOLS_SUBAGENT_ENABLED": "false",
                "PICOCLAW_TOOLS_WEB_ENABLED": "false",
                "PICOCLAW_TOOLS_WEB_FETCH_ENABLED": "false",
                "PICOCLAW_TOOLS_EXEC_ENABLED": str(self.allow_shell).lower(),
                "PICOCLAW_TOOLS_EXEC_ENABLE_DENY_PATTERNS": "true",
                "PICOCLAW_TOOLS_EXEC_ALLOW_REMOTE": "false",
                "PICOCLAW_HEARTBEAT_ENABLED": "false",
                "PICOCLAW_DEVICES_ENABLED": "false",
                "PICOCLAW_TOOLS_APPEND_FILE_ENABLED": "true",
                "PICOCLAW_TOOLS_EDIT_FILE_ENABLED": "true",
                "PICOCLAW_TOOLS_LIST_DIR_ENABLED": "true",
                "PICOCLAW_TOOLS_READ_FILE_ENABLED": "true",
                "PICOCLAW_TOOLS_WRITE_FILE_ENABLED": "true",
                "PICOCLAW_TOOLS_LOAD_IMAGE_ENABLED": "false",
                "PICOCLAW_MEDIA_CLEANUP_ENABLED": "false",
            }
        )
        if config_path is not None:
            env["PICOCLAW_CONFIG"] = str(config_path)
        for tool in (
            "FIND_SKILLS",
            "INSTALL_SKILL",
            "SEND_FILE",
            "SEND_TTS",
            "I2C",
            "SERIAL",
            "SPI",
        ):
            env[f"PICOCLAW_TOOLS_{tool}_ENABLED"] = "false"
        return env

    @staticmethod
    def _minimal_environment() -> dict[str, str]:
        allowed = {
            "APPDATA",
            "COMSPEC",
            "HOME",
            "LANG",
            "LC_ALL",
            "LOCALAPPDATA",
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "WINDIR",
        }
        return {
            key: value
            for key, value in os.environ.items()
            if key.upper() in allowed or key.upper().startswith("SSL_CERT_")
        }

    @staticmethod
    def _config_path() -> Path:
        config_path = os.environ.get("PICOCLAW_CONFIG")
        if config_path:
            path = Path(config_path).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
        else:
            home = os.environ.get("PICOCLAW_HOME")
            home_path = Path(home).expanduser() if home else Path.home() / ".picoclaw"
            path = home_path / "config.json"
        return path.resolve()

    @classmethod
    def _copy_user_config(cls, isolated_home: Path) -> Path:
        source = cls._config_path()
        if not source.exists():
            if os.environ.get("PICOCLAW_CONFIG"):
                raise PicoClawError(f"PicoClaw config file does not exist: {source}")
            raise PicoClawError(
                "PicoClaw is not configured. Run `picoclaw onboard`, configure a model, "
                "and set hooks.enabled and evolution.enabled to false before running coding tasks."
            )
        try:
            config = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PicoClawError(f"Could not inspect PicoClaw config at {source}: {exc}") from exc
        if not isinstance(config, dict):
            raise PicoClawError(f"PicoClaw config must contain a JSON object: {source}")

        hooks = config.get("hooks") or {}
        evolution = config.get("evolution") or {}
        if not isinstance(hooks, dict) or not isinstance(evolution, dict):
            raise PicoClawError(f"PicoClaw hooks/evolution config must be JSON objects: {source}")
        if hooks.get("enabled") is not False:
            raise PicoClawError(
                "PicoClaw config must explicitly set hooks.enabled to false before running coding tasks."
            )
        if evolution.get("enabled") is not False:
            raise PicoClawError(
                "PicoClaw config must explicitly set evolution.enabled to false before running coding tasks."
            )
        model_list = config.get("model_list")
        if not isinstance(model_list, list) or not model_list:
            raise PicoClawError("PicoClaw config must contain at least one model in model_list.")

        isolated_config = isolated_home / "config.json"
        shutil.copyfile(source, isolated_config)
        source_secrets = source.parent / ".security.yml"
        if source_secrets.is_file():
            shutil.copyfile(source_secrets, isolated_home / ".security.yml")
        return isolated_config

    @staticmethod
    def _kill_process(process: subprocess.Popen[str]) -> None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
