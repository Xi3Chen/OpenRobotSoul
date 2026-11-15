"""Long-lived language session and on-demand coding dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
import inspect
from hashlib import sha256
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from collections import deque
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable
from uuid import uuid4

from robotsoul.agent import AgentIntent, AgentPlan, AgentRejected
from robotsoul.robot_config import default_robot_config_path, identity_document, load_robot_deployment_config
from robotsoul.models import (AgentDecision, CapabilitySpec, ConversationContext, Observation,
                             ReviewProposal, ReviewVerdict, SafetyReviewResult, TaskContext)
from robotsoul.picoclaw import PicoClawCodingAgent
from robotsoul.sensor_context import language_sensor_context


class LanguageGatewayError(RuntimeError):
    """Raised when the resident language process cannot answer a request."""


class CodingDispatcher:
    """Starts a coding process only when a caller asks for one."""

    def __init__(self, launch: Callable[[str], str] | None = None) -> None:
        self._launch = launch if launch is not None else launch_coding_task
        self.calls: list[str] = []

    def dispatch(self, prompt: str, *, cancel_event: Event | None = None) -> str:
        text = prompt.strip()
        if not text:
            raise ValueError("coding prompt is empty")
        if cancel_event is not None and cancel_event.is_set():
            return "编码任务已取消"
        self.calls.append(text)
        try:
            if "cancel_event" in inspect.signature(self._launch).parameters:
                return self._launch(text, cancel_event=cancel_event)
            return self._launch(text)
        except Exception as exc:
            return f"编码进程没有完成：{exc}"


def launch_coding_task(prompt: str, *, cancel_event: Event | None = None) -> str:
    workspace = os.environ.get("OPENROBOTSOUL_CODING_WORKSPACE", "").strip()
    if not workspace:
        return "未配置 OPENROBOTSOUL_CODING_WORKSPACE，编码进程没有启动"
    result = PicoClawCodingAgent().run_task(prompt, workspace, cancel_event=cancel_event)
    if result.succeeded:
        return result.output.strip() or "编码进程已结束"
    detail = result.stderr.strip() or result.output.strip()
    return detail or "编码进程失败"


def report_startup_failure(dispatcher: CodingDispatcher, message: str) -> str:
    return dispatcher.dispatch(f"模块启动失败：{message}")


def identity_payload(robot_config_path: Path | None = None) -> dict[str, str]:
    try:
        deployment = load_robot_deployment_config(robot_config_path or default_robot_config_path())
        return identity_document(deployment)
    except ValueError:
        return {"robot_name": "机器人", "soul": ""}


class LanguageSession:
    """One resident process that answers newline-delimited JSON requests."""

    def __init__(self, process: subprocess.Popen[str], timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._process = process
        self._timeout = timeout_seconds
        self._lock = Lock()
        self._state_lock = Lock()
        self._closed = False
        self._restart_requested = False
        self._active_task_id: str | None = None
        self._interrupted_tasks: set[str] = set()
        self._launch: tuple[list[str], dict[str, str]] | None = None
        self.protocol_version = 0
        self._resident_paths: tuple[Path, Path] | None = None
        self._config_fingerprint: str = ""
        self._stderr: deque[str] = deque(maxlen=20)
        if process.stderr is not None:
            Thread(target=self._drain_stderr, daemon=True).start()

    @classmethod
    def start(
        cls,
        command: list[str],
        timeout_seconds: float = 35,
        env: dict[str, str] | None = None,
    ) -> "LanguageSession":
        runtime_env = os.environ.copy() if env is None else dict(env)
        runtime_env["PYTHONUNBUFFERED"] = "1"
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=runtime_env,
                bufsize=1,
            )
        except OSError as exc:
            raise LanguageGatewayError(f"无法启动语言进程：{exc}") from exc
        session = cls(process, timeout_seconds)
        session._launch = (list(command), runtime_env)
        return session

    @classmethod
    def start_resident(cls, llm_path: Path, robot_path: Path | None = None) -> "LanguageSession":
        timeout = _llm_timeout(llm_path)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["OPENROBOTSOUL_LLM_CONFIG"] = str(llm_path)
        env["OPENROBOTSOUL_ROBOT_CONFIG"] = str(robot_path or default_robot_config_path())
        session = cls.start(
            [str(dialogue_binary())],
            timeout_seconds=timeout + 5,
            env=env,
        )
        try:
            session.handshake()
        except Exception:
            session.close()
            raise
        session._resident_paths = (llm_path, robot_path or default_robot_config_path())
        session._config_fingerprint = session._fingerprint()
        return session

    def handshake(self) -> None:
        response = self.request({"type": "hello", "version": 1})
        if (response.get("ready") is not True or response.get("version") != 1
                or response.get("context_manager") != "openrobotsoul-reliable" or response.get("tools") != 0):
            raise LanguageGatewayError("对话进程没有通过启动检查")
        self.protocol_version = 1

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_process()
            context = payload.get("task_context") or {}
            task = context.get("task", {}) if isinstance(context, dict) else {}
            task_id = task.get("task_id") or payload.get("task_id")
            with self._state_lock:
                if task_id in self._interrupted_tasks:
                    raise LanguageGatewayError("任务已取消")
                self._active_task_id = task_id
            try:
                if self._closed or self._process.poll() is not None or self._process.stdin is None:
                    raise LanguageGatewayError("语言进程没有在运行")
                request_id = payload.get("id") or str(uuid4())
                message = {**payload, "id": request_id}
                try:
                    self._process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                    self._process.stdin.flush()
                except OSError as exc:
                    raise LanguageGatewayError(f"无法向语言进程发送请求：{exc}") from exc
                line = self._read_stdout()
                if not line:
                    detail = " ".join(self._stderr).strip()
                    raise LanguageGatewayError(detail or "语言进程在响应前退出")
                try:
                    body = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LanguageGatewayError("语言进程返回了无法解析的响应") from exc
                if not isinstance(body, dict) or body.get("id") != request_id:
                    raise LanguageGatewayError("语言进程返回了不匹配的响应")
                return body
            except (LanguageGatewayError, OSError, ValueError):
                self._restart_requested = True
                raise
            finally:
                with self._state_lock:
                    self._active_task_id = None

    def _ensure_process(self) -> None:
        config_changed = self._resident_paths is not None and self._fingerprint() != self._config_fingerprint
        if self._closed or not (self._restart_requested or config_changed):
            return
        self._stop_process()
        was_verified = self.protocol_version == 1
        if self._resident_paths is not None:
            replacement = type(self).start_resident(*self._resident_paths)
        elif self._launch is not None:
            replacement = type(self).start(self._launch[0], self._timeout, self._launch[1])
            try:
                if was_verified:
                    replacement.handshake()
            except Exception:
                replacement.close()
                raise
        else:
            raise LanguageGatewayError("对话进程需要重新启动")
        with self._state_lock:
            self._process = replacement._process
            self._stderr = replacement._stderr
            self._timeout = replacement._timeout
            self.protocol_version = replacement.protocol_version
            self._config_fingerprint = replacement._config_fingerprint
            self._restart_requested = False

    def interrupt(self, task_id: str) -> None:
        # This lock is separate from the blocking protocol request lock.
        with self._state_lock:
            self._interrupted_tasks.add(task_id)
            if self._active_task_id != task_id:
                return
            self._restart_requested = True
            if self._process.poll() is None:
                self._process.kill()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop_process()

    def _fingerprint(self) -> str:
        digest = sha256()
        for path in self._resident_paths or ():
            try:
                digest.update(path.read_bytes())
            except OSError as exc:
                digest.update(str(exc).encode("utf-8"))
        return digest.hexdigest()

    def _stop_process(self) -> None:
        process = self._process
        if self.protocol_version == 1 and process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write(json.dumps({"id": str(uuid4()), "version": 1, "type": "shutdown"}) + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def _read_stdout(self) -> str:
        if self._process.stdout is None:
            return ""
        holder: dict[str, str] = {}

        def read() -> None:
            holder["line"] = self._process.stdout.readline() if self._process.stdout is not None else ""

        thread = Thread(target=read, daemon=True)
        thread.start()
        thread.join(self._timeout)
        if thread.is_alive():
            self._process.kill()
            self._restart_requested = True
            raise LanguageGatewayError("语言进程超时")
        return holder.get("line", "")

    def _drain_stderr(self) -> None:
        if self._process.stderr is None:
            return
        for line in self._process.stderr:
            self._stderr.append(line.rstrip())


class LanguageAgent:
    """Forwards natural language to the resident process and returns one skill or an ordered plan."""

    def __init__(
        self,
        session: LanguageSession,
        coding: CodingDispatcher,
        *,
        robot_config_path: Path | None = None,
    ) -> None:
        self.session = session
        self.coding = coding
        self._robot_config_path = robot_config_path
        self._legacy_conversation = ConversationContext("default", "cli", "local", str(uuid4()))

    def cancel_decision(self, task_id: str) -> None:
        interrupt = getattr(self.session, "interrupt", None)
        if callable(interrupt):
            interrupt(task_id)

    def decide(self, context: TaskContext, capabilities: dict[str, CapabilitySpec]) -> AgentDecision | AgentIntent | AgentPlan:
        payload = {
            "type": "resume" if (context.feedback or context.decision_count) and not context.resume_input else "decide",
            "version": 1,
            "session_key": context.conversation.session_key,
            "text": context.resume_input or context.task.user_input,
            "skills": [_skill_descriptor(item) for item in capabilities.values()],
            "sensors": language_sensor_context(context.observations),
            "identity": identity_payload(self._robot_config_path),
            "task_context": _decision_context(context),
            "evaluators": list(getattr(context, "result_evaluators", ())),
        }
        try:
            decision = self.session.request(payload)
        except LanguageGatewayError as exc:
            raise AgentRejected(str(exc)) from exc
        if "kind" not in decision:
            return self._parse_legacy(decision, defer_coding=True)
        kind = decision.get("kind")
        if not isinstance(kind, str):
            raise AgentRejected("对话进程返回了无效决策")
        if decision.get("tool_calls"):
            raise AgentRejected("对话进程返回了未授权工具调用")
        if kind == "coding_task":
            prompt = decision.get("coding_task") or decision.get("text")
            if not isinstance(prompt, str) or not prompt.strip():
                raise AgentRejected("编码任务缺少内容")
            reason = decision.get("reason")
            return AgentDecision("coding_task", reply=reason if isinstance(reason, str) else "", coding_task=prompt)
        raw_steps = decision.get("steps", decision.get("plan", []))
        if not isinstance(raw_steps, list):
            raise AgentRejected("对话进程返回了无效步骤")
        steps = []
        for item in raw_steps:
            if not isinstance(item, dict) or not isinstance(item.get("skill"), str) or not isinstance(item.get("parameters"), dict):
                raise AgentRejected("对话进程返回了无效步骤")
            steps.append(AgentIntent(item["skill"], item["parameters"]))
        reply = decision.get("reply", decision.get("reason", ""))
        refs = decision.get("evidence_refs", [])
        if not isinstance(reply, str) or not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
            raise AgentRejected("对话进程返回了无效答复")
        return AgentDecision(
            kind, tuple(steps), reply,
            decision.get("rule"), decision.get("wait"), tuple(refs),
            str(decision.get("decision_id", "")), str(decision.get("evaluator", "skill_completion")),
        )

    def record_note(self, conversation: ConversationContext, event_id: str, text: str, user_text: str | None = None) -> None:
        # Raw start() remains available to existing stateless process adapters.
        if getattr(self.session, "protocol_version", 0) != 1:
            return
        response = self.session.request({"session_key": conversation.session_key, "event_id": event_id,
                                         "text": text, "user_text": user_text or "", "type": "note", "version": 1})
        if response.get("recorded") is not True:
            raise LanguageGatewayError(str(response.get("reason", "无法保存任务结果")))

    def plan(
        self,
        user_input: str,
        capabilities: dict[str, CapabilitySpec],
        observations: tuple[Observation, ...] = (),
    ) -> AgentIntent | AgentPlan | None:
        try:
            decision = self.session.request(
                {
                    "type": "decide",
                    "session_key": self._legacy_conversation.session_key,
                    "text": user_input,
                    "skills": [_skill_descriptor(item) for item in capabilities.values()],
                    "observations": [item.event_type for item in observations[:8]],
                    "sensors": language_sensor_context(observations),
                    "identity": identity_payload(self._robot_config_path),
                }
            )
        except LanguageGatewayError as exc:
            raise AgentRejected(str(exc)) from exc
        return self._parse_legacy(decision)

    def _parse_legacy(self, decision: dict[str, Any], *, defer_coding: bool = False) -> AgentIntent | AgentPlan | AgentDecision:
        if decision.get("tool_calls"):
            raise AgentRejected("对话进程返回了未授权工具调用")
        coding_task = decision.get("coding_task")
        if isinstance(coding_task, str) and coding_task.strip():
            reason = decision.get("reason")
            reason_text = reason.strip() if isinstance(reason, str) and reason.strip() else "这句话需要改代码，没有调用机器人技能"
            if defer_coding:
                return AgentDecision("coding_task", reply=reason_text, coding_task=coding_task, legacy_coding=True)
            note = self.coding.dispatch(coding_task)
            raise AgentRejected(f"{reason_text} {note}".strip())
        if decision.get("rejected") is True:
            reason = decision.get("reason")
            raise AgentRejected(reason if isinstance(reason, str) else "")
        plan = decision.get("plan")
        rationale = decision.get("rationale")
        rationale_text = rationale if isinstance(rationale, str) else ""
        if isinstance(plan, list):
            steps: list[AgentIntent] = []
            for item in plan:
                if not isinstance(item, dict):
                    raise AgentRejected("语言进程返回了无法执行的计划")
                skill = item.get("skill")
                parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else None
                if not isinstance(skill, str) or parameters is None:
                    raise AgentRejected("语言进程返回了无法执行的计划")
                steps.append(AgentIntent(skill, parameters))
            if not steps:
                raise AgentRejected("语言进程返回了无法执行的计划")
            return AgentPlan(tuple(steps), rationale_text)
        skill = decision.get("skill")
        parameters = decision.get("parameters") if isinstance(decision.get("parameters"), dict) else None
        if not isinstance(skill, str) or parameters is None:
            raise AgentRejected("语言进程返回了无法执行的技能")
        return AgentIntent(skill, parameters, rationale_text)


class LanguageSafetyReviewer:
    """Tool-free confirm-tier review through the same resident process."""

    def __init__(self, session: LanguageSession, *, robot_config_path: Path | None = None) -> None:
        self.session = session
        self._robot_config_path = robot_config_path

    def review(self, proposal: ReviewProposal) -> SafetyReviewResult:
        try:
            decision = self.session.request(
                {
                    "type": "review",
                    "task_id": proposal.command.task_id,
                    "skill": proposal.command.skill,
                    "parameters": proposal.command.parameters,
                    "user_input": proposal.user_input,
                    "risk_level": proposal.risk_level.value,
                    "identity": identity_payload(self._robot_config_path),
                }
            )
        except LanguageGatewayError as exc:
            return SafetyReviewResult(ReviewVerdict.DENY, proposal.arguments_digest, str(exc))
        verdict_name = decision.get("verdict")
        verdict = {
            "allow": ReviewVerdict.ALLOW,
            "deny": ReviewVerdict.DENY,
            "uncertain": ReviewVerdict.UNCERTAIN,
        }.get(verdict_name if isinstance(verdict_name, str) else "", ReviewVerdict.UNCERTAIN)
        rationale = decision.get("rationale")
        reason = rationale.strip() if isinstance(rationale, str) else ""
        if verdict != ReviewVerdict.ALLOW and not reason:
            reason = "复核没有给出拒绝理由，已拒绝执行"
        if verdict == ReviewVerdict.ALLOW and not reason:
            reason = "复核允许执行"
        return SafetyReviewResult(verdict, proposal.arguments_digest, reason)


def _skill_descriptor(capability: CapabilitySpec) -> dict[str, Any]:
    descriptor = {
        "name": capability.name,
        "estimate_ms": capability.estimate_ms,
        "completion": capability.completion,
        "risk_level": capability.risk_level.value,
        "parameters": {
            name: {
                "type": spec.value_type,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "max_length": spec.max_length,
            }
            for name, spec in capability.parameters.items()
        },
    }
    if capability.description:
        descriptor["description"] = capability.description
    if capability.completion == "timed":
        descriptor["estimate_note"] = "一次调用的时长就是参数 duration_ms，程序按 2 秒循环补发。estimate_ms 是允许的最长时长。不要把同一次移动拆成多次调用。"
    if capability.completion == "measured":
        descriptor["estimate_note"] = "estimate_ms 只是截止预算。走到 distance_m 才算完成，到预算仍不够则失败。不要把同一次移动拆成多次调用。"
    return descriptor


def _llm_timeout(path: Path) -> int:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        timeout = int(raw.get("timeout_seconds", 30))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        timeout = 30
    return min(max(timeout, 1), 180)


def _wire(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_wire(item) for item in value]
    return value


def _decision_context(context: TaskContext) -> dict[str, Any]:
    """Send each fact once; full command lifecycle/audits remain in TaskStore.

    A 256-event timeline must not repeat hundreds of ACCEPTED/RUNNING records
    and verbose audit copies in every model prompt. Every terminal result and
    its evidence are preserved, with correlation inherited from task above.
    """
    payload = _wire(asdict(context))
    command_skills = {item["details"].get("command_id"): item["details"].get("skill")
                      for item in payload["history"] if item["event_type"] == "command.created"}
    payload["feedback"] = [
        {key: value for key, value in item.items() if key not in {"task_id", "trace_id", "timestamp"}}
        for item in payload["feedback"] if item["status"] in {"succeeded", "failed", "cancelled"}
    ]
    for item in payload["feedback"]:
        skill = command_skills.get(item["command_id"])
        if skill:
            item["skill"] = skill
    if len(payload["feedback"]) > 32:
        # A column table factors repeated status/message/skill values while
        # preserving the exact UUIDs, order and evidence for every command.
        variants: list[dict[str, Any]] = []
        variant_indices: dict[str, int] = {}
        rows = []
        for item in payload["feedback"]:
            variant = {key: value for key, value in item.items()
                       if key not in {"feedback_id", "command_id", "evidence"}}
            encoded = json.dumps(variant, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if encoded not in variant_indices:
                variant_indices[encoded] = len(variants)
                variants.append(variant)
            rows.append([item["feedback_id"], item["command_id"], variant_indices[encoded], item["evidence"]])
        payload["feedback"] = rows
        payload["feedback_columns"] = ["feedback_id", "command_id", "variant", "evidence"]
        payload["feedback_variants"] = variants
        payload["feedback_format"] = "Each ordered row follows feedback_columns; variant indexes feedback_variants. All rows inherit task_id/trace_id from task."
        if not context.resume_input:
            del payload["task"]["user_input"]
            payload["task"]["user_input_ref"] = "request.text"
    payload["history"] = [item for item in payload["history"] if item["event_type"] not in {
        "task.status", "command.feedback", "tool.call.started", "tool.call.completed",
        "task.evaluation", "plan.step_completed", "command.created",
    }]
    # Command identity, skill and ordering remain available without repeating
    # task/trace/timestamp metadata for every audited status change.
    payload["history"] = [{"event_type": item["event_type"], "details": item["details"]}
                          for item in payload["history"]]
    return payload


def dialogue_binary() -> Path:
    configured = os.environ.get("OPENROBOTSOUL_DIALOGUE_BINARY", "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise LanguageGatewayError(f"找不到配置的对话进程：{candidate}")
    from robotsoul.build import detect_host_architecture, detect_host_platform
    host = detect_host_platform()
    arch = detect_host_architecture()
    name = "openrobotsoul-dialogue.exe" if host == "windows" else "openrobotsoul-dialogue"
    project = Path(__file__).resolve().parents[1]
    candidates = (project / "build" / f"{host}-{arch}" / "bin" / name,
                  project / "bin" / name, Path(sys.executable).parent / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    if found:
        return Path(found)
    raise LanguageGatewayError("找不到对话进程，请先运行 python -m robotsoul.build --dialogue-only，或设置 OPENROBOTSOUL_DIALOGUE_BINARY")
