from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from robotsoul.models import InvocationContext, InvocationSource, TaskResult
from robotsoul.runtime import RobotRuntime
from robotsoul.robot_config import RobotDeploymentConfig, load_robot_deployment_config, robot_settings_view
from robotsoul.settings import (
    LlmConfigStore,
    OperatorDirectory,
    audio_settings_view,
    default_llm_path,
    default_operators_path,
    default_robot_config_path,
    default_voice_config_path,
    load_audio_settings,
    robot_settings_document,
    save_audio_settings,
    save_robot_settings,
    validate_username,
)
from robotsoul.voice.config import VoiceConfig
from robotsoul.voice.core import CaptureArbiter
from robotsoul.voice.webrtc import WebRtcUnavailable


SESSION_TTL_SECONDS = 30 * 60
ACTION_TTL_SECONDS = 30
MAX_REQUEST_BYTES = 64 * 1024
ASSET_ROOT = Path(__file__).with_name("web")

CAPABILITY_LABELS = {
    "speech.say": "语音播报",
    "head.look": "转动头部",
    "base.move_distance": "直行一段距离",
    "flash.toggle": "开关闪光灯",
    "sport.stop_move": "停止移动",
    "sport.balance_stand": "平衡站立",
    "sport.stand_up": "站立",
    "sport.stand_down": "卧倒",
    "sport.sit": "坐下",
    "sport.rise_sit": "从坐姿起身",
    "sport.hello": "打招呼",
    "sport.stretch": "伸懒腰",
    "sport.content": "表现开心",
    "sport.heart": "比心",
    "sport.scrape": "作揖",
    "sport.free_walk": "灵动行走",
    "sport.static_walk": "静步行走",
    "sport.trot_run": "小跑",
    "sport.economic_gait": "节能步态",
    "sport.recovery_stand": "摔倒后恢复站立",
    "sport.front_flip": "前空翻",
    "sport.front_jump": "向前跳",
    "sport.front_pounce": "向前扑",
    "sport.dance1": "舞蹈一",
    "sport.dance2": "舞蹈二",
    "sport.left_flip": "向左侧翻",
    "sport.back_flip": "后空翻",
    "sport.switch_avoid_mode": "切换避障模式",
    "sport.pose": "摆姿势",
    "sport.classic_walk": "经典步态",
    "sport.switch_joystick": "切换手柄控制",
    "sport.handstand": "倒立",
    "sport.free_bound": "自由弹跳",
    "sport.free_jump": "自由跳跃",
    "sport.free_avoid": "行走时主动避让",
    "sport.walk_upright": "直立行走",
    "sport.cross_step": "交叉步",
    "sport.auto_recover_set": "设置摔倒自动恢复",
    "sport.euler": "调整身体倾角",
    "sport.move": "按时间移动",
    "sport.move_distance": "按距离移动",
    "sport.speed_level": "设置速度档位",
    "sport.auto_recover_get": "查询摔倒自动恢复",
    "vui.set_volume": "设置音量",
}
PARAMETER_LABELS = {
    "text": "播报内容",
    "yaw_deg": "水平转角",
    "pitch_deg": "俯仰角",
    "distance_m": "距离（米）",
    "max_speed_mps": "最高速度（米/秒）",
    "vx": "前进速度",
    "vy": "侧向速度",
    "vyaw": "转向速度",
    "duration_ms": "持续时间（毫秒）",
    "roll": "横滚",
    "pitch": "俯仰",
    "yaw": "偏航",
    "level": "速度档位",
    "volume": "音量（0到10）",
    "enabled": "开关",
}


@dataclass
class _Session:
    session_id: str
    csrf_token: str
    principal_id: str
    role: str
    expires_at: float


@dataclass
class _ActionChallenge:
    challenge_id: str
    session_id: str
    skill: str
    parameters: dict[str, Any]
    task_id: str
    trace_id: str
    digest: str
    requires_operator: bool
    expires_at: float


class DashboardService:
    """Authenticated local HTTP(S) console for a single RobotRuntime."""

    def __init__(
        self,
        runtime: RobotRuntime,
        *,
        operators: OperatorDirectory,
        llm_store: LlmConfigStore | None = None,
        voice_config_path: str | Path | None = None,
        robot_config_path: str | Path | None = None,
        session_ttl_seconds: int = SESSION_TTL_SECONDS,
        action_ttl_seconds: int = ACTION_TTL_SECONDS,
        capture_arbiter: CaptureArbiter | None = None,
        call_media: Any | None = None,
        clock=time.monotonic,
    ) -> None:
        if not operators.usernames():
            raise ValueError("Dashboard operator account is not configured")
        self.runtime = runtime
        self._operators = operators
        self._llm = llm_store or LlmConfigStore(None)
        self._voice_config_path = Path(voice_config_path or default_voice_config_path())
        self._robot_config_path = Path(robot_config_path or default_robot_config_path())
        self.session_ttl_seconds = session_ttl_seconds
        self.action_ttl_seconds = action_ttl_seconds
        self._clock = clock
        self.capture_arbiter = capture_arbiter
        self.call_media = call_media
        self._active_call: dict[str, str] | None = None
        self._sessions: dict[str, _Session] = {}
        self._challenges: dict[str, _ActionChallenge] = {}
        self._attempts: dict[str, tuple[int, float]] = {}
        self._actions: list[dict[str, Any]] = []
        self._lock = Lock()

    def login(self, client_key: str, username: str, password: str) -> tuple[_Session | None, str | None]:
        now = self._clock()
        with self._lock:
            count, window_start = self._attempts.get(client_key, (0, now))
            if now - window_start >= 60:
                count, window_start = 0, now
            if count >= 5:
                return None, "Too many login attempts; try again in one minute"
        candidate = username.strip() if isinstance(username, str) else ""
        if not self._operators.verify(candidate, password):
            with self._lock:
                count, window_start = self._attempts.get(client_key, (0, now))
                if now - window_start >= 60:
                    count, window_start = 0, now
                self._attempts[client_key] = (count + 1, window_start)
            return None, "Invalid operator username or password"
        with self._lock:
            self._attempts.pop(client_key, None)
            session = _Session(
                session_id=secrets.token_urlsafe(32),
                csrf_token=secrets.token_urlsafe(32),
                principal_id=candidate,
                role="operator",
                expires_at=now + self.session_ttl_seconds,
            )
            self._sessions[session.session_id] = session
            return session, None

    def session(self, session_id: str | None) -> _Session | None:
        if not session_id:
            return None
        now = self._clock()
        expired = False
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expires_at <= now:
                self._sessions.pop(session_id, None)
                self._expire_session_challenges(session_id)
                expired = True
            else:
                session.expires_at = now + self.session_ttl_seconds
        if expired:
            self.end_call_for_session(session_id)
            return None
        return session

    def logout(self, session_id: str) -> None:
        self.end_call_for_session(session_id)
        with self._lock:
            self._sessions.pop(session_id, None)
            self._expire_session_challenges(session_id)

    def start_call(self, session: _Session) -> dict[str, Any]:
        if self.capture_arbiter is None:
            raise RuntimeError("Voice capture is not configured")
        stale_call_id = None
        with self._lock:
            if self._active_call is not None:
                state = getattr(self.capture_arbiter, "state", None)
                state_value = getattr(state, "value", state)
                if state_value in {"CALL", "CALL_PENDING", "CALL_RECONNECTING"}:
                    raise RuntimeError("a call is already active")
                stale_call_id = self._active_call["call_id"]
                self._active_call = None
            if stale_call_id is not None and self.call_media is not None:
                self.call_media.close(stale_call_id)
            self.capture_arbiter.begin_call()
            call_id = secrets.token_urlsafe(24)
            self._active_call = {"call_id": call_id, "session_id": session.session_id}
        return {
            "call_id": call_id,
            "state": self.capture_arbiter.state.value,
            "local_mic_released": True,
            "offer_required": True,
            "generation": getattr(self.capture_arbiter, "generation", None),
        }

    def answer_call(self, session: _Session, call_id: str, offer: dict[str, str]) -> dict[str, str]:
        with self._lock:
            active = self._active_call
            if active is None or active["call_id"] != call_id or active["session_id"] != session.session_id:
                raise PermissionError("Call is invalid or does not belong to this session")
        state = getattr(self.capture_arbiter, "state", None)
        state = getattr(state, "value", state)
        if state != "CALL_PENDING":
            try:
                if self.call_media is not None:
                    self.call_media.close(call_id)
            finally:
                with self._lock:
                    if self._active_call is not None and self._active_call["call_id"] == call_id:
                        self._active_call = None
            raise RuntimeError("Call audio lease expired before WebRTC negotiation completed")
        if self.call_media is None:
            if self.capture_arbiter is not None:
                self.capture_arbiter.end_call()
            with self._lock:
                self._active_call = None
            raise NotImplementedError("WebRTC media backend is not configured")
        try:
            if not isinstance(offer.get("sdp"), str) or not offer["sdp"].strip() or offer.get("type") != "offer":
                raise ValueError("a non-empty offer SDP and type 'offer' are required")
            return self.call_media.answer(call_id, session, offer)
        except Exception:
            try:
                self.call_media.close(call_id)
            except Exception:
                pass
            with self._lock:
                self._active_call = None
            if self.capture_arbiter is not None:
                self.capture_arbiter.end_call()
            raise

    def hangup_call(self, session: _Session, call_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            active = self._active_call
            if active is None:
                return {"call_id": call_id, "state": self.capture_arbiter.state.value if self.capture_arbiter else "SLEEP"}
            if active["session_id"] != session.session_id or (call_id and active["call_id"] != call_id):
                raise PermissionError("Call is invalid or does not belong to this session")
            self._active_call = None
            actual_call_id = active["call_id"]
        try:
            if self.call_media is not None:
                self.call_media.close(actual_call_id)
        finally:
            state = self.capture_arbiter.end_call().value if self.capture_arbiter is not None else "SLEEP"
        return {"call_id": actual_call_id, "state": state}

    def end_call_for_session(self, session_id: str | None = None) -> None:
        with self._lock:
            active = self._active_call
            if active is None or (session_id is not None and active["session_id"] != session_id):
                return
            self._active_call = None
        try:
            if self.call_media is not None:
                self.call_media.close(active["call_id"])
        finally:
            if self.capture_arbiter is not None:
                self.capture_arbiter.end_call()

    def close(self) -> None:
        self.end_call_for_session()

    def check_operator_password(self, session: _Session, password: str | None, *, failure_message: str) -> str | None:
        now = self._clock()
        key = f"stepup:{session.session_id}"
        with self._lock:
            count, window_start = self._attempts.get(key, (0, now))
            if now - window_start >= 60:
                count, window_start = 0, now
            if count >= 5:
                return "Too many password attempts; try again in one minute"
        if not self._operators.verify(session.principal_id, password if isinstance(password, str) else ""):
            with self._lock:
                count, window_start = self._attempts.get(key, (0, now))
                if now - window_start >= 60:
                    count, window_start = 0, now
                self._attempts[key] = (count + 1, window_start)
            return failure_message
        with self._lock:
            self._attempts.pop(key, None)
        return None

    def settings_view(self, session: _Session) -> dict[str, Any]:
        return {
            "llm": self._llm.public(),
            "robot": robot_settings_document(self._robot_config_path),
            "audio": load_audio_settings(self._voice_config_path),
            "accounts": [{"username": name} for name in self._operators.usernames()],
            "current_username": session.principal_id,
        }

    def update_llm(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"llm": self._llm.update(payload)}

    def update_audio(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = save_audio_settings(self._voice_config_path, payload)
        self._apply_audio(config)
        return {
            "audio": audio_settings_view(config),
            "restart_required": ["wake_phrase", "language", "input_device", "output_device"],
        }

    def update_robot(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = save_robot_settings(self._robot_config_path, payload)
        self._apply_robot(config)
        return {"robot": robot_settings_view(config)}

    def upsert_account(self, session: _Session, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_account_payload(payload, ("username", "password", "current_password"))
        self._require_current_password(session, payload)
        self._operators.upsert(payload.get("username"), payload.get("password"))
        return self._account_view(session)

    def delete_account(self, session: _Session, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_account_payload(payload, ("username", "current_password"))
        self._require_current_password(session, payload)
        username = validate_username(payload.get("username"))
        if username == session.principal_id:
            raise ValueError("The signed-in account cannot be removed")
        self._operators.delete(username)
        return self._account_view(session)

    def _require_account_payload(self, payload: dict[str, Any], allowed: tuple[str, ...]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("account settings must be an object")
        unknown = sorted(set(payload) - set(allowed))
        if unknown:
            raise ValueError(f"Unsupported account field: {unknown[0]}")

    def _require_current_password(self, session: _Session, payload: dict[str, Any]) -> None:
        error = self.check_operator_password(
            session,
            payload.get("current_password"),
            failure_message="Current operator password is incorrect",
        )
        if error:
            raise PermissionError(error)

    def _account_view(self, session: _Session) -> dict[str, Any]:
        return {
            "accounts": [{"username": name} for name in self._operators.usernames()],
            "current_username": session.principal_id,
        }

    def _apply_audio(self, config: VoiceConfig) -> None:
        voice = self.capture_arbiter
        if voice is None or not hasattr(voice, "gate"):
            return
        voice.gate.follow_up_seconds = config.follow_up_seconds
        voice.gate.session_max_seconds = config.session_max_seconds
        if hasattr(voice, "call_reconnect_grace_seconds"):
            voice.call_reconnect_grace_seconds = config.call_reconnect_grace_seconds
        if hasattr(voice, "_call_reconnect_grace_seconds"):
            voice._call_reconnect_grace_seconds = config.call_reconnect_grace_seconds

    def _apply_robot(self, config: RobotDeploymentConfig) -> None:
        voice = self.capture_arbiter
        if voice is not None and hasattr(voice, "robot_name"):
            voice.robot_name = config.robot_name

    def _expire_session_challenges(self, session_id: str) -> None:
        for challenge_id in [
            key for key, item in self._challenges.items() if item.session_id == session_id
        ]:
            self._challenges.pop(challenge_id, None)

    def overview(self) -> dict[str, Any]:
        robot = self.runtime.adapter
        try:
            connected = bool(robot.health())
        except Exception:
            connected = False
        try:
            manufacturer, model = robot.identity()
        except Exception:
            manufacturer, model = self.runtime.profile.manufacturer, self.runtime.profile.model
        try:
            capabilities = self.runtime.dashboard_tools()
        except (AttributeError, TypeError):
            capabilities = self.runtime.available_tools()
        voice_state = getattr(self.capture_arbiter, "state", None)
        voice_state = getattr(voice_state, "value", voice_state)
        voice_health = self.capture_arbiter.health() if hasattr(self.capture_arbiter, "health") else None
        robot_identity = None
        try:
            robot_identity = load_robot_deployment_config(self._robot_config_path)
            display_name = robot_identity.robot_name
        except ValueError:
            display_name = getattr(self.capture_arbiter, "robot_name", None)
        payload = {
            "robot": {
                "robot_id": self.runtime.profile.robot_id,
                "manufacturer": manufacturer,
                "model": model,
                "connected": connected,
                "healthy": connected,
                "battery": self._battery_view(robot),
            },
            "robot_configuration": self._robot_configuration_view(robot_identity),
            "active_task": None,
            "voice": {
                "state": voice_state or "SLEEP",
                "available": getattr(voice_health, "available", self.capture_arbiter is not None),
                "robot_name": display_name,
                "error": getattr(voice_health, "error", None),
            },
            "latest_feedback": self._actions[-1]["message"] if self._actions else None,
            "policy_version": getattr(self.runtime.profile, "profile_version", "unknown"),
            "capabilities": [self._capability_view(name, spec) for name, spec in capabilities.items()],
            "actions": list(reversed(self._actions[-100:])),
        }
        return payload

    def _robot_configuration_view(self, identity: RobotDeploymentConfig | None) -> dict[str, Any]:
        profile = self.runtime.profile
        view = {
            "robot_id": profile.robot_id,
            "manufacturer": profile.manufacturer,
            "model": profile.model,
            "robot_module": profile.robot_module,
            "profile_id": profile.profile_id,
            "adapter_id": profile.adapter_id,
            "profile_version": profile.profile_version,
            "robot_name": identity.robot_name if identity is not None else None,
            "soul_personality": identity.soul.personality if identity is not None else "",
        }
        return view

    def _battery_view(self, robot: Any) -> dict[str, Any] | None:
        reader = getattr(robot, "battery", None)
        if not callable(reader):
            return None
        try:
            status = reader()
        except Exception:
            return None
        if status is None:
            return None
        level = getattr(status, "level_percent", None)
        charging = getattr(status, "charging", None)
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100:
            return None
        if charging is not None and not isinstance(charging, bool):
            return None
        return {"level_percent": level, "charging": charging}

    def sensor_snapshot(self, selected: set[str] | None = None) -> dict[str, Any]:
        raw = self._robot_sensor_snapshot()
        streams: list[dict[str, Any]] = []
        for item in raw.get("streams", []):
            if not isinstance(item, dict):
                continue
            stream_id = item.get("id")
            event_type = item.get("event_type")
            if not isinstance(stream_id, str) or not stream_id or not isinstance(event_type, str) or not event_type:
                continue
            if selected is not None and stream_id not in selected and event_type not in selected:
                continue
            streams.append(item)
        payload: dict[str, Any] = {"streams": streams}
        error = raw.get("error")
        if isinstance(error, str) and error:
            payload["error"] = error
        return payload

    def _robot_sensor_snapshot(self) -> dict[str, Any]:
        robot = self.runtime.adapter
        reader = getattr(robot, "sensor_snapshot", None)
        if not callable(reader):
            return {"streams": []}
        try:
            snapshot = reader()
        except Exception as exc:
            return {"streams": [], "error": str(exc)}
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("streams"), list):
            return {"streams": [], "error": "sensor snapshot is invalid"}
        return snapshot

    def runtime_logs(self, limit: int = 100, level: str | None = None) -> list[str]:
        from robotsoul.runlog import current

        return current().recent(limit, level)

    def audio_monitor(self, channels: set[str] | None = None) -> dict[str, Any]:
        wanted = {"input", "output"} if channels is None else {name for name in channels if name in {"input", "output"}}

        def empty() -> dict[str, Any]:
            payload: dict[str, Any] = {}
            if "input" in wanted:
                payload["input"] = []
                payload["input_active"] = False
            if "output" in wanted:
                payload["output"] = []
                payload["output_active"] = False
            return payload

        monitor = getattr(self.capture_arbiter, "monitor", None)
        snapshot = getattr(monitor, "snapshot", None)
        if snapshot is None:
            return empty()
        try:
            data = snapshot()
        except Exception:
            return empty()
        if not isinstance(data, dict):
            return empty()
        payload = empty()
        if "input" in wanted:
            payload["input"] = data.get("input") if isinstance(data.get("input"), list) else []
            payload["input_active"] = bool(data.get("input_active"))
        if "output" in wanted:
            payload["output"] = data.get("output") if isinstance(data.get("output"), list) else []
            payload["output_active"] = bool(data.get("output_active"))
        return payload

    def _capability_view(self, name: str, capability: Any) -> dict[str, Any]:
        risk = getattr(capability, "risk_level", "normal")
        callers = sorted(getattr(capability, "allowed_callers", ("agent", "dashboard")))
        dashboard_allowed = "dashboard" in callers
        enabled = True
        reason = None
        if risk == "high" and not dashboard_allowed:
            enabled, reason = False, "Dashboard caller is not authorized by policy"
        return {
            "name": name,
            "label": CAPABILITY_LABELS.get(name, name),
            "description": capability.description or CAPABILITY_LABELS.get(name, name),
            "risk_level": risk,
            "allowed_callers": callers,
            "dashboard_allowed": dashboard_allowed,
            "operator_confirmation_required": getattr(capability, "requires_operator_confirmation", risk == "high"),
            "ai_review_required": risk == "confirm" and self.runtime.safety_reviewer is not None,
            "enabled": enabled,
            "disabled_reason": reason,
            "parameters": {
                parameter: {
                    "type": spec.value_type,
                    "label": PARAMETER_LABELS.get(parameter, parameter),
                    "min": spec.minimum,
                    "max": spec.maximum,
                    "max_length": spec.max_length,
                }
                for parameter, spec in capability.parameters.items()
            },
            "timeout_ms": capability.timeout_ms,
            "estimate_ms": getattr(capability, "estimate_ms", 0),
            "completion": getattr(capability, "completion", "accepted"),
            "interruptible": capability.interruptible,
        }

    def prepare_action(self, session: _Session, skill: str, parameters: dict[str, Any]) -> dict[str, Any]:
        capabilities = self.runtime.dashboard_tools()
        capability = capabilities.get(skill)
        if capability is None:
            raise PermissionError("Operation is not enabled for Dashboard callers")
        if not isinstance(parameters, dict):
            raise ValueError("parameters must be an object")
        context = InvocationContext(
            source=InvocationSource.DASHBOARD,
            principal_id=session.principal_id,
            session_id=session.session_id,
            role=session.role,
        )
        validation_error = self.runtime.validate_skill_request(skill, parameters, context)
        if validation_error is not None:
            raise PermissionError(validation_error)
        canonical = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        challenge_id = secrets.token_urlsafe(24)
        task_id = secrets.token_urlsafe(18)
        trace_id = secrets.token_urlsafe(18)
        binding = {
            "robot_id": self.runtime.profile.robot_id,
            "task_id": task_id,
            "trace_id": trace_id,
            "skill": skill,
            "parameters": json.loads(canonical),
            "profile_version": self.runtime.profile.profile_version,
            "session_id": session.session_id,
            **self._capability_policy_binding(capability),
        }
        digest = hashlib.sha256(
            json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        risk = getattr(capability, "risk_level", "normal")
        requires_operator = risk == "high" or getattr(capability, "requires_operator_confirmation", False)
        ai_review_required = risk == "confirm" and self.runtime.safety_reviewer is not None
        challenge = _ActionChallenge(
            challenge_id=challenge_id,
            session_id=session.session_id,
            skill=skill,
            parameters=json.loads(canonical),
            task_id=task_id,
            trace_id=trace_id,
            digest=digest,
            requires_operator=requires_operator,
            expires_at=self._clock() + self.action_ttl_seconds,
        )
        with self._lock:
            self._challenges[challenge.challenge_id] = challenge
        return {
            "challenge_id": challenge.challenge_id,
            "digest": digest,
            "expires_in_seconds": self.action_ttl_seconds,
            "operation": skill,
            "label": CAPABILITY_LABELS.get(skill, skill),
            "parameters": challenge.parameters,
            "risk_level": getattr(capability, "risk_level", "normal"),
            "ai_review_required": ai_review_required,
            "requires_operator_confirmation": requires_operator,
        }

    def execute_action(
        self,
        session: _Session,
        challenge_id: str,
        operator_password: str | None = None,
    ) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            challenge = self._challenges.get(challenge_id)
            if challenge is None or challenge.session_id != session.session_id:
                raise PermissionError("Action confirmation is invalid or already used")
            self._challenges.pop(challenge_id)
        if challenge.expires_at <= now:
            raise PermissionError("Action confirmation has expired")
        try:
            capability = self.runtime.dashboard_tools().get(challenge.skill)
            if capability is None:
                raise PermissionError("Operation policy changed; prepare a new action")
            digest = self._action_digest(
                session.session_id,
                challenge.task_id,
                challenge.trace_id,
                challenge.skill,
                challenge.parameters,
            )
            if not hmac.compare_digest(digest, challenge.digest):
                raise PermissionError("Action confirmation does not match its parameters")
            risk = getattr(capability, "risk_level", "normal")
            requires_operator = risk == "high" or getattr(
                capability, "requires_operator_confirmation", False
            )
            if requires_operator != challenge.requires_operator:
                raise PermissionError("Confirmation policy changed; prepare a new action")
            if requires_operator:
                password_error = self.check_operator_password(
                    session,
                    operator_password,
                    failure_message="A fresh operator password is required for this action",
                )
                if password_error:
                    raise PermissionError(password_error)
            operator_confirmed = risk == "high" or getattr(
                capability, "requires_operator_confirmation", False
            )
            context = InvocationContext(
                source=InvocationSource.DASHBOARD,
                principal_id=session.principal_id,
                session_id=session.session_id,
                role=session.role,
                operator_confirmed=operator_confirmed,
            )
            result: TaskResult = self.runtime.invoke_skill(
                challenge.skill,
                challenge.parameters,
                invocation_context=context,
                operator_confirmed=operator_confirmed,
                task_id=challenge.task_id,
                trace_id=challenge.trace_id,
            )
            action = {
                "operation": challenge.skill,
                "label": CAPABILITY_LABELS.get(challenge.skill, challenge.skill),
                "source": "dashboard",
                "risk_level": risk,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": result.task.status.value,
                "message": result.task.error or (result.evaluation.conclusion if result.evaluation else result.task.status.value),
                "task_id": result.task.task_id,
                "digest": challenge.digest,
                "review": next(
                    (
                        record.details
                        for record in reversed(result.history)
                        if record.event_type == "safety.review.completed"
                    ),
                    None,
                ),
                "operator_confirmed": operator_confirmed,
            }
            with self._lock:
                self._actions.append(action)
            return {"action": action}
        except Exception:
            raise

    def _action_digest(
        self,
        session_id: str,
        task_id: str,
        trace_id: str,
        skill: str,
        parameters: dict[str, Any],
    ) -> str:
        binding = {
            "robot_id": self.runtime.profile.robot_id,
            "task_id": task_id,
            "trace_id": trace_id,
            "skill": skill,
            "parameters": parameters,
            "profile_version": self.runtime.profile.profile_version,
            "session_id": session_id,
        }
        capability = self.runtime.dashboard_tools().get(skill)
        if capability is None:
            return ""
        binding.update(self._capability_policy_binding(capability))
        return hashlib.sha256(
            json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _capability_policy_binding(capability: Any) -> dict[str, Any]:
        return {
            "risk_level": getattr(capability, "risk_level", "normal"),
            "allowed_callers": sorted(getattr(capability, "allowed_callers", ("agent", "dashboard"))),
            "operator_confirmation_required": getattr(capability, "requires_operator_confirmation", False),
            "parameters_schema": {
                name: {
                    "type": spec.value_type,
                    "min": spec.minimum,
                    "max": spec.maximum,
                    "max_length": spec.max_length,
                }
                for name, spec in capability.parameters.items()
            },
            "timeout_ms": capability.timeout_ms,
            "estimate_ms": getattr(capability, "estimate_ms", 0),
            "completion": getattr(capability, "completion", "accepted"),
            "interruptible": capability.interruptible,
        }


def _invoked_action(result: TaskResult) -> dict[str, str] | None:
    skill = None
    for event_type in ("command.created", "agent.intent"):
        for record in result.history:
            if record.event_type != event_type:
                continue
            candidate = record.details.get("skill")
            if isinstance(candidate, str) and candidate:
                skill = candidate
                break
        if skill is not None:
            break
    if skill is None:
        return None
    return {"skill": skill, "label": CAPABILITY_LABELS.get(skill, skill), "status": result.task.status.value}


def _task_result_view(result: TaskResult) -> dict[str, Any]:
    return {
        "task_id": result.task.task_id,
        "trace_id": result.task.trace_id,
        "status": result.task.status.value,
        "error": result.task.error,
        "evaluation": result.evaluation.conclusion if result.evaluation is not None else None,
        "invoked_action": _invoked_action(result),
        "feedback": [
            {
                "status": item.status.value,
                "message": item.message,
                "error_code": item.error_code,
                "timestamp": item.timestamp.isoformat(),
            }
            for item in result.feedback
        ],
    }


class _DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, service: DashboardService, *, secure: bool, allowed_host: str):
        self.service = service
        self.secure = secure
        super().__init__(address, handler)
        self.allowed_hosts = {
            allowed_host.lower(),
            f"{allowed_host.lower()}:{self.server_port}",
        }

    def server_close(self) -> None:
        self.service.close()
        super().server_close()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: _DashboardHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _log_query(self) -> tuple[int, str | None]:
        query = parse_qs(urlsplit(self.path).query)
        raw = query.get("limit", ["100"])
        try:
            limit = int(raw[0])
        except (TypeError, ValueError):
            limit = 100
        limit = min(200, max(1, limit))
        level = query.get("level", [None])[0]
        if level == "":
            level = None
        if level is not None and level not in {"Info", "Warning", "Error"}:
            raise ValueError("level must be Info, Warning or Error")
        return limit, level

    def _monitor_channels(self) -> set[str] | None:
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if "channels" not in query:
            return None
        selected: set[str] = set()
        for raw in query["channels"]:
            for part in raw.split(","):
                name = part.strip()
                if name in {"input", "output"}:
                    selected.add(name)
        return selected

    def _sensor_query(self) -> tuple[set[str] | None, bool]:
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        once = query.get("once", ["0"])[0] == "1"
        if "streams" not in query:
            return None, once
        selected: set[str] = set()
        for raw in query["streams"]:
            for part in raw.split(","):
                name = part.strip()
                if name:
                    selected.add(name)
        return selected, once

    def _sensor_stream(self) -> None:
        selected, once = self._sensor_query()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        loops = 1 if once else 60
        try:
            for index in range(loops):
                payload = self.server.service.sensor_snapshot(selected)
                data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.wfile.write(b"data: " + data + b"\n\n")
                self.wfile.flush()
                if index + 1 < loops:
                    time.sleep(0.5)
        except (BrokenPipeError, ConnectionError, OSError):
            return

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/" or path == "/index.html":
            self._static("index.html", "text/html; charset=utf-8")
            return
        if path == "/assets/dashboard.css":
            self._static("dashboard.css", "text/css; charset=utf-8")
            return
        if path == "/assets/dashboard.js":
            self._static("dashboard.js", "text/javascript; charset=utf-8")
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/session":
            session = self._session()
            self._json(HTTPStatus.OK, {
                "authenticated": session is not None,
                "csrf_token": session.csrf_token if session else None,
            })
            return
        session = self._require_session()
        if session is None:
            return
        if path == "/api/dashboard/overview":
            self._json(HTTPStatus.OK, self.server.service.overview())
            return
        if path == "/api/capabilities":
            data = self.server.service.overview()
            self._json(HTTPStatus.OK, {"capabilities": data["capabilities"]})
            return
        if path == "/api/proposals":
            self._json(HTTPStatus.OK, {"proposals": []})
            return
        if path == "/api/logs":
            try:
                limit, level = self._log_query()
                lines = self.server.service.runtime_logs(limit, level)
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"lines": lines})
            return
        if path == "/api/voice/monitor":
            self._json(HTTPStatus.OK, self.server.service.audio_monitor(self._monitor_channels()))
            return
        if path == "/api/sensors":
            selected, _once = self._sensor_query()
            self._json(HTTPStatus.OK, self.server.service.sensor_snapshot(selected))
            return
        if path == "/api/sensors/stream":
            self._sensor_stream()
            return
        if path == "/api/settings":
            try:
                self._json(HTTPStatus.OK, self.server.service.settings_view(session))
            except (OSError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/login":
            self._handle_login()
            return
        session = self._require_session(mutating=True)
        if session is None:
            return
        if path == "/api/logout":
            self._discard_body()
            self.server.service.logout(session.session_id)
            self._json(HTTPStatus.OK, {"authenticated": False}, {"Set-Cookie": self._cookie("", max_age=0)})
            return
        body = self._read_json()
        if body is None:
            return
        try:
            if path == "/api/voice/calls/start":
                self._json(HTTPStatus.CREATED, self.server.service.start_call(session))
                return
            if path.startswith("/api/voice/calls/") and path.endswith("/answer"):
                call_id = unquote(path[len("/api/voice/calls/") : -len("/answer")].strip("/"))
                answer = self.server.service.answer_call(session, call_id, {"sdp": body.get("sdp"), "type": body.get("type")})
                current_state = getattr(self.server.service.capture_arbiter, "state", "CALL")
                current_state = getattr(current_state, "value", current_state)
                self._json(HTTPStatus.OK, {"call_id": call_id, "state": current_state, "answer": answer})
                return
            if path.startswith("/api/voice/calls/") and path.endswith("/hangup"):
                call_id = unquote(path[len("/api/voice/calls/") : -len("/hangup")].strip("/"))
                self._json(HTTPStatus.OK, self.server.service.hangup_call(session, call_id))
                return
            if path == "/api/tasks":
                task_text = body.get("text")
                if not isinstance(task_text, str) or not task_text.strip() or len(task_text) > 1000:
                    raise ValueError("text must contain between 1 and 1000 characters")
                result = self.server.service.runtime.run_text(task_text)
                task = _task_result_view(result)
                action = {
                    "operation": "自然语言任务",
                    "source": "agent",
                    "risk_level": "normal",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": task["status"],
                    "message": task["error"] or task["status"],
                    "task_id": task["task_id"],
                }
                with self.server.service._lock:
                    self.server.service._actions.append(action)
                self._json(HTTPStatus.OK, {"task": task, "action": action})
                return
            if path == "/api/actions/prepare":
                skill, parameters = body.get("skill"), body.get("parameters")
                if not isinstance(skill, str) or not isinstance(parameters, dict):
                    raise ValueError("skill and parameters are required")
                self._json(HTTPStatus.CREATED, self.server.service.prepare_action(session, skill, parameters))
                return
            if path == "/api/settings/llm":
                self._json(HTTPStatus.OK, self.server.service.update_llm(body))
                return
            if path == "/api/settings/audio":
                self._json(HTTPStatus.OK, self.server.service.update_audio(body))
                return
            if path == "/api/settings/robot":
                self._json(HTTPStatus.OK, self.server.service.update_robot(body))
                return
            if path == "/api/settings/accounts":
                self._json(HTTPStatus.OK, self.server.service.upsert_account(session, body))
                return
            if path == "/api/settings/accounts/delete":
                self._json(HTTPStatus.OK, self.server.service.delete_account(session, body))
                return
            if path == "/api/actions/execute":
                challenge_id = body.get("challenge_id")
                if not isinstance(challenge_id, str) or not challenge_id:
                    raise ValueError("challenge_id is required")
                result = self.server.service.execute_action(session, challenge_id, body.get("operator_password"))
                self._json(HTTPStatus.OK, result)
                return
            if path.startswith("/api/actions/") and path.endswith("/execute"):
                challenge_id = unquote(path[len("/api/actions/") : -len("/execute")].strip("/"))
                result = self.server.service.execute_action(session, challenge_id, body.get("operator_password"))
                self._json(HTTPStatus.OK, result)
                return
            if path.startswith("/api/proposals/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "Proposal not found"})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except PermissionError as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except NotImplementedError as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc), "media_available": False})
        except WebRtcUnavailable as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc), "media_available": False})
        except RuntimeError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Action could not be completed"})

    def _handle_login(self) -> None:
        body = self._read_json()
        if body is None:
            return
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            host = self.headers.get("Host", "")
            origin_host = parsed.netloc
            if (
                parsed.scheme not in ({"https"} if self.server.secure else {"http"})
                or origin_host.lower() != host.lower()
                or host.lower() not in self.server.allowed_hosts
            ):
                self._json(HTTPStatus.FORBIDDEN, {"error": "Origin is not allowed"})
                return
        session, error = self.server.service.login(self.client_address[0], body.get("username"), body.get("password"))
        if session is None:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": error or "Login failed"})
            return
        self._json(
            HTTPStatus.OK,
            {"authenticated": True, "csrf_token": session.csrf_token},
            {"Set-Cookie": self._cookie(session.session_id)},
        )

    def _session(self) -> _Session | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get("robot_harness_session")
        return self.server.service.session(morsel.value if morsel else None)

    def _require_session(self, *, mutating: bool = False) -> _Session | None:
        session = self._session()
        if session is None:
            self.close_connection = True
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
            return None
        host = self.headers.get("Host", "").lower()
        if host not in self.server.allowed_hosts:
            self.close_connection = True
            self._json(HTTPStatus.FORBIDDEN, {"error": "Host is not allowed"})
            return None
        if mutating:
            token = self.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(token, session.csrf_token):
                self.close_connection = True
                self._json(HTTPStatus.FORBIDDEN, {"error": "CSRF validation failed"})
                return None
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.netloc.lower() != host:
                    self.close_connection = True
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Origin is not allowed"})
                    return None
        return session

    def _discard_body(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            return
        if length > 0:
            self.rfile.read(min(length, MAX_REQUEST_BYTES))
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True

    def _read_json(self) -> dict[str, Any] | None:
        if self.headers.get_content_type() != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Content-Type must be application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = MAX_REQUEST_BYTES + 1
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request body is missing or too large"})
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
            return None
        if not isinstance(payload, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON body must be an object"})
            return None
        return payload

    def _static(self, name: str, content_type: str) -> None:
        try:
            data = (ASSET_ROOT / name).read_bytes()
        except OSError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Asset not found"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _cookie(self, value: str, *, max_age: int = SESSION_TTL_SECONDS) -> str:
        cookie = f"robot_harness_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        if self.server.secure:
            cookie += "; Secure"
        return cookie

    def _json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if self.server.secure:
            self.send_header("Strict-Transport-Security", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)


def create_dashboard_server(
    runtime: RobotRuntime,
    *,
    operator_username: str = "operator",
    operator_password: str | None = None,
    operators_path: str | Path | None = None,
    llm_path: str | Path | None = None,
    voice_config_path: str | Path | None = None,
    robot_config_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    tls_cert: str | Path | None = None,
    tls_key: str | Path | None = None,
    capture_arbiter: CaptureArbiter | None = None,
    call_media: Any | None = None,
) -> _DashboardHTTPServer:
    try:
        is_loopback = ip_address(host).is_loopback or host.lower() == "localhost"
    except ValueError:
        is_loopback = False
    if host in {"0.0.0.0", "::", ""}:
        raise ValueError("Bind to a specific loopback or LAN address, not a wildcard address")
    if not is_loopback and (not tls_cert or not tls_key):
        raise ValueError("Binding the Dashboard to a LAN interface requires TLS certificate and key files")
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("Both TLS certificate and key must be provided")
    if operator_password is not None and operators_path is None:
        operators = OperatorDirectory.from_credentials(operator_username, operator_password)
    else:
        operators = OperatorDirectory(Path(operators_path) if operators_path else default_operators_path())
        if not operators.usernames():
            if not operator_password:
                raise ValueError("Dashboard operator account is not configured")
            operators.upsert(operator_username, operator_password)
    service = DashboardService(
        runtime,
        operators=operators,
        llm_store=LlmConfigStore(Path(llm_path) if llm_path else None),
        voice_config_path=voice_config_path,
        robot_config_path=robot_config_path,
        capture_arbiter=capture_arbiter,
        call_media=call_media,
    )
    server = _DashboardHTTPServer(
        (host, port), DashboardRequestHandler, service, secure=bool(tls_cert), allowed_host=host
    )
    if tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(tls_cert), str(tls_key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def run_dashboard(
    runtime: RobotRuntime,
    *,
    operator_username: str = "operator",
    operator_password: str | None = None,
    operators_path: str | Path | None = None,
    llm_path: str | Path | None = None,
    voice_config_path: str | Path | None = None,
    robot_config_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    tls_cert: str | Path | None = None,
    tls_key: str | Path | None = None,
    voice_service: Any | None = None,
    call_media: Any | None = None,
) -> None:
    from robotsoul.runlog import log as runtime_log

    scheme = "https" if tls_cert else "http"
    runtime_log("Dashboard", "Info", f"开始启动 {scheme}://{host}:{port}")
    server = create_dashboard_server(
        runtime,
        operator_username=operator_username,
        operator_password=operator_password,
        operators_path=operators_path,
        llm_path=llm_path if llm_path is not None else default_llm_path(),
        voice_config_path=voice_config_path,
        robot_config_path=robot_config_path,
        host=host,
        port=port,
        tls_cert=tls_cert,
        tls_key=tls_key,
        capture_arbiter=voice_service,
        call_media=call_media,
    )
    print(f"RobotHarness Dashboard: {scheme}://{host}:{port}")
    if scheme == "http":
        print("Loopback development only; mobile microphone access requires HTTPS.")
    voice_state = "已接入" if voice_service is not None else "未接入"
    runtime_log("Dashboard", "Info", f"监听已开始 {scheme}://{host}:{port} voice={voice_state}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime_log("Dashboard", "Info", "停止")
        server.server_close()
