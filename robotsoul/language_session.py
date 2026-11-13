"""Long-lived language session and on-demand coding dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import deque
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable
from uuid import uuid4

from robotsoul.agent import AgentIntent, AgentRejected
from robotsoul.models import CapabilitySpec, Observation, ReviewProposal, ReviewVerdict, SafetyReviewResult
from robotsoul.picoclaw import PicoClawCodingAgent


class LanguageGatewayError(RuntimeError):
    """Raised when the resident language process cannot answer a request."""


class CodingDispatcher:
    """Starts a coding process only when a caller asks for one."""

    def __init__(self, launch: Callable[[str], str] | None = None) -> None:
        self._launch = launch if launch is not None else launch_coding_task
        self.calls: list[str] = []

    def dispatch(self, prompt: str) -> str:
        text = prompt.strip()
        if not text:
            raise ValueError("coding prompt is empty")
        self.calls.append(text)
        try:
            return self._launch(text)
        except Exception as exc:
            return f"编码进程没有完成：{exc}"


def launch_coding_task(prompt: str) -> str:
    workspace = os.environ.get("OPENROBOTSOUL_CODING_WORKSPACE", "").strip()
    if not workspace:
        return "未配置 OPENROBOTSOUL_CODING_WORKSPACE，编码进程没有启动"
    result = PicoClawCodingAgent().run_task(prompt, workspace)
    if result.succeeded:
        return result.output.strip() or "编码进程已结束"
    detail = result.stderr.strip() or result.output.strip()
    return detail or "编码进程失败"


def report_startup_failure(dispatcher: CodingDispatcher, message: str) -> str:
    return dispatcher.dispatch(f"模块启动失败：{message}")


class LanguageSession:
    """One resident process that answers newline-delimited JSON requests."""

    def __init__(self, process: subprocess.Popen[str], timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._process = process
        self._timeout = timeout_seconds
        self._lock = Lock()
        self._closed = False
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
        return cls(process, timeout_seconds)

    @classmethod
    def start_resident(cls, llm_path: Path) -> "LanguageSession":
        timeout = _llm_timeout(llm_path)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["OPENROBOTSOUL_LLM_CONFIG"] = str(llm_path)
        return cls.start(
            [sys.executable, "-m", "robotsoul.language_gateway"],
            timeout_seconds=timeout + 5,
            env=env,
        )

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._closed or self._process.poll() is not None or self._process.stdin is None:
                raise LanguageGatewayError("语言进程没有在运行")
            request_id = str(uuid4())
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

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
        if process.poll() is None:
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
            raise LanguageGatewayError("语言进程超时")
        return holder.get("line", "")

    def _drain_stderr(self) -> None:
        if self._process.stderr is None:
            return
        for line in self._process.stderr:
            self._stderr.append(line.rstrip())


class LanguageAgent:
    """Forwards natural language to the resident process and returns one intent."""

    def __init__(self, session: LanguageSession, coding: CodingDispatcher) -> None:
        self.session = session
        self.coding = coding

    def plan(
        self,
        user_input: str,
        capabilities: dict[str, CapabilitySpec],
        observations: tuple[Observation, ...] = (),
    ) -> AgentIntent | None:
        try:
            decision = self.session.request(
                {
                    "type": "decide",
                    "text": user_input,
                    "skills": [_skill_descriptor(item) for item in capabilities.values()],
                    "observations": [item.event_type for item in observations[:8]],
                }
            )
        except LanguageGatewayError as exc:
            raise AgentRejected(str(exc)) from exc
        coding_task = decision.get("coding_task")
        if isinstance(coding_task, str) and coding_task.strip():
            note = self.coding.dispatch(coding_task)
            reason = decision.get("reason")
            reason_text = reason.strip() if isinstance(reason, str) and reason.strip() else "这句话需要改代码，没有调用机器人技能"
            raise AgentRejected(f"{reason_text} {note}".strip())
        if decision.get("rejected") is True:
            reason = decision.get("reason")
            raise AgentRejected(reason if isinstance(reason, str) else "")
        skill = decision.get("skill")
        parameters = decision.get("parameters") if isinstance(decision.get("parameters"), dict) else None
        if not isinstance(skill, str) or parameters is None:
            raise AgentRejected("语言进程返回了无法执行的技能")
        rationale = decision.get("rationale")
        return AgentIntent(skill, parameters, rationale if isinstance(rationale, str) else "")


class LanguageSafetyReviewer:
    """Tool-free confirm-tier review through the same resident process."""

    def __init__(self, session: LanguageSession) -> None:
        self.session = session

    def review(self, proposal: ReviewProposal) -> SafetyReviewResult:
        try:
            decision = self.session.request(
                {
                    "type": "review",
                    "skill": proposal.command.skill,
                    "parameters": proposal.command.parameters,
                    "user_input": proposal.user_input,
                    "risk_level": proposal.risk_level.value,
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
    return descriptor


def _llm_timeout(path: Path) -> int:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        timeout = int(raw.get("timeout_seconds", 30))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        timeout = 30
    return min(max(timeout, 1), 120)
