import sqlite3
import tempfile
import time
import unittest
from collections import defaultdict, deque
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from robotsoul.models import (
    AgentDecision, AgentIntent, AgentPlan, Command, ConversationContext, Evaluation,
    Feedback, FeedbackStatus, InvocationContext, InvocationSource, Observation,
    CapabilitySpec, TaskStatus, TaskResult, utc_now,
)
from robotsoul.task_runner import TaskLimits, TaskRunner


class ScriptedAgent:
    def __init__(self, *decisions):
        self.decisions = deque(decisions)
        self.contexts = []

    def decide(self, context, capabilities):
        self.contexts.append(context)
        decision = self.decisions.popleft()
        return decision(context) if callable(decision) else decision


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.on_sleep = None

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds
        if self.on_sleep:
            self.on_sleep(self)


class CheckedRuntime:
    """Adapter boundary fake: log commands only after the durable dispatch hook."""

    def __init__(self, agent, *, clock=None):
        self.agent = agent
        self.profile = SimpleNamespace(robot_id="robot-1")
        self.capabilities = {
            "navigate": CapabilitySpec("navigate", {}),
            "inspect": CapabilitySpec("inspect", {}, retry_safe=True),
            "light": CapabilitySpec("light", {}),
        }
        self.responses = defaultdict(deque)
        self.commands = []
        self.observations = ()
        self.block_skill = None
        self.started = Event()
        self.release = Event()
        self.stop = Event()
        self.cancel_unknown = False
        self.forbidden = set()
        self.clock = clock
        self.after_step = None

    def _authorize_step(self, intent, _context, _confirmed):
        if intent.skill not in self.capabilities:
            return "Unsupported skill"
        if intent.skill in self.forbidden:
            return "Caller is not permitted for this capability"
        if not isinstance(intent.parameters, dict):
            return "Invalid parameters"
        return None

    def collect_task_observations(self):
        return self.observations

    def execute_task_step(self, task, intent, _invocation, *, prior_feedback, on_dispatch):
        command = Command(str(uuid4()), task.task_id, task.trace_id, task.robot_id,
                          intent.skill, dict(intent.parameters), utc_now() + timedelta(seconds=5))
        on_dispatch(command)
        self.commands.append((command, self.clock.monotonic() if self.clock else time.monotonic()))
        if intent.skill == self.block_skill:
            self.started.set()
            if not self.release.wait(2):
                raise TimeoutError("Test adapter did not receive a release")
        data = dict(self.responses[intent.skill].popleft()) if self.responses[intent.skill] else {}
        status = data.pop("status", FeedbackStatus.SUCCEEDED)
        if self.stop.is_set():
            if self.cancel_unknown:
                return TaskResult(replace(task, status=TaskStatus.FAILED), prior_feedback, None, ())
            status = FeedbackStatus.CANCELLED
        evidence = {"condition_met": status == FeedbackStatus.SUCCEEDED, "outcome_known": True, **data.pop("evidence", {})}
        feedback = Feedback(str(uuid4()), command.command_id, task.task_id, task.trace_id,
                            status, utc_now(), data.pop("message", intent.skill + " finished"),
                            data.pop("error_code", None), evidence)
        task.status = TaskStatus.COMPLETED if status == FeedbackStatus.SUCCEEDED else TaskStatus.FAILED
        if self.after_step:
            self.after_step(command)
        return TaskResult(task, prior_feedback + (feedback,), None, ())

    def cancel_active_command(self, _task_id):
        self.stop.set()
        self.release.set()


def completion(context):
    refs = tuple(item.feedback_id for item in context.feedback if item.status == FeedbackStatus.SUCCEEDED)
    return AgentDecision("complete", reply="model unverified claim", evidence_refs=refs)


def wait_status(runner, task_id, status):
    until = time.monotonic() + 2
    while time.monotonic() < until:
        result = runner.get_task(task_id)
        if result.task.status == status:
            return result
        time.sleep(0.005)
    raise AssertionError(f"Task did not reach {status}: {runner.get_task(task_id)}")


class TaskRunnerTests(unittest.TestCase):
    def runner(self, runtime, **kwargs):
        runner = TaskRunner(runtime, **kwargs)
        self.addCleanup(runner.close)
        return runner

    def test_replans_from_feedback_and_retains_fresh_observations(self):
        runtime = CheckedRuntime(ScriptedAgent(
            AgentDecision("act", (AgentIntent("navigate", {}),)),
            AgentDecision("act", (AgentIntent("inspect", {}),)), completion,
        ))
        now = utc_now()
        runtime.observations = (Observation("window-1", "robot-1", "camera", "window", now, now,
                                            {"state": "open"}, "simulator", 30000),)
        runner = self.runner(runtime)
        result = runner.wait_task(runner.submit_task("去阳台检查窗户"), 2)
        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual([item.skill for item, _ in runtime.commands], ["navigate", "inspect"])
        self.assertEqual([len(item.feedback) for item in runtime.agent.contexts], [0, 1, 2])
        self.assertTrue(all(item.observations == runtime.observations for item in runtime.agent.contexts))
        self.assertNotEqual(result.reply, "model unverified claim")

    def test_legacy_intent_or_plan_runs_once(self):
        for planned in (AgentIntent("navigate", {}), AgentPlan((AgentIntent("navigate", {}), AgentIntent("inspect", {})))):
            with self.subTest(planned=planned):
                runtime = CheckedRuntime(ScriptedAgent(planned))
                runner = self.runner(runtime)
                result = runner.wait_task(runner.submit_task("legacy"), 2)
                self.assertEqual(result.task.status, TaskStatus.COMPLETED)
                self.assertEqual(len(runtime.agent.contexts), 1)

    def test_pure_answer_has_independent_reply(self):
        runner = self.runner(CheckedRuntime(ScriptedAgent(AgentDecision("complete", reply="你好"))))
        result = runner.wait_task(runner.submit_task("问候"), 2)
        self.assertEqual(result.reply, "你好")
        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertFalse(result.feedback)

    def test_host_physical_goal_cannot_complete_without_action_evidence(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("complete", reply="窗户已检查完成")))
        runner = self.runner(runtime)
        result = runner.wait_task(runner.submit_task("检查窗户", goal_kind="physical"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertIn("command evidence", result.reply)
        self.assertFalse(result.evaluation)
        self.assertEqual(runtime.agent.contexts[0].goal_kind, "physical")

    def test_model_cannot_downgrade_host_required_domain_evaluator(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentIntent("inspect", {})))
        runner = self.runner(runtime)
        runner.register_evaluator("window_check", lambda _context, _refs: None)
        task_id = runner.submit_task("检查窗户", expected_evaluator="window_check")
        result = runner.wait_task(task_id, 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertIn("host-required", result.reply)
        self.assertEqual(runtime.agent.contexts[0].expected_evaluator, "window_check")

    def test_clarification_resumes_only_same_conversation_and_deduplicates(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("clarify", reply="检查哪扇窗？"),
                                             AgentDecision("complete", reply="收到")))
        runner = self.runner(runtime)
        conversation = runner.default_conversation()
        task_id = runner.submit_task("检查窗户", conversation=conversation)
        self.assertEqual(runner.wait_task(task_id, 2).task.status, TaskStatus.AWAITING_INPUT)
        self.assertEqual(runner.pending_input(conversation), task_id)
        foreign = replace(conversation, principal_id="other-user")
        with self.assertRaises(PermissionError):
            runner.resume_task(task_id, "阳台", conversation=foreign, event_id="answer")
        with self.assertRaises(ValueError):
            runner.resume_task(task_id, "", conversation=conversation, event_id="answer")
        runner.resume_task(task_id, "阳台", conversation=conversation, event_id="answer")
        self.assertEqual(runner.wait_task(task_id, 2).task.status, TaskStatus.COMPLETED)
        runner.resume_task(task_id, "阳台", conversation=conversation, event_id="answer")
        self.assertEqual(runtime.agent.contexts[-1].resume_input, "阳台")
        self.assertEqual(len(runtime.agent.contexts), 2)
        with self.assertRaises(ValueError):
            runner.resume_task(task_id, "厨房", conversation=conversation, event_id="answer")

    def test_session_key_separates_case_users_and_channels(self):
        original = ConversationContext("r", "voice", "USER", "one")
        self.assertNotEqual(original.session_key, replace(original, principal_id="user").session_key)
        self.assertNotEqual(original.session_key, replace(original, channel="dashboard").session_key)

    def test_cannot_complete_physical_action_with_missing_or_foreign_evidence(self):
        for refs in ((), ("foreign-command",)):
            runtime = CheckedRuntime(ScriptedAgent(
                AgentDecision("act", (AgentIntent("navigate", {}),)),
                AgentDecision("complete", reply="任务完成", evidence_refs=refs),
            ))
            runner = self.runner(runtime)
            result = runner.wait_task(runner.submit_task("移动"), 2)
            self.assertEqual(result.task.status, TaskStatus.FAILED)
            self.assertIn("evidence", result.task.error)

    def test_all_actions_authorized_before_first_dispatch(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("act", (
            AgentIntent("navigate", {}), AgentIntent("inspect", {}),
        ))))
        runtime.forbidden.add("inspect")
        runner = self.runner(runtime)
        self.assertEqual(runner.wait_task(runner.submit_task("检查"), 2).task.status, TaskStatus.FAILED)
        self.assertFalse(runtime.commands)

    def test_default_evaluator_cannot_hide_an_earlier_failed_command(self):
        runtime = CheckedRuntime(ScriptedAgent(
            AgentDecision("act", (AgentIntent("navigate", {}),)),
            AgentDecision("act", (AgentIntent("inspect", {}),)), completion,
        ))
        runtime.responses["navigate"].append({"status": FeedbackStatus.FAILED, "error_code": "blocked"})
        runner = self.runner(runtime)
        result = runner.wait_task(runner.submit_task("check after failure"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertIn("every command", result.reply)

    def test_registered_evaluators_are_visible_to_agent(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("complete", reply="hello")))
        runner = self.runner(runtime)
        runner.register_evaluator("window_check", lambda _context, _refs: None,
                                  description="Require navigation and observed open/closed window state")
        runner.wait_task(runner.submit_task("hello"), 2)
        descriptors = runtime.agent.contexts[0].result_evaluators
        self.assertIn({"name": "window_check", "description": "Require navigation and observed open/closed window state"}, descriptors)

    def test_non_retry_safe_failure_cannot_be_resent_by_agent(self):
        runtime = CheckedRuntime(ScriptedAgent(
            AgentDecision("act", (AgentIntent("navigate", {}),)),
            AgentDecision("act", (AgentIntent("navigate", {}),)),
        ))
        runtime.responses["navigate"].append({"status": FeedbackStatus.FAILED, "error_code": "blocked"})
        runner = self.runner(runtime)
        result = runner.wait_task(runner.submit_task("repeat failed movement"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertIn("Retry", result.reply)
        self.assertEqual(len(runtime.commands), 1)

    def test_cancel_before_durable_dispatch_hook_prevents_action(self):
        class PendingDispatchRuntime(CheckedRuntime):
            def execute_task_step(self, *args, **kwargs):
                self.started.set()
                self.release.wait(2)
                return super().execute_task_step(*args, **kwargs)

        runtime = PendingDispatchRuntime(ScriptedAgent(AgentIntent("navigate", {})))
        runner = self.runner(runtime)
        task_id = runner.submit_task("cancel before dispatch")
        self.assertTrue(runtime.started.wait(2))
        runner.cancel_task(task_id)
        result = runner.wait_task(task_id, 2)
        self.assertEqual(result.task.status, TaskStatus.CANCELLED)
        self.assertFalse(runtime.commands)
        self.assertFalse(runner.store.command_ids(task_id))

    def test_decision_and_command_budgets(self):
        runtime = CheckedRuntime(ScriptedAgent(*[AgentDecision("act", (AgentIntent("inspect", {}),))] * 3))
        runner = self.runner(runtime, limits=TaskLimits(decisions=2))
        result = runner.wait_task(runner.submit_task("loop"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertIn("decision budget", result.reply)
        self.assertEqual(len(runtime.commands), 2)
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("act", (AgentIntent("navigate", {}), AgentIntent("inspect", {})))))
        runner = self.runner(runtime, limits=TaskLimits(commands=1))
        result = runner.wait_task(runner.submit_task("too much"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertFalse(runtime.commands)

    def test_cancel_while_model_waits_is_immediate_and_discards_returned_action(self):
        entered, release = Event(), Event()

        def blocked(_context):
            entered.set()
            release.wait(2)
            return AgentDecision("act", (AgentIntent("navigate", {}),))

        runtime = CheckedRuntime(ScriptedAgent(blocked))
        runner = self.runner(runtime)
        self.addCleanup(release.set)
        task_id = runner.submit_task("slow model")
        self.assertTrue(entered.wait(2))
        self.assertEqual(runner.cancel_task(task_id).task.status, TaskStatus.CANCELLED)
        self.assertEqual(runner.wait_task(task_id, 0.1).task.status, TaskStatus.CANCELLED)
        release.set()
        runner._queue.join()
        self.assertFalse(runtime.commands)

    def test_model_cancel_interrupts_provider_outside_runner_lock(self):
        entered, release = Event(), Event()
        calls = []

        def blocked(_context):
            entered.set()
            release.wait(2)
            return AgentDecision("complete", reply="late")

        runtime = CheckedRuntime(ScriptedAgent(blocked, AgentDecision("complete", reply="next")))
        runner = self.runner(runtime)
        self.addCleanup(release.set)

        def interrupt(task_id):
            self.assertFalse(runner._lock._is_owned())
            calls.append(task_id)
            release.set()

        runtime.interrupt_task_decision = interrupt
        task_id = runner.submit_task("slow model")
        self.assertTrue(entered.wait(2))
        self.assertEqual(runner.cancel_task(task_id).task.status, TaskStatus.CANCELLED)
        self.assertEqual(calls, [task_id])
        next_id = runner.submit_task("next task")
        self.assertEqual(runner.wait_task(next_id, 2).reply, "next")

    def test_cancel_active_action_stops_next_step_and_preserves_feedback(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentPlan((AgentIntent("navigate", {}), AgentIntent("inspect", {})))))
        runtime.block_skill = "navigate"
        runner = self.runner(runtime)
        task_id = runner.submit_task("cancel action")
        self.assertTrue(runtime.started.wait(2))
        # The single step's mutable status is not exposed as the task's completion.
        self.assertEqual(runner.get_task(task_id).task.status, TaskStatus.EXECUTING)
        runner.cancel_task(task_id)
        result = runner.wait_task(task_id, 2)
        self.assertEqual(result.task.status, TaskStatus.CANCELLED)
        self.assertEqual([command.skill for command, _ in runtime.commands], ["navigate"])
        self.assertEqual(result.feedback[-1].status, FeedbackStatus.CANCELLED)

    def test_cancel_with_unknown_action_result_is_interrupted(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentIntent("navigate", {})))
        runtime.block_skill, runtime.cancel_unknown = "navigate", True
        runner = self.runner(runtime)
        task_id = runner.submit_task("unknown stop")
        self.assertTrue(runtime.started.wait(2))
        runner.cancel_task(task_id)
        self.assertEqual(runner.wait_task(task_id, 2).task.status, TaskStatus.INTERRUPTED)

    def test_unknown_physical_outcome_prevents_a_new_agent_retry(self):
        runtime = CheckedRuntime(ScriptedAgent(
            AgentDecision("act", (AgentIntent("navigate", {}),)),
            AgentDecision("act", (AgentIntent("navigate", {}),)),
        ))
        runtime.responses["navigate"].append({"status": FeedbackStatus.FAILED,
                                               "evidence": {"outcome_known": False}})
        runner = self.runner(runtime)
        result = runner.wait_task(runner.submit_task("unknown outcome"), 2)
        self.assertEqual(result.task.status, TaskStatus.INTERRUPTED)
        self.assertEqual(len(runtime.commands), 1)
        self.assertEqual(len(runtime.agent.contexts), 1)

    def test_runtime_event_resume_is_owned_and_idempotent(self):
        runtime = CheckedRuntime(ScriptedAgent(
            AgentDecision("wait", wait={"event_type": "audio.finished", "timeout_seconds": 2}),
            AgentDecision("complete", reply="播放完成"),
        ))
        runner = self.runner(runtime)
        task_id = runner.submit_task("等待播放结束")
        wait_status(runner, task_id, TaskStatus.WAITING_EVENT)
        event = {"event_type": "audio.finished", "payload": {"track": "test"}}
        conversation = runner.default_conversation()
        runner.resume_task(task_id, event=event, conversation=conversation, event_id="runtime-1")
        result = runner.wait_task(task_id, 2)
        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        runner.resume_task(task_id, event=event, conversation=conversation, event_id="runtime-1")
        self.assertEqual(sum(record.event_type == "task.event_received" for record in result.history), 1)
        self.assertEqual(runtime.agent.contexts[-1].observations[-1].event_id, "runtime-1")

    def test_waiting_event_can_cancel_without_model(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("wait", wait={"event_type": "never", "timeout_seconds": 30})))
        runner = self.runner(runtime)
        task_id = runner.submit_task("wait")
        wait_status(runner, task_id, TaskStatus.WAITING_EVENT)
        self.assertEqual(runner.cancel_task(task_id).task.status, TaskStatus.CANCELLED)
        self.assertEqual(len(runtime.agent.contexts), 1)

    def test_dispatch_storage_failure_prevents_adapter_and_keeps_worker_alive(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentIntent("navigate", {})))
        runner = self.runner(runtime)
        with patch.object(runner.store, "dispatch", side_effect=sqlite3.OperationalError("disk full")):
            result = runner.wait_task(runner.submit_task("move"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertIn("disk full", result.reply)
        self.assertFalse(runtime.commands)
        self.assertTrue(runner._worker.is_alive())

    def test_state_storage_failure_fails_closed_instead_of_killing_worker(self):
        entered, release = Event(), Event()

        def blocked(_context):
            entered.set()
            release.wait(2)
            return AgentDecision("act", (AgentIntent("navigate", {}),))

        runtime = CheckedRuntime(ScriptedAgent(blocked))
        runner = self.runner(runtime)
        self.addCleanup(release.set)
        task_id = runner.submit_task("persist")
        self.assertTrue(entered.wait(2))
        with patch.object(runner.store, "save", side_effect=sqlite3.OperationalError("read-only disk")):
            release.set()
            result = runner.wait_task(task_id, 2)
            self.assertEqual(result.task.status, TaskStatus.INTERRUPTED)
            self.assertTrue(runner._worker.is_alive())
            with self.assertRaises(RuntimeError):
                runner.submit_task("more")
        self.assertFalse(runtime.commands)

    def test_coding_handoff_is_persisted_before_dispatch_and_never_runs_skills(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("coding_task", coding_task="实现一个本地技能")))
        runner = self.runner(runtime)
        calls = []

        def dispatch(task, decision, cancelled):
            self.assertFalse(cancelled.is_set())
            self.assertTrue(any(item.event_type == "task.coding_dispatch_started"
                                for item in runner.get_task(task.task_id).history))
            calls.append((task.task_id, decision.coding_task))
            return "编码任务已完成"

        runtime.dispatch_coding_task = dispatch
        result = runner.wait_task(runner.submit_task("补充能力"), 2)
        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(result.reply, "编码任务已完成")
        self.assertEqual(len(calls), 1)
        self.assertFalse(runtime.commands)

    def test_legacy_coding_handoff_preserves_failure_reason(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("coding_task", coding_task="add skill",
                                                             legacy_coding=True, reply="缺少技能；")))
        runtime.dispatch_coding_task = lambda *_args: "编码已转交"
        runner = self.runner(runtime)
        result = runner.wait_task(runner.submit_task("new feature"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertEqual(result.reply, "缺少技能；编码已转交")

    def test_pre_cancelled_coding_decision_does_not_dispatch(self):
        entered, release = Event(), Event()

        def blocked(_context):
            entered.set()
            release.wait(2)
            return AgentDecision("coding_task", coding_task="do not start")

        runtime = CheckedRuntime(ScriptedAgent(blocked))
        calls = []
        runtime.dispatch_coding_task = lambda *_args: calls.append(True) or "done"
        runner = self.runner(runtime)
        self.addCleanup(release.set)
        task_id = runner.submit_task("cancel coding before model returns")
        self.assertTrue(entered.wait(2))
        runner.cancel_task(task_id)
        release.set()
        runner._queue.join()
        self.assertEqual(runner.get_task(task_id).task.status, TaskStatus.CANCELLED)
        self.assertFalse(calls)

    def test_coding_persistence_failure_does_not_dispatch(self):
        entered, release = Event(), Event()

        def blocked(_context):
            entered.set()
            release.wait(2)
            return AgentDecision("coding_task", coding_task="do not start")

        runtime = CheckedRuntime(ScriptedAgent(blocked))
        calls = []
        runtime.dispatch_coding_task = lambda *_args: calls.append(True) or "done"
        runner = self.runner(runtime)
        self.addCleanup(release.set)
        task_id = runner.submit_task("persist before coding")
        self.assertTrue(entered.wait(2))
        save = runner.store.save

        def fail_started(result, metadata, **kwargs):
            if any(record.event_type == "task.coding_dispatch_started" for record in result.history):
                raise sqlite3.OperationalError("disk full before coding")
            return save(result, metadata, **kwargs)

        with patch.object(runner.store, "save", side_effect=fail_started):
            release.set()
            result = runner.wait_task(task_id, 2)
            self.assertEqual(result.task.status, TaskStatus.INTERRUPTED)
        self.assertFalse(calls)

    def test_coding_cancellation_discards_late_result_and_does_not_complete(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("coding_task", coding_task="slow coding")))
        entered = Event()

        def dispatch(_task, _decision, cancelled):
            entered.set()
            cancelled.wait(2)
            return "late successful result"

        runtime.dispatch_coding_task = dispatch
        runner = self.runner(runtime)
        task_id = runner.submit_task("cancel running coding")
        self.assertTrue(entered.wait(2))
        self.assertEqual(runner.cancel_task(task_id).task.status, TaskStatus.CANCELLED)
        runner._queue.join()
        self.assertEqual(runner.get_task(task_id).task.status, TaskStatus.CANCELLED)
        self.assertNotEqual(runner.get_task(task_id).reply, "late successful result")

    def test_coding_cancel_after_durable_started_record_prevents_dispatch(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("coding_task", coding_task="do not start")))
        calls = []
        runtime.dispatch_coding_task = lambda *_args: calls.append(True) or "done"
        runner = self.runner(runtime)
        save = runner.store.save

        def cancel_started(result, metadata, **kwargs):
            outcome = save(result, metadata, **kwargs)
            if result.task.status != TaskStatus.CANCELLED and any(
                record.event_type == "task.coding_dispatch_started" for record in result.history
            ):
                runner.cancel_task(result.task.task_id)
            return outcome

        with patch.object(runner.store, "save", side_effect=cancel_started):
            task_id = runner.submit_task("cancel at coding boundary")
            self.assertEqual(runner.wait_task(task_id, 2).task.status, TaskStatus.CANCELLED)
            runner._queue.join()
        self.assertFalse(calls)

    def test_coding_restart_and_duplicate_submit_never_dispatches_again(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = CheckedRuntime(ScriptedAgent(AgentDecision("coding_task", coding_task="only once")))
            calls = []
            runtime.dispatch_coding_task = lambda *_args: calls.append(True) or "done"
            path = Path(directory) / "coding.sqlite"
            first = TaskRunner(runtime, store_path=path)
            task_id = first.submit_task("coding", task_id="coding-1")
            self.assertEqual(first.wait_task(task_id, 2).task.status, TaskStatus.COMPLETED)
            first.close()
            second = TaskRunner(runtime, store_path=path)
            second.submit_task("coding", task_id="coding-1")
            self.assertEqual(second.wait_task(task_id, 2).task.status, TaskStatus.COMPLETED)
            second.close()
            self.assertEqual(calls, [True])

    def test_duplicate_submit_and_restart_never_repeat_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = CheckedRuntime(ScriptedAgent(AgentIntent("navigate", {})))
            first = TaskRunner(runtime, store_path=Path(directory) / "tasks.db")
            task_id = first.submit_task("move", task_id="stable-id")
            self.assertEqual(first.wait_task(task_id, 2).task.status, TaskStatus.COMPLETED)
            first.submit_task("move", task_id="stable-id")
            first.close()
            second = TaskRunner(runtime, store_path=Path(directory) / "tasks.db")
            second.submit_task("move", task_id="stable-id")
            self.assertEqual(second.wait_task(task_id, 2).task.status, TaskStatus.COMPLETED)
            second.close()
            self.assertEqual(len(runtime.commands), 1)

    def test_failed_note_delivery_retries_only_note(self):
        class NotesAgent(ScriptedAgent):
            fail = True
            notes = []

            def record_note(self, conversation, event_id, text, *, user_text=None):
                if self.fail:
                    raise ConnectionError("dialogue unavailable")
                self.notes.append((conversation, event_id, text, user_text))

        agent = NotesAgent()
        runtime = CheckedRuntime(agent)
        runner = self.runner(runtime)
        task_id = runner.submit_task("/navigate")
        self.assertEqual(runner.wait_task(task_id, 2).task.status, TaskStatus.COMPLETED)
        runner._queue.join()
        self.assertTrue(runner.store.pending_notes())
        agent.fail = False
        runner.flush_outbox()
        self.assertFalse(runner.store.pending_notes())
        self.assertEqual([note[-1] for note in agent.notes if note[-1] is not None], ["/navigate"])
        self.assertEqual(len(runtime.commands), 1)


    def test_real_deadline_interrupts_blocked_provider_and_returns_without_waiting(self):
        entered, release = Event(), Event()

        def blocked(_context):
            entered.set()
            release.wait(3)
            return AgentDecision("act", (AgentIntent("navigate", {}),))

        runtime = CheckedRuntime(ScriptedAgent(blocked))
        interrupts = []

        def interrupt(task_id):
            interrupts.append(task_id)
            release.set()

        runtime.interrupt_task_decision = interrupt
        runner = self.runner(runtime, limits=TaskLimits(duration_seconds=0.15))
        self.addCleanup(release.set)
        task_id = runner.submit_task("blocking decision")
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        result = runner.wait_task(task_id, 0.6)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertIn("deadline", result.reply.lower())
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertTrue(release.wait(1))
        self.assertEqual(interrupts, [task_id])
        release.set()
        runner._queue.join()
        self.assertFalse(runtime.commands)

    def test_real_deadline_during_coding_stops_dispatch_and_keeps_failure(self):
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("coding_task", coding_task="slow")))
        started, cancelled = Event(), Event()

        def dispatch(_task, _decision, cancel_event):
            started.set()
            cancel_event.wait(3)
            cancelled.set()
            return "late coding result"

        runtime.dispatch_coding_task = dispatch
        runner = self.runner(runtime, limits=TaskLimits(duration_seconds=0.15))
        task_id = runner.submit_task("slow coding")
        self.assertTrue(started.wait(1))
        result = runner.wait_task(task_id, 0.6)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertIn("deadline", result.reply.lower())
        self.assertTrue(cancelled.wait(1))
        runner._queue.join()
        self.assertNotEqual(runner.get_task(task_id).reply, "late coding result")
        self.assertFalse(runtime.commands)

    def test_finished_activation_timer_cannot_expire_resumed_task(self):
        entered, release = Event(), Event()

        def resumed(_context):
            entered.set()
            release.wait(2)
            return AgentDecision("complete", reply="resumed")

        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("clarify", reply="which window?"), resumed))
        runner = self.runner(runtime)
        self.addCleanup(release.set)
        with patch("robotsoul.task_runner.Timer") as timer_factory:
            task_id = runner.submit_task("clarify then resume")
            self.assertEqual(runner.wait_task(task_id, 1).task.status, TaskStatus.AWAITING_INPUT)
            runner._queue.join()
            original_activation = runner._activation
            self.assertEqual(timer_factory.return_value.cancel.call_count, 1)
            runner.resume_task(task_id, "balcony", conversation=runner.default_conversation())
            self.assertTrue(entered.wait(1))
            runner._deadline_reached(runner._entry(task_id), original_activation)
            self.assertEqual(runner.get_task(task_id).task.status, TaskStatus.AWAITING_AGENT)
            self.assertFalse(runner._entry(task_id).deadline_expired.is_set())
            release.set()
            self.assertEqual(runner.wait_task(task_id, 1).reply, "resumed")
            runner._queue.join()
            self.assertEqual(timer_factory.return_value.cancel.call_count, 2)


if __name__ == "__main__":
    unittest.main()
