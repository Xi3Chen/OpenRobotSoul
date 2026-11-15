from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from robotsoul.build import detect_host_architecture, detect_host_platform
from robotsoul.language_session import CodingDispatcher, LanguageAgent, LanguageSession
from robotsoul.models import (
    AgentDecision,
    AuditRecord,
    CapabilitySpec,
    ConversationContext,
    Feedback,
    FeedbackStatus,
    ParameterSpec,
    Task,
    TaskContext,
    TaskStatus,
    utc_now,
)


class RecordingSession:
    """Capture the real Python-to-Go request without replacing the Go response."""

    protocol_version = 1

    def __init__(self, session):
        self.session = session
        self.requests = []
        self.responses = []

    def request(self, payload):
        self.requests.append(payload)
        response = self.session.request(payload)
        self.responses.append(response)
        return response


class DialogueBudgetTests(unittest.TestCase):
    def test_256_terminal_commands_fit_the_real_dialogue_budget_without_losing_evidence(self):
        platform_name = detect_host_platform()
        architecture = detect_host_architecture()
        binary_name = "openrobotsoul-dialogue.exe" if platform_name == "windows" else "openrobotsoul-dialogue"
        binary = Path(__file__).resolve().parents[1] / "build" / f"{platform_name}-{architecture}" / "bin" / binary_name
        if not binary.is_file():
            self.skipTest("Native dialogue binary not built; run python -m robotsoul.build --dialogue-only")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        session = LanguageSession.start(
            [str(binary), "--fake-provider", "--home", str(Path(temporary.name) / "dialogue"), "--context-window", "32768"],
            timeout_seconds=10,
        )
        self.addCleanup(session.close)
        session.handshake()
        bridge = RecordingSession(session)
        context, terminal_results, semantic_events = _completed_timeline_context()
        original_context = asdict(context)
        capability = CapabilitySpec("phone.flash", {"enabled": ParameterSpec("boolean")})

        decision = LanguageAgent(bridge, CodingDispatcher()).decide(context, {capability.name: capability})

        self.assertIsInstance(decision, AgentDecision)
        self.assertEqual(decision.kind, "complete")
        self.assertEqual(decision.reply, "256 个节拍的灯光时间线已完成。")
        self.assertEqual(decision.evidence_refs, tuple(item.feedback_id for item in terminal_results))
        self.assertEqual(len(decision.evidence_refs), 256)
        self.assertNotEqual(bridge.responses[0].get("error_code"), "context_budget")
        request = bridge.requests[0]
        self.assertEqual(request["type"], "resume")
        self.assertEqual(request["session_key"], context.conversation.session_key)
        wire_context = request["task_context"]
        self.assertEqual(wire_context["task"]["task_id"], context.task.task_id)
        self.assertEqual(wire_context["task"]["trace_id"], context.task.trace_id)
        if "user_input_ref" in wire_context["task"]:
            self.assertEqual(wire_context["task"]["user_input_ref"], "request.text")
            self.assertEqual(request["text"], context.task.user_input)
        else:
            self.assertEqual(wire_context["task"]["user_input"], context.task.user_input)
        self.assertEqual(len(wire_context["feedback"]), 256)
        terminal_feedback = _decode_terminal_feedback(wire_context)
        expected_results = {item.feedback_id: item for item in terminal_results}
        command_ids = set()
        command_skills = {
            item["details"]["command_id"]: item["details"]["skill"]
            for item in wire_context["history"] if item["event_type"] == "command.created"
        }
        self.assertEqual([item["feedback_id"] for item in terminal_feedback], list(expected_results))
        for result in terminal_feedback:
            original = expected_results[result["feedback_id"]]
            self.assertEqual(result["command_id"], original.command_id)
            UUID(result["feedback_id"])
            UUID(result["command_id"])
            command_ids.add(result["command_id"])
            self.assertEqual(result["status"], original.status.value)
            self.assertEqual(result["message"], original.message)
            self.assertEqual(result["error_code"], original.error_code)
            self.assertEqual(result["evidence"], original.evidence)
            self.assertEqual(result.get("skill") or command_skills.get(result["command_id"]), "phone.flash")
            self.assertNotIn("task_id", result)
            self.assertNotIn("trace_id", result)
            self.assertNotIn("timestamp", result)
        self.assertEqual(len(command_ids), 256)
        history = wire_context["history"]
        filtered = {"task.status", "command.feedback", "tool.call.started", "tool.call.completed",
                    "task.evaluation", "plan.step_completed"}
        self.assertFalse(any(item["event_type"] in filtered for item in history))
        for event_type, details in semantic_events:
            self.assertIn({"event_type": event_type, "details": details}, history)
        self.assertTrue(all(set(item) == {"event_type", "details"} for item in history))
        self.assertEqual(asdict(context), original_context)
        self.assertEqual(len(context.feedback), 768)
        self.assertTrue(any(item.event_type == "tool.call.started" for item in context.history))


def _decode_terminal_feedback(context):
    if "feedback_columns" not in context:
        return context["feedback"]
    columns = context["feedback_columns"]
    if columns != ["feedback_id", "command_id", "variant", "evidence"]:
        raise AssertionError(f"Unexpected terminal feedback columns: {columns!r}")
    variants = context["feedback_variants"]
    decoded = []
    for row in context["feedback"]:
        if len(row) != len(columns):
            raise AssertionError("A terminal feedback row has missing or extra values")
        item = dict(zip(columns, row))
        variant = item.pop("variant")
        if type(variant) is not int or not 0 <= variant < len(variants):
            raise AssertionError("A terminal feedback row has an invalid variant reference")
        decoded.append({**variants[variant], **item})
    return decoded


def _completed_timeline_context():
    now = utc_now()
    task_id, trace_id = str(uuid4()), str(uuid4())
    feedback = []
    terminal_results = []
    history = []
    semantic_events = (
        ("agent.decision", {"kind": "run_rule", "decision_id": str(uuid4())}),
        ("timeline.ended", {}),
    )
    history.append(AuditRecord(now, semantic_events[0][0], task_id, trace_id, semantic_events[0][1]))
    for index in range(256):
        command_id = str(uuid4())
        call_id = str(uuid4())
        enabled = index % 2 == 0
        timestamp = now + timedelta(milliseconds=index * 100)
        history.append(AuditRecord(timestamp, "command.created", task_id, trace_id,
                                   {"command_id": command_id, "skill": "phone.flash"}))
        audit = {"call_id": call_id, "command_id": command_id, "task_id": task_id, "trace_id": trace_id,
                 "skill": "phone.flash", "parameters": {"enabled": enabled},
                 "source": "agent", "risk_level": "normal", "timestamp": timestamp.isoformat()}
        history.append(AuditRecord(timestamp, "tool.call.started", task_id, trace_id, dict(audit)))
        history.append(AuditRecord(timestamp, "task.status", task_id, trace_id, {"status": "executing"}))
        for status in (FeedbackStatus.ACCEPTED, FeedbackStatus.RUNNING, FeedbackStatus.SUCCEEDED):
            evidence = {"enabled": enabled, "brightness": 255 if enabled else 0, "condition_met": True} if status == FeedbackStatus.SUCCEEDED else {}
            message = ("MiMax2 flashlight on" if enabled else "MiMax2 flashlight off") if status == FeedbackStatus.SUCCEEDED else f"MiMax2 flash action {status.value}"
            result = Feedback(str(uuid4()), command_id, task_id, trace_id, status, timestamp,
                              message=message, evidence=evidence)
            feedback.append(result)
            history.append(AuditRecord(timestamp, "command.feedback", task_id, trace_id,
                                       {"feedback_id": result.feedback_id, "command_id": command_id,
                                        "status": status.value, "message": message, "evidence": evidence}))
            if status == FeedbackStatus.SUCCEEDED:
                terminal_results.append(result)
        history.append(AuditRecord(timestamp, "tool.call.completed", task_id, trace_id,
                                   {**audit, "status": "succeeded", "evidence": evidence}))
    history.append(AuditRecord(now, semantic_events[1][0], task_id, trace_id, semantic_events[1][1]))
    reply = {"kind": "complete", "reply": "256 个节拍的灯光时间线已完成。", "evaluator": "skill_completion",
             "evidence_refs": [item.feedback_id for item in terminal_results]}
    context = TaskContext(
        Task(task_id, trace_id, "robot-1", json.dumps(reply, ensure_ascii=False), status=TaskStatus.AWAITING_AGENT),
        ConversationContext("robot-1", "voice", "alice", str(uuid4())),
        feedback=tuple(feedback), decision_count=1, history=tuple(history),
        result_evaluators=({"name": "skill_completion", "description": "Verify each terminal result against its task and command"},),
    )
    return context, terminal_results, semantic_events


if __name__ == "__main__":
    unittest.main()
