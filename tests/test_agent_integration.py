from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import Mock, patch

from robotsoul.agent import AgentRejected
from robotsoul.dashboard import create_dashboard_server
from robotsoul.language_session import (
    CodingDispatcher, LanguageAgent, LanguageGatewayError, LanguageSafetyReviewer, LanguageSession,
)
from robotsoul.models import (
    AgentDecision,
    AgentIntent,
    AgentPlan,
    ConversationContext,
    Evaluation,
    Feedback,
    FeedbackStatus,
    Observation,
    Task,
    TaskContext,
    TaskResult,
    TaskStatus,
    utc_now,
)
from robotsoul.profiles import load_profile
from robotsoul.runlog import configure, current
from robotsoul.runtime import RobotRuntime
from robotsoul.voice.service import VoiceService
from robotsoul.voice.factory import create_voice_service
from tests.test_voice_service import FakeCapture, FakeEngine


PROTOCOL_PROCESS = r"""
import json
import sys
import time

log_path, mode = sys.argv[1:]
with open(log_path, 'a', encoding='utf-8') as log:
    for line in sys.stdin:
        request = json.loads(line)
        log.write(json.dumps(request, ensure_ascii=False) + '\n')
        log.flush()
        if request['type'] == 'shutdown':
            break
        if request['type'] == 'hello':
            body = {'ready': mode != 'not_ready', 'version': 2 if mode == 'wrong_version' else 1,
                    'context_manager': 'openrobotsoul-reliable', 'tools': 0}
            if mode == 'legacy_manager':
                body['context_manager'] = 'legacy'
            elif mode == 'enabled_tools':
                body['tools'] = 1
            elif mode == 'missing_isolation_fields':
                body.pop('context_manager')
                body.pop('tools')
        elif request['type'] == 'note':
            body = {'recorded': True}
        elif request['type'] == 'review':
            if mode == 'block_review':
                while True:
                    time.sleep(1)
            body = {'verdict': 'allow', 'rationale': 'test review'}
        elif request['text'] == 'legacy_action':
            body = {'skill': 'head.look', 'parameters': {'yaw_deg': 0, 'pitch_deg': 10}}
        elif request['text'] == 'legacy_plan':
            body = {'plan': [{'skill': 'head.look', 'parameters': {'yaw_deg': 0, 'pitch_deg': 10}}]}
        elif request['text'] == 'new_action':
            feedback = request.get('task_context', {}).get('feedback', [])
            if feedback:
                body = {'kind': 'complete', 'evidence_refs': [feedback[-1]['feedback_id']]}
            else:
                body = {'kind': 'act', 'steps': [{'skill': 'head.look', 'parameters': {'yaw_deg': 0, 'pitch_deg': 10}}]}
        elif request['text'] == 'review_action':
            body = {'skill': 'base.move_distance', 'parameters': {'distance_m': 0.2, 'max_speed_mps': 0.1}}
        elif request['text'] == 'after_cancel':
            body = {'kind': 'complete', 'reply': '下一条指令正常完成。'}
        elif request['text'] == 'unexpected_tool':
            body = {'kind': 'complete', 'reply': '已完成', 'tool_calls': [{'name': 'exec'}]}
        else:
            body = {'kind': 'clarify', 'reply': '要检查哪一扇窗户？', 'decision_id': 'decision-1'}
        body['id'] = request['id']
        sys.stdout.write(json.dumps(body, ensure_ascii=False) + '\n')
        sys.stdout.flush()
"""


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class DialogueProtocolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.script = self.root / "protocol.py"
        self.script.write_text(PROTOCOL_PROCESS, encoding="utf-8")
        self.log = self.root / "requests.jsonl"

    def start(self, mode="ready"):
        session = LanguageSession.start([sys.executable, str(self.script), str(self.log), mode], timeout_seconds=3)
        self.addCleanup(session.close)
        return session

    def requests(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def context(self, text="检查窗户", conversation=None):
        conversation = conversation or ConversationContext("robot-1", "voice", "alice", "conversation-1")
        return TaskContext(Task("task-1", "trace-1", conversation.robot_id, text), conversation)

    def test_handshake_and_decision_preserve_the_explicit_conversation_and_task_facts(self):
        session = self.start()
        session.handshake()
        self.assertEqual(session.protocol_version, 1)
        coding = CodingDispatcher(lambda prompt: prompt)
        agent = LanguageAgent(session, coding)
        now = utc_now()
        context = self.context()
        observation = Observation("seen-1", "robot-1", "camera", "window.state", now, now,
                                  {"state": "unknown"}, "simulator", 3000)
        context = replace(context, observations=(observation,), decision_count=1)

        decision = agent.decide(context, {})

        self.assertIsInstance(decision, AgentDecision)
        self.assertEqual(decision.kind, "clarify")
        self.assertEqual(decision.reply, "要检查哪一扇窗户？")
        hello, request = self.requests()
        self.assertEqual((hello["type"], hello["version"]), ("hello", 1))
        self.assertTrue(hello["id"])
        self.assertEqual((request["type"], request["version"]), ("resume", 1))
        self.assertEqual(request["session_key"], context.conversation.session_key)
        self.assertEqual(request["task_context"]["task"]["user_input"], "检查窗户")
        self.assertEqual(request["task_context"]["observations"][0]["event_id"], "seen-1")
        self.assertEqual(request["task_context"]["observations"][0]["observed_at"], now.isoformat())
        self.assertEqual(request["task_context"]["decision_count"], 1)
        self.assertEqual(coding.calls, [])

    def test_resume_and_notes_carry_stable_result_event_and_session_identifiers(self):
        session = self.start()
        session.handshake()
        agent = LanguageAgent(session, CodingDispatcher())
        context = self.context()
        feedback = Feedback("feedback-1", "command-1", "task-1", "trace-1", FeedbackStatus.SUCCEEDED,
                            utc_now(), evidence={"condition_met": True})
        context = replace(context, feedback=(feedback,), resume_input="阳台的窗户")

        agent.decide(context, {})
        agent.decide(replace(context, resume_input=""), {})
        agent.record_note(context.conversation, "result-event-1", "已完成检查", "检查窗户")

        _hello, answer, continuation, note = self.requests()
        self.assertEqual(answer["type"], "decide")
        self.assertEqual(answer["text"], "阳台的窗户")
        self.assertEqual(answer["task_context"]["task"]["user_input"], "检查窗户")
        self.assertEqual(answer["task_context"]["feedback"][0]["status"], "succeeded")
        self.assertEqual(answer["task_context"]["feedback"][0]["command_id"], "command-1")
        self.assertEqual(continuation["type"], "resume")
        self.assertEqual(continuation["session_key"], answer["session_key"])
        self.assertEqual(note["type"], "note")
        self.assertEqual(note["version"], 1)
        self.assertEqual(note["event_id"], "result-event-1")
        self.assertEqual(note["user_text"], "检查窗户")
        self.assertEqual(note["text"], "已完成检查")
        self.assertEqual(note["session_key"], answer["session_key"])
        self.assertNotEqual(note["id"], answer["id"])

    def test_user_entry_robot_and_conversation_each_partition_the_session_key(self):
        session = self.start()
        agent = LanguageAgent(session, CodingDispatcher())
        base = ConversationContext("robot-1", "dashboard", "Alice", "conversation-1")
        conversations = (base, replace(base, principal_id="alice"), replace(base, channel="voice"),
                         replace(base, robot_id="robot-2"), replace(base, conversation_id="conversation-2"))
        for conversation in conversations:
            agent.decide(self.context(conversation=conversation), {})
        keys = [request["session_key"] for request in self.requests()]
        self.assertEqual(len(set(keys)), len(conversations))
        self.assertEqual(keys, [conversation.session_key for conversation in conversations])
        self.assertEqual(base.session_key, replace(base).session_key)

    def test_decide_remains_compatible_with_responses_without_kind(self):
        session = self.start()
        agent = LanguageAgent(session, CodingDispatcher())
        action = agent.decide(self.context("legacy_action"), {})
        plan = agent.decide(self.context("legacy_plan"), {})
        self.assertIsInstance(action, AgentIntent)
        self.assertEqual(action.skill, "head.look")
        self.assertIsInstance(plan, AgentPlan)
        self.assertEqual(plan.steps, (action,))

    def test_unexpected_model_tool_call_is_rejected(self):
        session = self.start()
        coding = CodingDispatcher(lambda prompt: prompt)
        agent = LanguageAgent(session, coding)
        with self.assertRaisesRegex(AgentRejected, "工具调用"):
            agent.decide(self.context("unexpected_tool"), {})
        self.assertEqual(coding.calls, [])

    def test_new_action_protocol_executes_once_and_redecides_with_real_feedback(self):
        session = self.start()
        session.handshake()
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"), agent=LanguageAgent(session, CodingDispatcher()))
        self.addCleanup(runtime.close)
        conversation = runtime.conversation("dashboard", "alice", "dialogue-1")

        result = runtime.run_text("new_action", conversation=conversation)

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(len(runtime.adapter.executed_commands), 1)
        command = runtime.adapter.executed_commands[0]
        self.assertEqual(command.skill, "head.look")
        self.assertEqual(command.task_id, result.task.task_id)
        requests = [request for request in self.requests() if request["type"] in {"decide", "resume"}]
        self.assertEqual([request["type"] for request in requests], ["decide", "resume"])
        feedback = requests[1]["task_context"]["feedback"]
        self.assertTrue(feedback)
        self.assertEqual(feedback[-1]["status"], "succeeded")
        self.assertEqual(feedback[-1]["command_id"], command.command_id)
        self.assertTrue(feedback[-1]["evidence"]["condition_met"])
        self.assertEqual(requests[1]["session_key"], conversation.session_key)

    def test_bad_startup_handshake_cannot_enable_protocol_one(self):
        for mode in ("not_ready", "wrong_version", "legacy_manager", "enabled_tools", "missing_isolation_fields"):
            with self.subTest(mode=mode):
                session = self.start(mode)
                with self.assertRaisesRegex(LanguageGatewayError, "启动检查"):
                    session.handshake()
                self.assertEqual(session.protocol_version, 0)

    def test_note_failure_is_visible_and_legacy_processes_receive_no_new_note_message(self):
        class Session:
            protocol_version = 1

            def request(self, _payload):
                raise LanguageGatewayError("disk unavailable")

        session = Session()
        agent = LanguageAgent(session, CodingDispatcher())
        with self.assertRaisesRegex(LanguageGatewayError, "disk unavailable"):
            agent.record_note(self.context().conversation, "event-1", "结果")
        session.protocol_version = 0
        agent.record_note(self.context().conversation, "event-1", "结果")
        session.protocol_version = 1
        session.request = lambda _payload: {"recorded": False, "reason": "storage failed"}
        with self.assertRaisesRegex(LanguageGatewayError, "storage failed"):
            agent.record_note(self.context().conversation, "event-1", "结果")

    def test_cancel_during_risk_review_unblocks_the_worker_without_dispatching_the_action(self):
        for separate_reviewer in (False, True):
            with self.subTest(separate_reviewer=separate_reviewer):
                session = self.start("ready" if separate_reviewer else "block_review")
                session.handshake()
                reviewer_session = self.start("block_review") if separate_reviewer else session
                if separate_reviewer:
                    reviewer_session.handshake()
                runtime = RobotRuntime.from_profile(load_profile("simulator-basic"),
                                                   agent=LanguageAgent(session, CodingDispatcher()),
                                                   safety_reviewer=LanguageSafetyReviewer(reviewer_session))
                self.addCleanup(runtime.close)
                task_id = runtime.submit_task("review_action", task_id=f"review-waiting-{separate_reviewer}")
                self.assertTrue(wait_until(lambda: any(request["type"] == "review" and request.get("task_id") == task_id
                                                      for request in self.requests())))

                started = time.monotonic()
                runtime.cancel_task(task_id)
                cancelled = runtime.wait_task(task_id, timeout=2)

                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(cancelled.task.status, TaskStatus.CANCELLED)
                self.assertEqual(runtime.adapter.executed_commands, [])
                next_task = runtime.submit_task("after_cancel")
                self.assertEqual(runtime.wait_task(next_task, timeout=3).task.status, TaskStatus.COMPLETED)
                runtime.close()
                reviewer_session.close()
                session.close()


class BuiltDialogueSmokeTests(unittest.TestCase):
    def setUp(self):
        from robotsoul.build import detect_host_architecture, detect_host_platform

        platform_name = detect_host_platform()
        architecture = detect_host_architecture()
        filename = "openrobotsoul-dialogue.exe" if platform_name == "windows" else "openrobotsoul-dialogue"
        self.binary = Path(__file__).resolve().parents[1] / "build" / f"{platform_name}-{architecture}" / "bin" / filename
        if not self.binary.is_file():
            self.skipTest("Native dialogue binary not built; run python -m robotsoul.build --dialogue-only")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "dialogue"

    def start(self):
        session = LanguageSession.start([str(self.binary), "--fake-provider", "--home", str(self.home)], timeout_seconds=8)
        self.addCleanup(session.close)
        session.handshake()
        return session

    def test_real_go_process_restores_dialogue_deduplicates_requests_and_isolates_sessions(self):
        session = self.start()
        conversation = ConversationContext("robot-1", "voice", "alice", "dialogue-1")
        first = {"id": "first-goal", "type": "decide", "version": 1,
                 "session_key": conversation.session_key, "text": "检查阳台窗户"}
        response = session.request(first)
        self.assertEqual(response["kind"], "complete")
        self.assertEqual(response["history_messages"], 0)
        agent = LanguageAgent(session, CodingDispatcher())
        agent.record_note(conversation, "result-1", "导航完成")
        agent.record_note(conversation, "result-1", "导航完成")
        session.close()

        restarted = self.start()
        self.assertEqual(restarted.request(first), response)
        response = restarted.request({"id": "next-fact", "type": "resume", "version": 1,
                                      "session_key": conversation.session_key, "text": "窗户是打开的"})
        self.assertEqual(response["kind"], "complete")
        self.assertEqual(response["history_messages"], 3)
        isolated = restarted.request({"id": "isolated", "type": "decide", "version": 1,
                                      "session_key": replace(conversation, channel="dashboard").session_key,
                                      "text": "独立对话"})
        self.assertEqual(isolated["history_messages"], 0)

    def test_real_go_tool_call_is_rejected_before_python_can_dispatch_an_action(self):
        session = self.start()
        agent = LanguageAgent(session, CodingDispatcher())
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"), agent=agent)
        self.addCleanup(runtime.close)

        result = runtime.run_text("__tool_call__", conversation=runtime.conversation("voice", "alice", "tools-1"))

        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertIn("tool call", result.task.error)
        self.assertEqual(runtime.adapter.executed_commands, [])
        follow_up = runtime.run_text("你好", conversation=runtime.conversation("voice", "alice", "tools-1"))
        self.assertEqual(follow_up.task.status, TaskStatus.COMPLETED)
        self.assertEqual(follow_up.reply, "你好")

    def test_real_go_legacy_action_runs_once_through_python_task_runner(self):
        session = self.start()
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"), agent=LanguageAgent(session, CodingDispatcher()))
        self.addCleanup(runtime.close)
        text = json.dumps({"skill": "head.look", "parameters": {"yaw_deg": 0, "pitch_deg": 10}})

        result = runtime.run_text(text, task_id="legacy-go-task", conversation=runtime.conversation("voice", "alice", "action-1"))

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(len(runtime.adapter.executed_commands), 1)
        self.assertEqual(runtime.adapter.executed_commands[0].task_id, result.task.task_id)
        self.assertTrue(result.evaluation.success)
        self.assertEqual(runtime.get_task("legacy-go-task").reply, result.reply)

    def test_real_go_waiting_model_can_be_cancelled_and_the_next_task_restarts_the_process(self):
        session = self.start()
        process_before = session._process
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"), agent=LanguageAgent(session, CodingDispatcher()))
        self.addCleanup(runtime.close)
        task_id = runtime.submit_task("__wait_for_cancel__", task_id="waiting-go-model")
        self.assertTrue(wait_until(lambda: session._active_task_id == task_id
                                  and runtime.get_task(task_id).task.status == TaskStatus.AWAITING_AGENT))

        started = time.monotonic()
        cancelled = runtime.cancel_task(task_id)

        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(cancelled.task.status, TaskStatus.CANCELLED)
        next_task = runtime.submit_task("你好")
        next_result = runtime.wait_task(next_task, timeout=4)
        self.assertEqual(next_result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(next_result.reply, "你好")
        self.assertIsNotNone(process_before.poll())
        self.assertNotEqual(session._process.pid, process_before.pid)
        self.assertEqual(session.protocol_version, 1)
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_llm_or_robot_config_change_restarts_the_verified_process_and_retains_history(self):
        config = Path(self.temporary.name) / "llm.json"
        config.write_text(json.dumps({"timeout_seconds": 3, "model": "before"}), encoding="utf-8")
        robot = Path(self.temporary.name) / "robot.json"
        robot.write_text(json.dumps({"robot_name": "before"}), encoding="utf-8")
        original_start = LanguageSession.start
        original_resident = LanguageSession.start_resident

        def start_test_provider(command, timeout_seconds=35, env=None):
            return original_start([*command, "--fake-provider", "--home", str(self.home)], timeout_seconds, env)

        with patch("robotsoul.language_session.dialogue_binary", return_value=self.binary), patch.object(
            LanguageSession, "start", side_effect=start_test_provider
        ):
            session = LanguageSession.start_resident(config, robot)
            self.addCleanup(session.close)
            conversation = ConversationContext("robot-1", "voice", "alice", "config-1")
            request = {"type": "decide", "version": 1, "session_key": conversation.session_key, "text": "第一句"}
            first = session.request(request)
            self.assertEqual(first["history_messages"], 0)

            for target, update, expected_history in (
                (config, {"timeout_seconds": 4, "model": "after"}, 2),
                (robot, {"robot_name": "after"}, 4),
            ):
                with self.subTest(target=target.name):
                    previous = session._process
                    target.write_text(json.dumps(update), encoding="utf-8")
                    with patch.object(LanguageSession, "start_resident", wraps=original_resident) as restart:
                        result = session.request({**request, "text": "继续对话"})
                    restart.assert_called_once_with(config, robot)
                    self.assertIsNotNone(previous.poll())
                    self.assertNotEqual(session._process.pid, previous.pid)
                    self.assertEqual(session.protocol_version, 1)
                    self.assertEqual(result["history_messages"], expected_history)
                    self.assertEqual(session._timeout, 9)


class ClarifyingAgent:
    def __init__(self):
        self.contexts = []

    def decide(self, context, _capabilities):
        self.contexts.append(context)
        if context.resume_input:
            return AgentDecision("complete", reply=f"已收到：{context.resume_input}")
        return AgentDecision("clarify", reply="要检查哪一扇窗户？")


class DashboardTaskIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.agent = ClarifyingAgent()
        self.runtime = RobotRuntime.from_profile(load_profile("simulator-basic"), agent=self.agent)
        self.server = create_dashboard_server(self.runtime, operator_password="integration-pin-4982", port=0)
        self.server.service._operators.upsert("other", "another-pin-4982", persist=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.runtime.close()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers or {})
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = response.getheaders()
        connection.close()
        return response.status, response_headers, json.loads(raw)

    def login(self, username="operator", password="integration-pin-4982"):
        status, headers, body = self.request("POST", "/api/login", {"username": username, "password": password})
        self.assertEqual(status, 200)
        cookie = next(value.split(";", 1)[0] for name, value in headers if name.lower() == "set-cookie")
        return {"Cookie": cookie, "X-CSRF-Token": body["csrf_token"]}

    def submit(self, headers, task_id="task-1"):
        status, _headers, body = self.request("POST", "/api/tasks",
                                            {"text": "检查窗户", "async": True, "request_id": task_id}, headers)
        self.assertEqual(status, 202)
        self.assertEqual(body["task"]["task_id"], task_id)
        self.runtime.wait_task(task_id, timeout=3)
        return body

    def test_async_submit_does_not_wait_for_model_and_get_returns_the_clarification_reply(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.agent.decide

        def blocking_decision(context, capabilities):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test model was not released")
            return original(context, capabilities)

        self.agent.decide = blocking_decision
        self.addCleanup(release.set)
        headers = self.login()
        status, _headers, body = self.request("POST", "/api/tasks",
                                            {"text": "检查窗户", "async": True, "request_id": "async-1"}, headers)
        self.assertEqual(status, 202)
        self.assertEqual(body["task"]["task_id"], "async-1")
        self.assertTrue(entered.wait(2))
        self.assertNotEqual(body["task"]["status"], "completed")
        release.set()
        self.runtime.wait_task("async-1", timeout=3)
        status, _headers, body = self.request("GET", "/api/tasks/async-1", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "awaiting_input")
        self.assertEqual(body["task"]["reply"], "要检查哪一扇窗户？")
        self.assertIsNone(body["task"]["error"])
        self.assertIsNone(body["task"]["evaluation"])

    def test_get_resume_and_cancel_require_the_original_login_conversation(self):
        owner = self.login()
        other = self.login("other", "another-pin-4982")
        self.submit(owner)
        for method, path, body in (
            ("GET", "/api/tasks/task-1", None),
            ("POST", "/api/tasks/task-1/resume", {"text": "阳台的窗户"}),
            ("POST", "/api/tasks/task-1/cancel", {}),
        ):
            with self.subTest(path=path):
                status, _headers, _body = self.request(method, path, body, other)
                self.assertEqual(status, 403)
        self.assertEqual(self.runtime.get_task("task-1").task.status, TaskStatus.AWAITING_INPUT)
        status, _headers, body = self.request("POST", "/api/tasks/task-1/resume",
                                            {"text": "阳台的窗户", "event_id": "answer-1"}, owner)
        self.assertEqual(status, 200)
        completed = self.runtime.wait_task("task-1", timeout=3)
        self.assertEqual(completed.task.status, TaskStatus.COMPLETED)
        self.assertEqual(completed.reply, "已收到：阳台的窗户")
        status, _headers, body = self.request("GET", "/api/tasks/task-1", headers=owner)
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["reply"], "已收到：阳台的窗户")
        self.assertEqual(len(self.agent.contexts), 2)
        self.assertEqual(self.agent.contexts[1].task.user_input, "检查窗户")
        self.assertEqual(self.agent.contexts[1].resume_input, "阳台的窗户")
        self.assertEqual(self.runtime.adapter.executed_commands, [])

    def test_duplicate_submit_and_resume_do_not_repeat_decisions_and_owner_can_cancel(self):
        owner = self.login()
        self.submit(owner)
        self.submit(owner)
        self.assertEqual(len(self.agent.contexts), 1)
        answer = {"text": "阳台的窗户", "event_id": "answer-1"}
        self.request("POST", "/api/tasks/task-1/resume", answer, owner)
        self.runtime.wait_task("task-1", timeout=3)
        status, _headers, _body = self.request("POST", "/api/tasks/task-1/resume", answer, owner)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.agent.contexts), 2)
        self.submit(owner, "task-2")
        status, _headers, body = self.request("POST", "/api/tasks/task-2/cancel", {}, owner)
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "cancelled")
        self.assertTrue(body["task"]["reply"])

    def test_relogin_with_the_same_account_does_not_inherit_the_old_conversation(self):
        owner = self.login()
        second_login = self.login()
        self.submit(owner)
        status, _headers, _body = self.request("POST", "/api/tasks/task-1/resume", {"text": "阳台"}, second_login)
        self.assertEqual(status, 403)
        status, _headers, _body = self.request("GET", "/api/tasks/task-1", headers=second_login)
        self.assertEqual(status, 403)

    def test_synchronous_submission_exposes_reply_as_the_action_message(self):
        headers = self.login()
        status, _headers, body = self.request("POST", "/api/tasks", {"text": "检查窗户"}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "awaiting_input")
        self.assertEqual(body["task"]["reply"], "要检查哪一扇窗户？")
        self.assertEqual(body["action"]["message"], body["task"]["reply"])


class VoiceTaskIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_log = current().directory
        configure(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(configure, self.previous_log)

    def factory_service(self, agent):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"), agent=agent)
        self.addCleanup(runtime.close)
        data = Path(__file__).resolve().parents[1] / "robotsoul" / "data"
        with patch("robotsoul.voice.factory.SoundDeviceCapture", return_value=FakeCapture()), patch(
            "robotsoul.voice.factory.SoundDeviceOutput"
        ), patch("robotsoul.voice.factory.SherpaVoiceEngine", return_value=FakeEngine()), patch(
            "robotsoul.voice.factory.resolve_playback_output", return_value=Mock()
        ), patch("robotsoul.voice.factory.shutil.which", return_value=None), patch(
            "robotsoul.voice.factory.default_robot_config_path", return_value=data / "robot.json"
        ):
            service, _config = create_voice_service(runtime, config_path=data / "voice.json")
        self.addCleanup(service.stop)
        return runtime, service

    def test_reply_is_spoken_before_evaluation_or_task_error(self):
        for status, error, evaluation, reply in (
            (TaskStatus.COMPLETED, None, "旧的执行说明", "窗户检查已经完成。"),
            (TaskStatus.AWAITING_INPUT, None, None, "要检查哪一扇窗户？"),
            (TaskStatus.FAILED, "旧的失败说明", None, "请先打开手机连接。"),
        ):
            with self.subTest(status=status):
                service = VoiceService(capture=FakeCapture(), engine=FakeEngine(), run_text=lambda text: text)
                self.addCleanup(service.stop)
                task = Task("voice-1", "trace-1", "robot-1", "检查窗户", status=status, error=error)
                result_evaluation = None if evaluation is None else Evaluation("voice-1", "trace-1", True, evaluation, (), 1)
                result = TaskResult(task, (), result_evaluation, (), reply)
                future = Future()
                future.set_result(result)
                with patch.object(service, "_speak_reply") as speak:
                    service._handle_runtime_result(future, service.arbiter.generation)
                self.assertEqual(speak.call_args.args[0], reply)

    def test_factory_resumes_clarification_in_the_same_task_and_then_accepts_a_new_goal(self):
        agent = ClarifyingAgent()
        runtime, service = self.factory_service(agent)
        first = service.runtime_executor.submit_final("检查窗户").result(timeout=3)
        self.assertEqual(first.task.status, TaskStatus.AWAITING_INPUT)
        self.assertEqual(agent.contexts[0].conversation.channel, "voice")
        self.assertEqual(agent.contexts[0].conversation.principal_id, "local-microphone")

        answered = service.runtime_executor.submit_final("阳台的窗户").result(timeout=3)

        self.assertEqual(answered.task.task_id, first.task.task_id)
        self.assertEqual(answered.task.status, TaskStatus.COMPLETED)
        self.assertEqual(answered.reply, "已收到：阳台的窗户")
        self.assertEqual(agent.contexts[1].task.user_input, "检查窗户")
        self.assertEqual(agent.contexts[1].resume_input, "阳台的窗户")
        self.assertEqual(agent.contexts[1].conversation, agent.contexts[0].conversation)
        next_goal = service.runtime_executor.submit_final("检查门").result(timeout=3)
        self.assertNotEqual(next_goal.task.task_id, first.task.task_id)
        self.assertEqual(next_goal.task.status, TaskStatus.AWAITING_INPUT)
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_factory_does_not_resume_another_voice_entrys_pending_question(self):
        agent = ClarifyingAgent()
        runtime, service = self.factory_service(agent)
        microphone_conversation = service.runtime_conversation()
        first = service.runtime_executor.submit_final("检查窗户").result(timeout=3)
        service.runtime_conversation = lambda: runtime.conversation("call", "alice", "call-1")

        call_task = service.runtime_executor.submit_final("阳台的窗户").result(timeout=3)

        self.assertNotEqual(first.task.task_id, call_task.task.task_id)
        self.assertEqual(runtime.get_task(first.task.task_id).task.status, TaskStatus.AWAITING_INPUT)
        self.assertEqual(call_task.task.status, TaskStatus.AWAITING_INPUT)
        self.assertEqual(agent.contexts[1].conversation.channel, "call")
        self.assertEqual(agent.contexts[1].resume_input, "")
        service.runtime_conversation = lambda: microphone_conversation
        resumed = service.runtime_executor.submit_final("厨房的窗户").result(timeout=3)
        self.assertEqual(resumed.task.task_id, first.task.task_id)
        self.assertEqual(resumed.task.status, TaskStatus.COMPLETED)
        self.assertEqual(runtime.get_task(call_task.task.task_id).task.status, TaskStatus.AWAITING_INPUT)

    def test_queued_voice_text_keeps_the_identity_captured_before_hangup(self):
        agent = ClarifyingAgent()
        runtime, service = self.factory_service(agent)
        release = threading.Event()
        self.addCleanup(release.set)
        service.runtime_executor._pool.submit(lambda: release.wait(2))
        call = runtime.conversation("call", "alice", "call-before-hangup")
        future = service.runtime_executor.submit_final("检查窗户", conversation=call)
        service.runtime_conversation = lambda: runtime.conversation("voice", "local-microphone", "local")
        release.set()
        self.assertEqual(future.result(timeout=3).task.status, TaskStatus.AWAITING_INPUT)
        self.assertEqual(agent.contexts[0].conversation, call)

    def test_factory_maps_replacement_cancel_to_the_resumed_task_while_model_is_waiting(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingResumeAgent(ClarifyingAgent):
            def decide(self, context, capabilities):
                if context.resume_input:
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("test model was not released")
                return super().decide(context, capabilities)

        runtime, service = self.factory_service(BlockingResumeAgent())
        self.addCleanup(release.set)
        first = service.runtime_executor.submit_final("检查窗户").result(timeout=3)
        resumed = service.runtime_executor.submit_final("阳台的窗户")
        self.assertTrue(entered.wait(2))

        service.runtime_executor.supersede()

        result = resumed.result(timeout=2)
        self.assertEqual(result.task.task_id, first.task.task_id)
        self.assertEqual(result.task.status, TaskStatus.CANCELLED)
        self.assertEqual(runtime.adapter.executed_commands, [])
        release.set()


if __name__ == "__main__":
    unittest.main()
