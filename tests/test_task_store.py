from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robotsoul.models import (
    AgentDecision, Command, ConversationContext, Feedback, FeedbackStatus,
    InvocationContext, InvocationSource, Task, TaskResult, TaskStatus, utc_now,
)
from robotsoul.task_store import TaskStore


class TaskStoreTests(unittest.TestCase):
    def request(self, status=TaskStatus.RECEIVED):
        task = Task("task-1", "trace-1", "robot-1", "检查阳台窗户", status=status)
        result = TaskResult(task, (), None, (), "")
        conversation = ConversationContext("robot-1", "voice", "alice", "chat-1")
        invocation = InvocationContext(InvocationSource.AGENT)
        return result, conversation, invocation

    def test_restart_marks_inflight_command_unknown_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.sqlite"
            store = TaskStore(path)
            result, conversation, invocation = self.request(TaskStatus.EXECUTING)
            store.create(result, conversation, invocation, {"decisions": 2})
            command = Command("command-1", "task-1", "trace-1", "robot-1", "navigate", {},
                              utc_now() + timedelta(seconds=10))
            store.dispatch(command)
            store.close()
            recovered = TaskStore(path)
            self.addCleanup(recovered.close)
            actual, actual_conversation, _, metadata = recovered.load_all()[0]
            self.assertEqual(actual.task.status, TaskStatus.INTERRUPTED)
            self.assertIn("restarted", actual.reply)
            self.assertEqual(actual_conversation, conversation)
            self.assertEqual(metadata["decisions"], 2)
            self.assertEqual(recovered.unknown_command_ids("task-1"), {"command-1"})
            with self.assertRaises(ValueError):
                recovered.dispatch(command)
            recovered.close()

    def test_completed_task_and_outbox_survive_restart_together(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.sqlite"
            store = TaskStore(path)
            result, conversation, invocation = self.request()
            store.create(result, conversation, invocation, {})
            completed = replace(result, task=replace(result.task, status=TaskStatus.COMPLETED), reply="检查完成")
            store.save(completed, {}, note=("note-1", {"text": "检查完成"}))
            store.close()
            recovered = TaskStore(path)
            self.addCleanup(recovered.close)
            self.assertEqual(recovered.load_all()[0][0].task.status, TaskStatus.COMPLETED)
            self.assertEqual(recovered.pending_notes(), [("note-1", "task-1", {"text": "检查完成"})])
            recovered.delivered("note-1")
            self.assertFalse(recovered.pending_notes())
            recovered.close()

    def test_restart_restores_feedback_committed_before_task_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.sqlite"
            store = TaskStore(path)
            result, conversation, invocation = self.request(TaskStatus.EXECUTING)
            store.create(result, conversation, invocation, {})
            command = Command("command-1", "task-1", "trace-1", "robot-1", "inspect", {}, utc_now())
            store.dispatch(command)
            feedback = Feedback("terminal-1", command.command_id, command.task_id, command.trace_id,
                                FeedbackStatus.SUCCEEDED, utc_now(), evidence={"condition_met": True})
            store.finish_command(command.command_id, (feedback,))
            store.close()
            recovered = TaskStore(path)
            actual = recovered.load_all()[0][0]
            self.assertEqual(actual.task.status, TaskStatus.INTERRUPTED)
            self.assertEqual(actual.feedback, (feedback,))
            self.assertFalse(recovered.unknown_command_ids("task-1"))
            recovered.close()

    def test_outbox_conflict_rolls_back_task_update_in_same_transaction(self):
        store = TaskStore()
        self.addCleanup(store.close)
        result, conversation, invocation = self.request()
        store.create(result, conversation, invocation, {})
        store.enqueue_note("note-1", "task-1", {"text": "old"})
        completed = replace(result, task=replace(result.task, status=TaskStatus.COMPLETED))
        with self.assertRaises(ValueError):
            store.save(completed, {}, note=("note-1", {"text": "different"}))
        self.assertEqual(store.load_all()[0][0].task.status, TaskStatus.RECEIVED)
        self.assertEqual(store.pending_notes()[0][-1], {"text": "old"})

    def test_request_ids_include_conversation_and_invocation_authority(self):
        store = TaskStore()
        self.addCleanup(store.close)
        result, conversation, invocation = self.request()
        self.assertTrue(store.create(result, conversation, invocation, {}))
        self.assertFalse(store.create(result, conversation, invocation, {}))
        with self.assertRaises(ValueError):
            store.create(result, replace(conversation, channel="dashboard"), invocation, {})
        with self.assertRaises(ValueError):
            store.create(result, conversation, replace(invocation, operator_confirmed=True), {})
        with self.assertRaises(ValueError):
            store.create(result, conversation, invocation, {"expected_evaluator": "window_check"})

    def test_decision_and_resume_ids_deduplicate_exact_payloads(self):
        store = TaskStore()
        self.addCleanup(store.close)
        result, conversation, invocation = self.request()
        store.create(result, conversation, invocation, {})
        decision = AgentDecision("complete", reply="hello")
        self.assertTrue(store.decision("task-1", "d-1", decision))
        self.assertFalse(store.decision("task-1", "d-1", decision))
        with self.assertRaises(ValueError):
            store.decision("task-1", "d-1", replace(decision, reply="changed"))
        payload = {"text": "阳台"}
        self.assertFalse(store.has_resume_event("task-1", "r-1", payload))
        self.assertTrue(store.resume_event("task-1", "r-1", payload))
        self.assertTrue(store.has_resume_event("task-1", "r-1", payload))
        self.assertFalse(store.resume_event("task-1", "r-1", payload))
        with self.assertRaises(ValueError):
            store.has_resume_event("task-1", "r-1", {"text": "厨房"})

    def test_terminal_feedback_retains_command_identity(self):
        store = TaskStore()
        self.addCleanup(store.close)
        result, conversation, invocation = self.request()
        store.create(result, conversation, invocation, {})
        command = Command("command-1", "task-1", "trace-1", "robot-1", "inspect", {}, utc_now())
        store.dispatch(command)
        accepted = Feedback("f-1", "command-1", "task-1", "trace-1", FeedbackStatus.ACCEPTED, utc_now())
        store.finish_command("command-1", (accepted,))
        self.assertEqual(store.unknown_command_ids("task-1"), {"command-1"})
        succeeded = replace(accepted, feedback_id="f-2", status=FeedbackStatus.SUCCEEDED,
                            evidence={"condition_met": True})
        store.finish_command("command-1", (accepted, succeeded))
        self.assertFalse(store.unknown_command_ids("task-1"))

    def test_corrupt_persisted_task_fails_loudly_on_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.sqlite"
            store = TaskStore(path)
            result, conversation, invocation = self.request()
            store.create(result, conversation, invocation, {})
            store.close()
            connection = sqlite3.connect(path)
            with connection:
                connection.execute("UPDATE tasks SET result='broken json'")
            connection.close()
            with self.assertRaises(ValueError):
                TaskStore(path)


    def test_stop_failed_command_is_recorded_as_unknown_outcome(self):
        store = TaskStore()
        self.addCleanup(store.close)
        result, conversation, invocation = self.request()
        store.create(result, conversation, invocation, {})
        command = Command("command-1", "task-1", "trace-1", "robot-1", "sport.stop_move", {}, utc_now())
        store.dispatch(command)
        failed = Feedback("terminal-1", command.command_id, command.task_id, command.trace_id,
                          FeedbackStatus.FAILED, utc_now(), "StopMove failed", "stop_failed", {})
        store.finish_command(command.command_id, (failed,))
        self.assertEqual(store.unknown_command_ids("task-1"), {"command-1"})


if __name__ == "__main__":
    unittest.main()
