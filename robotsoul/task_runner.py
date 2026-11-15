"""Persistent, bounded semantic task loop above the checked robot runtime."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from math import isfinite
from pathlib import Path
from queue import Empty, Queue
import sqlite3
from threading import Condition, Event, RLock, Thread, Timer
import time
from typing import Any, Callable
from uuid import uuid4

from robotsoul.agent import AgentRejected, resolve_slash_command
from robotsoul.models import (
    AgentDecision, AgentIntent, AgentPlan, AuditRecord, Command, ConversationContext,
    Evaluation, Feedback, FeedbackStatus, InvocationContext, InvocationSource,
    Observation, Task, TaskContext, TaskResult, TaskStatus, utc_now,
)
from robotsoul.task_rules import RuleEngine, RuleError
from robotsoul.task_store import TERMINAL, TaskStore, encode


class TaskCancelled(RuntimeError):
    pass


class TaskBudgetExceeded(RuntimeError):
    pass


class TaskOutcomeUnknown(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskLimits:
    decisions: int = 8
    commands: int = 32
    duration_seconds: float = 300

    def __post_init__(self) -> None:
        if not 1 <= self.decisions <= 64 or not 1 <= self.commands <= 1024 or not 0 < self.duration_seconds <= 3600:
            raise ValueError("Invalid task budgets")


@dataclass
class _Entry:
    task: Task
    conversation: ConversationContext
    invocation: InvocationContext
    feedback: list[Feedback] = field(default_factory=list)
    history: list[AuditRecord] = field(default_factory=list)
    evaluation: Evaluation | None = None
    reply: str = ""
    decisions: int = 0
    commands: int = 0
    intent: AgentIntent | AgentPlan | None = None
    resume_input: str = ""
    cancelled: Event = field(default_factory=Event)
    controls: deque[dict[str, Any]] = field(default_factory=deque)
    seen_events: set[str] = field(default_factory=set)
    direct: bool = False
    queued: bool = False
    timeline_events: int = 0
    started: float = 0
    runtime_events: list[Observation] = field(default_factory=list)
    step_count: int = 0
    last_step_feedback: tuple[Feedback, ...] = ()
    recovered_commands: set[str] = field(default_factory=set)
    action_attempts: dict[str, int] = field(default_factory=dict)
    failed_actions: set[str] = field(default_factory=set)
    goal_kind: str = "general"
    expected_evaluator: str | None = None
    request_intent: str | None = None
    user_recorded: bool = False
    deadline_expired: Event = field(default_factory=Event)


class TaskRunner:
    """One robot executes one task at a time; cancellation never takes its queue lock."""

    def __init__(
        self, runtime: Any, *, store_path: str | Path = ":memory:", clock: Any = None,
        limits: TaskLimits | None = None,
    ) -> None:
        self.runtime = runtime
        self.limits = limits or TaskLimits()
        self.clock = clock
        self.store = TaskStore(store_path)
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._queue: Queue[str | None] = Queue()
        self._closed = False
        self._active: str | None = None
        self._activation = 0
        self._storage_failed = False
        self._entries: dict[str, _Entry] = {}
        self._evaluators: dict[str, Callable[[TaskContext, tuple[str, ...]], Evaluation]] = {
            "skill_completion": self._evaluate_skills,
        }
        self._evaluator_descriptions = {
            "skill_completion": "Verify all dispatched skills completed; this evaluator does not infer an unobserved physical goal.",
        }
        for result, conversation, invocation, meta in self.store.load_all():
            self._entries[result.task.task_id] = _Entry(
                result.task, conversation, invocation, list(result.feedback), list(result.history),
                result.evaluation, result.reply, meta.get("decisions", 0), meta.get("commands", 0),
                timeline_events=meta.get("timeline_events", 0), started=self._now(),
                step_count=meta.get("step_count", 0),
                recovered_commands=set(meta.get("recovered_commands", [])),
                action_attempts=meta.get("action_attempts", {}), failed_actions=set(meta.get("failed_actions", [])),
                goal_kind=meta.get("goal_kind", "general"), expected_evaluator=meta.get("expected_evaluator"),
                request_intent=meta.get("request_intent"), user_recorded=meta.get("user_recorded", False),
            )
            if meta.get("deadline_expired", False):
                self._entries[result.task.task_id].deadline_expired.set()
        self._worker = Thread(target=self._work, name="robot-task-runner", daemon=True)
        self._worker.start()

    def register_evaluator(self, name: str, evaluator: Callable[[TaskContext, tuple[str, ...]], Evaluation], *, description: str = "") -> None:
        if not isinstance(name, str) or not name or not callable(evaluator):
            raise ValueError("A result evaluator needs a name and callable")
        with self._lock:
            if name in self._evaluators:
                raise ValueError("Result evaluator already registered")
            self._evaluators[name] = evaluator
            self._evaluator_descriptions[name] = description or (evaluator.__doc__ or name).strip()

    def submit_task(
        self, text: str, *, conversation: ConversationContext | None = None,
        invocation_context: InvocationContext | None = None, task_id: str | None = None,
        trace_id: str | None = None, intent: AgentIntent | AgentPlan | None = None,
        goal_kind: str = "general", expected_evaluator: str | None = None,
    ) -> str:
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError("Task text must contain 1 to 10000 characters")
        conversation = conversation or self.default_conversation()
        if conversation.robot_id != self.runtime.profile.robot_id:
            raise ValueError("Conversation belongs to another robot")
        task = Task(task_id or str(uuid4()), trace_id or str(uuid4()), conversation.robot_id, text)
        if not isinstance(task.task_id, str) or not task.task_id.strip() or not isinstance(task.trace_id, str) or not task.trace_id.strip():
            raise ValueError("Task and trace IDs must be non-empty")
        invocation = invocation_context or InvocationContext(InvocationSource.AGENT)
        if goal_kind not in {"general", "physical"}:
            raise ValueError("Task goal_kind must be general or physical")
        if expected_evaluator is not None and expected_evaluator not in self._evaluators:
            raise ValueError("Expected evaluator is not registered")
        if expected_evaluator is not None:
            goal_kind = "physical"
        entry = _Entry(task, conversation, invocation, intent=intent,
                       direct=intent is not None or text.strip().startswith("/"), started=self._now(),
                       goal_kind=goal_kind, expected_evaluator=expected_evaluator,
                       request_intent=encode(intent) if intent is not None else None)
        with self._changed:
            if self._closed:
                raise RuntimeError("Task runner is closed")
            if self._storage_failed:
                raise RuntimeError("Task storage is unavailable; action dispatch is disabled")
            if self.store.create(self._snapshot(entry), conversation, invocation, self._meta(entry)):
                self._entries[task.task_id] = entry
                self._record(entry, "task.received", {"input_chars": len(text), "channel": conversation.channel})
                self._trace(entry, f"收到指令：{text.strip()}")
                consume_preempted = getattr(self.runtime, "_consume_preempted", None)
                if callable(consume_preempted) and consume_preempted(task.task_id):
                    self._finish(entry, TaskStatus.FAILED, "Voice command was replaced")
                    return task.task_id
                self._enqueue(entry)
            return task.task_id

    def default_conversation(self) -> ConversationContext:
        return ConversationContext(self.runtime.profile.robot_id, "cli", "local", "default")

    def _now(self) -> float:
        if self.clock is None:
            return time.monotonic()
        return float(self.clock.monotonic() if hasattr(self.clock, "monotonic") else self.clock())

    def _entry(self, task_id: str, conversation: ConversationContext | None = None) -> _Entry:
        entry = self._entries.get(task_id)
        if entry is None:
            raise KeyError("Unknown task")
        if conversation is not None and entry.conversation != conversation:
            raise PermissionError("Task belongs to another conversation")
        return entry

    def get_task(self, task_id: str, *, conversation: ConversationContext | None = None) -> TaskResult:
        with self._lock:
            return self._snapshot(self._entry(task_id, conversation))

    def pending_input(self, conversation: ConversationContext) -> str | None:
        with self._lock:
            for entry in reversed(tuple(self._entries.values())):
                if entry.conversation == conversation and entry.task.status == TaskStatus.AWAITING_INPUT:
                    return entry.task.task_id
        return None

    def wait_task(self, task_id: str, timeout: float | None = None) -> TaskResult:
        until = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while True:
                entry = self._entry(task_id)
                if entry.task.status in TERMINAL or entry.task.status == TaskStatus.AWAITING_INPUT:
                    return self._snapshot(entry)
                remaining = None if until is None else until - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("Task has not finished")
                self._changed.wait(remaining)

    def resume_task(
        self, task_id: str, text: str | None = None, *, event: dict[str, Any] | None = None,
        conversation: ConversationContext, event_id: str | None = None,
    ) -> str:
        with self._changed:
            if self._closed:
                raise RuntimeError("Task runner is closed")
            entry = self._entry(task_id, conversation)
            if event and event.get("timeline_action"):
                action = event["timeline_action"]
                if action not in {"pause", "resume", "seek", "end"}:
                    raise ValueError("Unknown timeline control")
                if action == "seek":
                    value = event.get("position_ms")
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or not 0 <= value <= 300000:
                        raise ValueError("Invalid seek position")
                identity = event_id or str(event.get("event_id") or uuid4())
                if self.store.has_resume_event(task_id, identity, event):
                    return task_id
                if entry.task.status in TERMINAL:
                    raise ValueError("Task is already finished")
                if self.store.resume_event(task_id, identity, event):
                    entry.controls.append({"action": action, "position_ms": event.get("position_ms")})
                self._changed.notify_all()
                return task_id
            payload = {"text": text, "event": event}
            identity = event_id or (str(event.get("event_id")) if event and event.get("event_id") else str(uuid4()))
            # Deduplicate before checking state, so a completed repeat is harmless.
            if self.store.has_resume_event(task_id, identity, payload):
                return task_id
            if event is not None and text is None:
                if entry.task.status != TaskStatus.WAITING_EVENT:
                    raise ValueError("Only a task waiting for an event can receive runtime events")
                event_type = event.get("event_type")
                event_payload = event.get("payload", {})
                if not isinstance(event_type, str) or not event_type or len(event_type) > 128 or not isinstance(event_payload, dict):
                    raise ValueError("Runtime events need an event_type and object payload")
                self.store.resume_event(task_id, identity, payload)
                now = utc_now()
                entry.runtime_events.append(Observation(identity, entry.task.robot_id, "runtime", event_type,
                                                        now, now, dict(event_payload), "task_resume", 300000))
                self._record(entry, "task.runtime_event", {"event_id": identity, "event_type": event_type,
                                                           "payload": event_payload})
                return task_id
            if entry.task.status != TaskStatus.AWAITING_INPUT:
                raise ValueError("Only a task awaiting input can be resumed")
            if not isinstance(text, str) or not text.strip() or len(text) > 10000:
                raise ValueError("A clarification answer must contain 1 to 10000 characters")
            self.store.resume_event(task_id, identity, payload)
            entry.resume_input = text
            entry.reply = ""
            self._transition(entry, TaskStatus.RECEIVED)
            self._record(entry, "task.resumed", {"event_id": identity})
            self._enqueue(entry)
            return task_id

    def cancel_task(self, task_id: str, *, conversation: ConversationContext | None = None) -> TaskResult:
        interrupt_decision = False
        stop_command = False
        with self._changed:
            entry = self._entry(task_id, conversation)
            if entry.task.status in TERMINAL:
                return self._snapshot(entry)
            entry.cancelled.set()
            if self._active != task_id or entry.task.status in {
                TaskStatus.RECEIVED, TaskStatus.AWAITING_AGENT, TaskStatus.WAITING_EVENT, TaskStatus.AWAITING_INPUT,
            }:
                interrupt_decision = self._active == task_id and entry.task.status == TaskStatus.AWAITING_AGENT
                self._finish(entry, TaskStatus.CANCELLED, "任务已取消。")
            else:
                stop_command = True
                self._transition(entry, TaskStatus.CANCEL_REQUESTED)
        # No language-process lock or wait. The adapter owns stop confirmation.
        if interrupt_decision:
            interrupt = getattr(self.runtime, "interrupt_task_decision", None)
            if callable(interrupt):
                interrupt(task_id)
        if stop_command:
            cancel = getattr(self.runtime, "cancel_active_command", None)
            if callable(cancel):
                cancel(task_id)
        return self.get_task(task_id)

    def _enqueue(self, entry: _Entry) -> None:
        if not entry.queued:
            entry.queued = True
            self._queue.put_nowait(entry.task.task_id)
            self._changed.notify_all()

    def _work(self) -> None:
        while True:
            try:
                task_id = self._queue.get(timeout=0.25)
            except Empty:
                if self._closed:
                    return
                self.flush_outbox()
                continue
            if task_id is None:
                self._queue.task_done()
                return
            with self._changed:
                entry = self._entry(task_id)
                entry.queued = False
                if entry.task.status in TERMINAL:
                    self._queue.task_done()
                    continue
                self._active = task_id
                self._activation += 1
                activation = self._activation
            deadline_timer = None
            try:
                deadline_timer = self._start_deadline(entry, activation)
                self._run(entry)
            except TaskCancelled:
                with self._changed:
                    try:
                        if self.store.unknown_command_ids(entry.task.task_id):
                            self._safe_finish(entry, TaskStatus.INTERRUPTED, "取消已请求；动作结果未知，需要检查设备状态。")
                        else:
                            self._safe_finish(entry, TaskStatus.CANCELLED, "任务已取消。")
                    except Exception as exc:
                        self._storage_failure(entry, exc)
            except (AgentRejected, TaskBudgetExceeded, RuleError, ValueError) as exc:
                with self._changed:
                    self._safe_finish(entry, TaskStatus.FAILED, str(exc))
            except TaskOutcomeUnknown as exc:
                with self._changed:
                    self._safe_finish(entry, TaskStatus.INTERRUPTED, str(exc))
            except Exception as exc:
                with self._changed:
                    self._safe_finish(entry, TaskStatus.FAILED, f"Task execution failed: {exc}")
            finally:
                if deadline_timer is not None:
                    deadline_timer.cancel()
                with self._changed:
                    self._active = None
                    try:
                        self._persist(entry)
                    except Exception as exc:
                        self._storage_failure(entry, exc)
                    self._changed.notify_all()
                self._queue.task_done()
                self.flush_outbox()

    def _start_deadline(self, entry: _Entry, activation: int) -> Timer | None:
        if self.clock is not None:
            return None
        remaining = self.limits.duration_seconds - (self._now() - entry.started)
        timer = Timer(max(0, remaining), self._deadline_reached, args=(entry, activation))
        timer.daemon = True
        timer.start()
        return timer

    def _deadline_reached(self, entry: _Entry, activation: int | None = None, *, interrupt: bool = True) -> None:
        with self._changed:
            if activation is not None and (self._active != entry.task.task_id or self._activation != activation):
                return
            if entry.task.status in TERMINAL or entry.task.status == TaskStatus.AWAITING_INPUT or entry.cancelled.is_set():
                return
            entry.deadline_expired.set()
            # The same local stop signal wakes waits and coding dispatchers;
            # deadline_expired distinguishes this from a user's cancellation.
            entry.cancelled.set()
            entry.evaluation = None
            entry.history.append(AuditRecord(utc_now(), "task.deadline_expired", entry.task.task_id,
                                             entry.task.trace_id, {"duration_seconds": self.limits.duration_seconds}))
            self._safe_finish(entry, TaskStatus.FAILED, "Task deadline expired")
        if interrupt:
            # Persist and wake callers before asking external components to stop.
            for method in ("interrupt_task_decision", "cancel_active_command"):
                stop = getattr(self.runtime, method, None)
                if callable(stop):
                    try:
                        stop(entry.task.task_id)
                    except Exception as exc:
                        with self._changed:
                            try:
                                self._record(entry, "task.deadline_stop_failed", {"method": method, "error_type": type(exc).__name__})
                            except Exception as persist_error:
                                self._storage_failure(entry, persist_error)

    def _deadline_result(self, entry: _Entry, unknown_commands: frozenset[str] | None = None) -> tuple[TaskStatus, str]:
        if unknown_commands is None:
            unknown_commands = self.store.unknown_command_ids(entry.task.task_id)
        if unknown_commands:
            return TaskStatus.INTERRUPTED, "Task deadline expired; command outcome is unknown and must be checked before another action"
        return TaskStatus.FAILED, "Task deadline expired"

    def _safe_finish(self, entry: _Entry, status: TaskStatus, reply: str) -> None:
        try:
            unknown_commands = self.store.unknown_command_ids(entry.task.task_id)
            if entry.deadline_expired.is_set():
                status, reply = self._deadline_result(entry, unknown_commands)
            elif entry.cancelled.is_set():
                status = TaskStatus.INTERRUPTED if unknown_commands else TaskStatus.CANCELLED
                reply = "取消已请求；动作结果未知，需要检查设备状态。" if unknown_commands else "任务已取消。"
            elif status == TaskStatus.FAILED and unknown_commands:
                status = TaskStatus.INTERRUPTED
                reply = f"Command outcome is unknown; no further actions were dispatched. {reply}"
            self._finish(entry, status, reply)
        except Exception as exc:
            self._storage_failure(entry, exc)

    def _storage_failure(self, entry: _Entry, error: Exception) -> None:
        self._storage_failed = True
        entry.cancelled.set()
        entry.task.status = TaskStatus.INTERRUPTED
        entry.task.error = entry.reply = f"Task storage failed; further actions are disabled: {error}"
        entry.task.updated_at = utc_now()
        self._changed.notify_all()

    def _run(self, entry: _Entry) -> None:
        host = _RuleHost(self, entry)
        host.check_running()
        self._transition(entry, TaskStatus.CONTEXTUALIZING)
        observations = self._observations(entry)
        if entry.intent is not None:
            self._run_fixed(entry, entry.intent, host)
            return
        if entry.decisions == 0:
            direct = resolve_slash_command(entry.task.user_input, self.runtime.capabilities)
            if isinstance(direct, str):
                raise AgentRejected(direct)
            if direct is not None:
                entry.direct = True
                self._trace(entry, "使用直接指令")
                self._run_fixed(entry, direct, host)
                return
        agent = self.runtime.agent
        decide = getattr(agent, "decide", None)
        if not callable(decide):
            with self._changed:
                self._transition(entry, TaskStatus.AWAITING_AGENT)
            self._trace(entry, "等待语言决策")
            try:
                intent = agent.plan(entry.task.user_input, self.runtime.capabilities, observations)
            except TimeoutError as exc:
                raise AgentRejected("Agent request timed out") from exc
            except AgentRejected:
                raise
            except Exception as exc:
                raise AgentRejected(f"Agent request failed: {exc}") from exc
            host.check_running()
            if intent is None:
                raise AgentRejected("No supported skill matched the request")
            self._run_fixed(entry, intent, host)
            return
        while True:
            host.check_running()
            if entry.decisions >= self.limits.decisions:
                raise TaskBudgetExceeded("Task decision budget exhausted")
            with self._changed:
                self._transition(entry, TaskStatus.AWAITING_AGENT)
            context = self._context(entry)
            self._trace(entry, "等待语言决策")
            try:
                decision = decide(context, self.runtime.capabilities)
            except TimeoutError as exc:
                raise AgentRejected("Agent request timed out") from exc
            host.check_running()
            if isinstance(decision, (AgentIntent, AgentPlan)):
                self._run_fixed(entry, decision, host)
                return
            if not isinstance(decision, AgentDecision):
                raise ValueError("Agent returned an invalid task decision")
            entry.decisions += 1
            identity = decision.decision_id or f"{entry.task.task_id}:decision:{entry.decisions}"
            if not self.store.decision(entry.task.task_id, identity, decision):
                raise ValueError("Agent repeated an already consumed decision")
            self._record(entry, "agent.decision", {"kind": decision.kind, "decision_id": identity})
            if decision.kind == "act":
                if not decision.steps or len(decision.steps) > 8:
                    raise ValueError("An action decision must contain 1 to 8 steps")
                for step in decision.steps:
                    host.validate_intent(step)
                host.reserve_rule(len(decision.steps), 0)
                for step in decision.steps:
                    if not host.execute_intent(step):
                        # The next decision sees the actual failure, not a
                        # fabricated completion or an automatically replayed action.
                        break
            elif decision.kind == "run_rule":
                RuleEngine(host).execute(decision.rule)
            elif decision.kind == "wait":
                spec = {"template": "wait_event", **(decision.wait or {})}
                RuleEngine(host).execute(spec)
            elif decision.kind == "clarify":
                if not decision.reply.strip():
                    raise ValueError("Clarification needs a question")
                with self._changed:
                    entry.reply = decision.reply
                    entry.evaluation = None
                    self._transition(entry, TaskStatus.AWAITING_INPUT)
                return
            elif decision.kind == "complete":
                self._complete(entry, decision)
                return
            elif decision.kind == "coding_task":
                if not isinstance(decision.coding_task, str) or not decision.coding_task.strip():
                    raise ValueError("Coding handoff requires a task description")
                dispatch = getattr(self.runtime, "dispatch_coding_task", None)
                if not callable(dispatch):
                    raise AgentRejected("Coding dispatcher is unavailable")
                host.check_running()
                self._record(entry, "task.coding_dispatch_started", {"decision_id": identity})
                host.check_running()
                reply = dispatch(replace(entry.task), decision, entry.cancelled)
                host.check_running()
                if not isinstance(reply, str) or not reply.strip():
                    raise ValueError("Coding dispatcher returned no result")
                if decision.legacy_coding:
                    self._finish(entry, TaskStatus.FAILED, decision.reply + reply)
                else:
                    self._finish(entry, TaskStatus.COMPLETED, reply)
                return
            elif decision.kind == "reject":
                raise AgentRejected(decision.reply or "Task could not be executed")
            else:
                raise ValueError("Unknown agent decision kind")
            entry.resume_input = ""

    def _run_fixed(self, entry: _Entry, intent: AgentIntent | AgentPlan, host: "_RuleHost") -> None:
        from robotsoul.runtime import _coerce_steps, _collapse_repeated_motion, _PLAN_MARGIN_MS, step_allowance_ms
        steps, error = _coerce_steps(intent)
        if error:
            raise ValueError(error)
        requested = len(steps)
        steps = _collapse_repeated_motion(steps, self.runtime.capabilities)
        if len(steps) != requested:
            self._record(entry, "plan.collapsed", {"from": requested, "to": len(steps),
                                                  "skills": [step.skill for step in steps]})
        if len(steps) == 1:
            self._record(entry, "agent.intent", {"skill": steps[0].skill,
                         "parameter_names": sorted(steps[0].parameters) if isinstance(steps[0].parameters, dict) else []})
            self._trace(entry, f"已选择技能 {steps[0].skill}")
        else:
            self._record(entry, "agent.plan", {"steps": [{"skill": step.skill,
                         "parameter_names": sorted(step.parameters) if isinstance(step.parameters, dict) else []}
                         for step in steps], "rationale": intent.rationale if isinstance(intent, AgentPlan) else ""})
            self._trace(entry, f"已编排计划 步数={len(steps)}")
        for step in steps:
            host.validate_intent(step)
        host.reserve_rule(len(steps), 0)
        allowances = [step_allowance_ms(self.runtime.capabilities[step.skill], step.parameters) for step in steps]
        self._record(entry, "plan.scheduled", {"steps": [step.skill for step in steps], "allowance_ms": allowances,
                                              "margin_ms": _PLAN_MARGIN_MS})
        for step in steps:
            if not host.execute_intent(step):
                with self._changed:
                    self._finish(entry, TaskStatus.FAILED, entry.task.error or "Command execution failed")
                return
            self._record(entry, "plan.step_completed", {"skill": step.skill})
        refs = tuple(item.feedback_id for item in entry.feedback if item.status == FeedbackStatus.SUCCEEDED)
        self._complete(entry, AgentDecision("complete", evidence_refs=refs))

    def _complete(self, entry: _Entry, decision: AgentDecision) -> None:
        if entry.expected_evaluator is not None and decision.evaluator != entry.expected_evaluator:
            raise ValueError("Completion must use the host-required result evaluator")
        if entry.goal_kind == "physical" and not self.store.command_ids(entry.task.task_id):
            raise ValueError("Physical goal completion requires observed command evidence")
        if self.store.command_ids(entry.task.task_id):
            evaluator = self._evaluators.get(decision.evaluator)
            if evaluator is None:
                raise ValueError("Result evaluator is not registered")
            refs = decision.evidence_refs
            if not refs:
                raise ValueError("Physical task completion requires current-task evidence references")
            context = self._context(entry)
            self._select_evidence(context, refs)
            evaluation = evaluator(context, refs)
            if not isinstance(evaluation, Evaluation) or evaluation.task_id != entry.task.task_id or evaluation.trace_id != entry.task.trace_id:
                raise ValueError("Evaluator returned an unrelated result")
            if not evaluation.success:
                self._finish(entry, TaskStatus.FAILED, evaluation.conclusion)
                return
            entry.evaluation = evaluation
            # Domain evaluators own the factual conclusion. Generic model text
            # cannot overwrite a verified physical result with a new claim.
            reply = evaluation.conclusion
        else:
            if decision.evidence_refs:
                raise ValueError("Answer references evidence this task never produced")
            reply = decision.reply.strip()
            if not reply:
                raise ValueError("A conversational completion needs a reply")
            entry.evaluation = Evaluation(entry.task.task_id, entry.task.trace_id, True, reply, (), 1.0)
        with self._changed:
            self._finish(entry, TaskStatus.COMPLETED, reply)

    def _evaluate_skills(self, context: TaskContext, refs: tuple[str, ...]) -> Evaluation:
        selected = self._select_evidence(context, refs)
        expected = self.store.command_ids(context.task.task_id) - self._entry(context.task.task_id).recovered_commands
        if {item.command_id for item in selected} != expected:
            raise ValueError("Skill completion must cover every command, including unresolved earlier failures")
        nominal = any(item.evidence.get("completion") == "nominal" for item in selected)
        conclusion = (
            "Plan completed; body completion is estimated where a step used a nominal duration"
            if len(selected) > 1 and nominal else
            "Nominal duration elapsed; body completion is estimated" if nominal else
            "Plan completed" if len(selected) > 1 else selected[0].message or "Skill completed with observable evidence"
        )
        return Evaluation(context.task.task_id, context.task.trace_id, True, conclusion,
                          tuple(item.feedback_id for item in selected), 1.0)

    def _select_evidence(self, context: TaskContext, refs: tuple[str, ...]) -> tuple[Feedback, ...]:
        valid_commands = self.store.command_ids(context.task.task_id)
        selected = []
        for ref in refs:
            item = next((f for f in reversed(context.feedback) if ref in {f.feedback_id, f.command_id}
                         and f.status == FeedbackStatus.SUCCEEDED), None)
            if item is None or item.task_id != context.task.task_id or item.trace_id != context.task.trace_id or item.command_id not in valid_commands or item.evidence.get("condition_met") is not True:
                raise ValueError("Completion evidence is missing, stale, or belongs to another task")
            if item not in selected:
                selected.append(item)
        if not selected:
            raise ValueError("No verified completion evidence")
        return tuple(selected)

    def _observations(self, entry: _Entry | None = None) -> tuple[Observation, ...]:
        collect = getattr(self.runtime, "collect_task_observations", None)
        observations = tuple(collect()) if callable(collect) else ()
        drain = getattr(self.runtime, "drain_task_observation_records", None)
        if entry is not None and callable(drain):
            for event_type, details in drain():
                self._record(entry, event_type, details)
        return observations

    def _context(self, entry: _Entry) -> TaskContext:
        events = tuple(event for event in entry.runtime_events if event.is_fresh)
        return TaskContext(replace(entry.task), entry.conversation, self._observations(entry) + events, tuple(entry.feedback),
                           entry.decisions, entry.resume_input, tuple(entry.history),
                           tuple({"name": name, "description": self._evaluator_descriptions[name]}
                                 for name in self._evaluators), entry.goal_kind, entry.expected_evaluator)

    def _snapshot(self, entry: _Entry) -> TaskResult:
        return TaskResult(replace(entry.task), tuple(entry.feedback), entry.evaluation, tuple(entry.history), entry.reply)

    @staticmethod
    def _meta(entry: _Entry) -> dict[str, Any]:
        return {"decisions": entry.decisions, "commands": entry.commands,
                "timeline_events": entry.timeline_events, "step_count": entry.step_count,
                "recovered_commands": sorted(entry.recovered_commands), "action_attempts": entry.action_attempts,
                "failed_actions": sorted(entry.failed_actions), "goal_kind": entry.goal_kind,
                "expected_evaluator": entry.expected_evaluator, "request_intent": entry.request_intent,
                "user_recorded": entry.user_recorded, "deadline_expired": entry.deadline_expired.is_set()}

    @staticmethod
    def _trace(entry: _Entry, message: str) -> None:
        from robotsoul.runtime import _trace
        _trace("Info", entry.task.task_id, message)

    def _persist(self, entry: _Entry, *, note: tuple[str, dict[str, Any]] | None = None) -> None:
        with self._changed:
            self.store.save(self._snapshot(entry), self._meta(entry), note=note)
            self._changed.notify_all()

    def _transition(self, entry: _Entry, status: TaskStatus) -> None:
        with self._changed:
            if entry.deadline_expired.is_set():
                raise TaskBudgetExceeded("Task deadline expired")
            if entry.cancelled.is_set() and status not in {TaskStatus.CANCEL_REQUESTED, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED}:
                raise TaskCancelled()
            entry.task.status = status
            entry.task.updated_at = utc_now()
            self._record(entry, "task.status", {"status": status.value})

    def _record(self, entry: _Entry, event_type: str, details: dict[str, Any]) -> None:
        with self._changed:
            entry.history.append(AuditRecord(utc_now(), event_type, entry.task.task_id, entry.task.trace_id, details))
            self._persist(entry)

    def _finish(self, entry: _Entry, status: TaskStatus, reply: str) -> None:
        with self._changed:
            if entry.task.status in TERMINAL:
                return
            if entry.deadline_expired.is_set():
                status, reply = self._deadline_result(entry)
                entry.evaluation = None
            entry.reply = reply
            entry.task.error = reply if status in {TaskStatus.FAILED, TaskStatus.INTERRUPTED} else None
            entry.task.status = status
            entry.task.updated_at = utc_now()
            if status == TaskStatus.FAILED:
                self._trace(entry, f"指令未执行 原因={reply}")
            entry.history.append(AuditRecord(utc_now(), "task.status", entry.task.task_id, entry.task.trace_id,
                                             {"status": status.value}))
            self._persist(entry, note=self._note_payload(entry, f"{entry.task.task_id}:result:{status.value}", reply))

    def _note_payload(self, entry: _Entry, event_id: str, text: str) -> tuple[str, dict[str, Any]] | None:
        if not callable(getattr(self.runtime.agent, "record_note", None)):
            return None
        user_text = entry.task.user_input if entry.direct and not entry.user_recorded else None
        if user_text is not None:
            entry.user_recorded = True
        return event_id, {
            "text": text, "user_text": user_text,
            "conversation": entry.conversation.__dict__,
        }

    def flush_outbox(self) -> None:
        deliver = getattr(self.runtime.agent, "record_note", None)
        if not callable(deliver):
            return
        try:
            notes = self.store.pending_notes()
        except sqlite3.Error:
            return
        for event_id, _task_id, payload in notes:
            try:
                deliver(ConversationContext(**payload["conversation"]), event_id, payload["text"], user_text=payload.get("user_text"))
                self.store.delivered(event_id)
            except Exception:
                # Delivery is independent of command execution; retry only this note.
                continue

    def close(self) -> None:
        with self._changed:
            if self._closed:
                return
            self._closed = True
            pending = [entry.task.task_id for entry in self._entries.values() if entry.task.status not in TERMINAL]
        for task_id in pending:
            self.cancel_task(task_id)
        self._queue.put_nowait(None)
        self._worker.join(timeout=5)
        if self._worker.is_alive():
            # Keep the database alive until the provider/adapter returns. A daemon
            # worker may still need to persist the cancellation acknowledgement.
            return
        self.flush_outbox()
        self.store.close()
class _RuleHost:
    def __init__(self, runner: TaskRunner, entry: _Entry) -> None:
        self.runner = runner
        self.entry = entry
        self.capabilities = runner.runtime.capabilities

    def now(self) -> float:
        return self.runner._now()

    def check_running(self) -> None:
        if self.entry.deadline_expired.is_set():
            raise TaskBudgetExceeded("Task deadline expired")
        if self.entry.cancelled.is_set():
            raise TaskCancelled()
        if self.runner._storage_failed:
            raise RuntimeError("Task storage is unavailable; action dispatch is disabled")
        if self.now() - self.entry.started >= self.runner.limits.duration_seconds:
            self.runner._deadline_reached(self.entry, interrupt=False)
            raise TaskBudgetExceeded("Task deadline expired")

    def wait(self, seconds: float) -> None:
        self.check_running()
        clock = self.runner.clock
        if clock is not None and callable(getattr(clock, "sleep", None)):
            clock.sleep(seconds)
        else:
            self.entry.cancelled.wait(seconds)
        self.check_running()

    def record(self, kind: str, payload: dict[str, Any]) -> None:
        self.runner._record(self.entry, kind, payload)

    def validate_intent(self, intent: AgentIntent) -> None:
        if not isinstance(intent, AgentIntent):
            raise RuleError("Invalid action step")
        authorize = getattr(self.runner.runtime, "_authorize_step", None)
        validate = getattr(self.runner.runtime, "_validate_intent", None)
        if callable(authorize):
            error = authorize(intent, self.entry.invocation, self.entry.invocation.operator_confirmed)
            if error:
                raise RuleError(error)
        elif callable(validate):
            error = validate(intent)
            if error:
                raise RuleError(error)
        elif intent.skill not in self.capabilities:
            raise RuleError("Unsupported skill")
        if not isinstance(intent.parameters, dict):
            raise RuleError("Invalid action parameters")

    def reserve_rule(self, commands: int, timeline_events: int) -> None:
        self.check_running()
        if self.entry.commands + commands > self.runner.limits.commands:
            raise TaskBudgetExceeded("Rule exceeds remaining task command budget")
        if self.entry.timeline_events + timeline_events > 256:
            raise TaskBudgetExceeded("Rule exceeds remaining task timeline budget")
        self.entry.timeline_events += timeline_events
        self.runner._persist(self.entry)

    def execute_intent(self, intent: AgentIntent, *, timeline: bool = False, cleanup: bool = False) -> bool:
        self.check_running()
        self.validate_intent(intent)
        entry = self.entry
        action_key = encode({"skill": intent.skill, "parameters": intent.parameters})
        if action_key in entry.failed_actions:
            if not self.capabilities[intent.skill].retry_safe or entry.action_attempts.get(action_key, 0) >= 3:
                raise RuleError("Retry requires a registered safe skill and permits at most three attempts")
        if not timeline and entry.commands >= self.runner.limits.commands:
            raise TaskBudgetExceeded("Task command budget exhausted")
        if not timeline:
            entry.commands += 1
        entry.step_count += 1
        entry.last_step_feedback = ()
        self.runner._transition(entry, TaskStatus.EXECUTING)
        dispatched: list[str] = []

        def on_dispatch(command: Command) -> None:
            with self.runner._changed:
                self.check_running()
                if command.task_id != entry.task.task_id or command.trace_id != entry.task.trace_id:
                    raise ValueError("Runtime tried to dispatch an unrelated command")
                self.runner.store.dispatch(command)
                dispatched.append(command.command_id)
                if action_key in entry.failed_actions:
                    entry.action_attempts[action_key] = entry.action_attempts.get(action_key, 1) + 1
                else:
                    entry.action_attempts[action_key] = 1

        outcome = self.runner.runtime.execute_task_step(
            replace(entry.task), intent, entry.invocation, prior_feedback=tuple(entry.feedback), on_dispatch=on_dispatch,
        )
        previous = {item.feedback_id for item in entry.feedback}
        fresh = tuple(item for item in outcome.feedback if item.feedback_id not in previous)
        if any(item.task_id != entry.task.task_id or item.trace_id != entry.task.trace_id
               or item.command_id not in dispatched for item in fresh):
            raise ValueError("Runtime returned feedback unrelated to this step")
        for command_id in dispatched:
            self.runner.store.finish_command(command_id, fresh)
        entry.feedback.extend(fresh)
        entry.last_step_feedback = fresh
        entry.history.extend(outcome.history)
        entry.evaluation = None
        if not entry.deadline_expired.is_set():
            entry.task.error = outcome.task.error
        terminal = next((item for item in reversed(fresh) if item.status in {
            FeedbackStatus.SUCCEEDED, FeedbackStatus.FAILED, FeedbackStatus.CANCELLED,
        }), None)
        if terminal is not None and terminal.status == FeedbackStatus.FAILED:
            entry.failed_actions.add(action_key)
        elif terminal is not None and terminal.status == FeedbackStatus.SUCCEEDED:
            entry.failed_actions.discard(action_key)
        note = self.runner._note_payload(entry, f"{entry.task.task_id}:step:{entry.step_count}", encode({
            "skill": intent.skill, "status": outcome.task.status.value, "feedback": fresh,
        }))
        self.runner._persist(entry, note=note)
        if terminal is not None and terminal.status == FeedbackStatus.CANCELLED:
            entry.cancelled.set()
            raise TaskCancelled()
        if self.runner.store.unknown_command_ids(entry.task.task_id) and not entry.cancelled.is_set():
            detail = f": {entry.task.error}" if entry.task.error else ""
            raise TaskOutcomeUnknown("Command outcome is unknown; check device state before submitting another action" + detail)
        self.check_running()
        if terminal is None:
            return False
        return terminal.status == FeedbackStatus.SUCCEEDED and terminal.evidence.get("condition_met") is True

    def last_step_command_ids(self) -> tuple[str, ...]:
        return tuple(item.command_id for item in self.entry.last_step_feedback if item.status == FeedbackStatus.FAILED)

    def resolve_retry_failures(self, command_ids: tuple[str, ...]) -> None:
        self.entry.recovered_commands.update(command_ids)
        self.runner._persist(self.entry)

    def last_evidence(self) -> dict[str, Any]:
        item = next((f for f in reversed(self.entry.last_step_feedback) if f.status in {
            FeedbackStatus.SUCCEEDED, FeedbackStatus.FAILED, FeedbackStatus.CANCELLED,
        }), None)
        return dict(item.evidence) if item else {}

    def last_failure_retryable(self) -> bool:
        item = next((f for f in reversed(self.entry.last_step_feedback) if f.status in {
            FeedbackStatus.SUCCEEDED, FeedbackStatus.FAILED, FeedbackStatus.CANCELLED,
        }), None)
        return bool(item and item.status == FeedbackStatus.FAILED and item.evidence.get("outcome_known") is True
                    and item.error_code not in {"timeout", "deadline_expired", "device_error", "cancel_failed"})

    def wait_event(self, spec: dict[str, Any]) -> bool:
        until = self.now() + spec["timeout_seconds"]
        self.runner._transition(self.entry, TaskStatus.WAITING_EVENT)
        while self.now() < until:
            self.check_running()
            for item in self.runner._context(self.entry).observations:
                if item.event_id in self.entry.seen_events or not item.is_fresh:
                    continue
                if item.event_type == spec["event_type"] and all(item.payload.get(k) == v for k, v in spec.get("equals", {}).items()):
                    self.entry.seen_events.add(item.event_id)
                    self.record("task.event_received", {"event_id": item.event_id, "event_type": item.event_type})
                    return True
            self.wait(min(0.02, max(0, until - self.now())))
        self.record("task.event_timeout", {"event_type": spec["event_type"]})
        return False

    def timeline_controls(self) -> tuple[dict[str, Any], ...]:
        with self.runner._lock:
            controls = tuple(self.entry.controls)
            self.entry.controls.clear()
            return controls
