"""Local Dashboard settings: operator accounts, cloud model path, and voice config."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from robotsoul.voice.config import DEVICE_IDS, MODEL_IDS, VoiceConfig, load_voice_config


PASSWORD_KDF = "pbkdf2-hmac-sha256"
PASSWORD_ITERATIONS = 200_000
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGED_DATA = Path(__file__).resolve().parent / "data"

_AUDIO_FIELDS = (
    "robot_name",
    "wake_phrase",
    "language",
    "input_device",
    "output_device",
    "follow_up_seconds",
    "session_max_seconds",
    "call_reconnect_grace_seconds",
)
_LLM_FIELDS = ("provider", "base_url", "model", "timeout_seconds", "api_key")
_ACCOUNT_FIELDS = ("username", "password", "current_password")
_directory_lock = Lock()
_llm_lock = Lock()
_audio_lock = Lock()
_dummy_lock = Lock()
_dummy_record: tuple[bytes, bytes] | None = None


def default_operators_path() -> Path:
    return PROJECT_ROOT / "config" / "operators.json"


def default_llm_path() -> Path:
    return PROJECT_ROOT / "config" / "llm.json"


def default_voice_config_path() -> Path:
    project_config = PROJECT_ROOT / "config" / "voice.json"
    packaged_config = PACKAGED_DATA / "voice.json"
    return project_config if project_config.is_file() else packaged_config


def _dummy_password_record() -> tuple[bytes, bytes]:
    global _dummy_record
    with _dummy_lock:
        if _dummy_record is None:
            salt = secrets.token_bytes(16)
            digest = hashlib.pbkdf2_hmac("sha256", b"dashboard-password-check", salt, PASSWORD_ITERATIONS)
            _dummy_record = (salt, digest)
        return _dummy_record


def _reject_unknown(payload: dict, allowed: tuple[str, ...], label: str) -> None:
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise ValueError(f"Unsupported {label} field: {unknown[0]}")


def validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValueError("username must be a string")
    username = username.strip()
    if not username or len(username) > 64:
        raise ValueError("username must contain 1 to 64 characters")
    if any(ord(character) < 32 or character in "/\\:" for character in username):
        raise ValueError("username contains unsupported characters")
    return username


def validate_password(password: str) -> str:
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ValueError("password must contain 8 to 128 characters")
    return password


def _password_record(password: str) -> dict[str, str | int]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return {
        "kdf": PASSWORD_KDF,
        "iterations": PASSWORD_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "digest": base64.b64encode(digest).decode("ascii"),
    }


def _decode_password_record(record: dict) -> tuple[bytes, bytes]:
    if not isinstance(record, dict):
        raise ValueError("operator password record must use pbkdf2-hmac-sha256")
    if record.get("kdf") != PASSWORD_KDF or record.get("iterations") != PASSWORD_ITERATIONS:
        raise ValueError("operator password record must use pbkdf2-hmac-sha256")
    try:
        salt = base64.b64decode(record["salt"], validate=True)
        digest = base64.b64decode(record["digest"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("operator password record must use pbkdf2-hmac-sha256") from exc
    if len(salt) < 16 or len(digest) != 32:
        raise ValueError("operator password record must use pbkdf2-hmac-sha256")
    return salt, digest


def _write_json(path: Path, payload: dict, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    temporary.replace(path)


class OperatorDirectory:
    """Plaintext usernames with a per-account PBKDF2 password digest."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._accounts: dict[str, dict] = {}
        if path is not None and path.is_file():
            self._load()

    @classmethod
    def from_credentials(cls, username: str, password: str) -> OperatorDirectory:
        directory = cls()
        directory.upsert(validate_username(username), validate_password(password), persist=False)
        return directory

    def usernames(self) -> list[str]:
        with _directory_lock:
            return sorted(self._accounts)

    def verify(self, username: str, password: str) -> bool:
        dummy_salt, dummy_digest = _dummy_password_record()
        with _directory_lock:
            record = self._accounts.get(username) if isinstance(username, str) else None
            if record is None:
                salt, digest = dummy_salt, dummy_digest
                present = False
            else:
                salt, digest = _decode_password_record(record["password"])
                present = True
        material = password.encode("utf-8") if isinstance(password, str) else b""
        derived = hashlib.pbkdf2_hmac("sha256", material, salt, PASSWORD_ITERATIONS)
        return present and hmac.compare_digest(derived, digest)

    def upsert(self, username: str, password: str, *, persist: bool = True) -> None:
        username = validate_username(username)
        password = validate_password(password)
        stored = {"username": username, "password": _password_record(password)}
        with _directory_lock:
            self._accounts[username] = stored
            if persist:
                self._save_locked()

    def delete(self, username: str) -> None:
        username = validate_username(username)
        with _directory_lock:
            if username not in self._accounts:
                raise ValueError("operator account was not found")
            if len(self._accounts) <= 1:
                raise ValueError("At least one operator account is required")
            self._accounts.pop(username)
            self._save_locked()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("operator account file is unreadable") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("accounts"), list):
            raise ValueError("operator account file is invalid")
        accounts: dict[str, dict] = {}
        for item in raw["accounts"]:
            if not isinstance(item, dict):
                raise ValueError("operator account file is invalid")
            username = validate_username(item.get("username"))
            if username in accounts:
                raise ValueError("operator account file contains a duplicate username")
            _decode_password_record(item.get("password"))
            accounts[username] = {"username": username, "password": item["password"]}
        self._accounts = accounts

    def _save_locked(self) -> None:
        if self.path is None:
            return
        payload = {
            "schema_version": 1,
            "accounts": [self._accounts[name] for name in sorted(self._accounts)],
        }
        _write_json(self.path, payload, mode=0o600)


@dataclass
class LlmSettings:
    provider: str = "openai-compatible"
    base_url: str = ""
    model: str = ""
    timeout_seconds: int = 30
    api_key: str = ""

    def public(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "api_key_configured": bool(self.api_key),
        }


class LlmConfigStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._settings = LlmSettings()
        if path is not None and path.is_file():
            self._settings = self._read(path)

    def public(self) -> dict:
        with _llm_lock:
            return self._settings.public()

    def update(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("llm settings must be an object")
        _reject_unknown(payload, _LLM_FIELDS, "llm")
        with _llm_lock:
            current = self._settings
            provider = payload.get("provider", current.provider)
            base_url = payload.get("base_url", current.base_url)
            model = payload.get("model", current.model)
            timeout = payload.get("timeout_seconds", current.timeout_seconds)
            api_key = current.api_key if "api_key" not in payload else payload.get("api_key")
            updated = LlmSettings(
                provider=_validate_provider(provider),
                base_url=_validate_base_url(base_url),
                model=_validate_model(model),
                timeout_seconds=_validate_timeout(timeout),
                api_key=_validate_api_key(api_key),
            )
            if self.path is not None:
                _write_json(
                    self.path,
                    {
                        "schema_version": 1,
                        "provider": updated.provider,
                        "base_url": updated.base_url,
                        "model": updated.model,
                        "timeout_seconds": updated.timeout_seconds,
                        "api_key": updated.api_key,
                    },
                    mode=0o600,
                )
            self._settings = updated
            return updated.public()

    @staticmethod
    def _read(path: Path) -> LlmSettings:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("llm settings file is unreadable") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("llm settings file is invalid")
        _reject_unknown(
            {key: value for key, value in raw.items() if key != "schema_version"},
            _LLM_FIELDS,
            "llm",
        )
        return LlmSettings(
            provider=_validate_provider(raw.get("provider", "openai-compatible")),
            base_url=_validate_base_url(raw.get("base_url", "")),
            model=_validate_model(raw.get("model", "")),
            timeout_seconds=_validate_timeout(raw.get("timeout_seconds", 30)),
            api_key=_validate_api_key(raw.get("api_key", "")),
        )


def _validate_provider(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise ValueError("provider must contain 1 to 64 characters")
    return value.strip()


def _validate_base_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("base_url must be an http(s) URL or empty")
    value = value.strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("base_url must be an http(s) URL without embedded credentials")
    return value


def _validate_model(value: object) -> str:
    if not isinstance(value, str) or len(value.strip()) > 128:
        raise ValueError("model must contain at most 128 characters")
    return value.strip()


def _validate_timeout(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or not 1 <= value <= 180:
        raise ValueError("timeout_seconds must be an integer from 1 to 180")
    return value


def _validate_api_key(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512 or any(character in value for character in "\r\n"):
        raise ValueError("api_key must be a single-line string of at most 512 characters")
    return value


def audio_settings_view(config: VoiceConfig) -> dict:
    return {
        "robot_name": config.robot_name,
        "wake_phrase": config.wake_phrase,
        "language": config.language,
        "input_device": config.input_device,
        "output_device": config.output_device,
        "follow_up_seconds": config.follow_up_seconds,
        "session_max_seconds": config.session_max_seconds,
        "call_reconnect_grace_seconds": config.call_reconnect_grace_seconds,
        "models": dict(config.models),
        "devices": sorted(DEVICE_IDS),
    }


def load_audio_settings(path: Path) -> dict:
    return audio_settings_view(load_voice_config(path))


def save_audio_settings(path: Path, updates: dict) -> VoiceConfig:
    if not isinstance(updates, dict):
        raise ValueError("audio settings must be an object")
    _reject_unknown(updates, _AUDIO_FIELDS, "audio")
    with _audio_lock:
        current = load_voice_config(path)
        payload = {
            "schema_version": 1,
            "robot_name": current.robot_name,
            "wake_phrase": current.wake_phrase,
            "language": current.language,
            "models": {name: MODEL_IDS[name] for name in ("kws", "asr", "vad", "tts")},
            "input_device": current.input_device,
            "output_device": current.output_device,
            "follow_up_seconds": current.follow_up_seconds,
            "session_max_seconds": current.session_max_seconds,
            "call_reconnect_grace_seconds": current.call_reconnect_grace_seconds,
        }
        payload.update(updates)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            config = load_voice_config(temporary)
        except ValueError:
            temporary.unlink(missing_ok=True)
            raise
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        temporary.replace(path)
        return config
