import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from robotsoul import RobotRuntime, load_profile
from robotsoul.models import CapabilitySpec, ParameterSpec, RiskLevel
from robotsoul.language_gateway import handle_request
from robotsoul.language_session import (
    AgentRejected,
    CodingDispatcher,
    LanguageAgent,
    LanguageSafetyReviewer,
    LanguageSession,
    report_startup_failure,
)
from robotsoul.models import Observation, utc_now


FAKE_GATEWAY = r"""
import json
import sys

log_path, mode = sys.argv[1], sys.argv[2]
with open(log_path, "a", encoding="utf-8") as log:
    for line in sys.stdin:
        request = json.loads(line)
        log.write(request["type"] + "\n")
        log.flush()
        if request["type"] == "review":
            body = {"verdict": "deny", "rationale": "姿态不适合这个动作"}
        elif mode == "reject":
            body = {"rejected": True, "reason": "没有对应的已注册技能"}
        elif mode == "coding":
            body = {"coding_task": "修复启动失败", "reason": "这是编程请求"}
        elif mode == "high":
            body = {"skill": "test.high", "parameters": {"enable": True}, "rationale": "高危"}
        elif mode == "move":
            body = {
                "skill": "base.move_distance",
                "parameters": {"distance_m": 0.2, "max_speed_mps": 0.1},
                "rationale": "前进",
            }
        else:
            body = {"skill": "head.look", "parameters": {"yaw_deg": 0, "pitch_deg": 10}, "rationale": "抬头"}
        body["id"] = request["id"]
        sys.stdout.write(json.dumps(body, ensure_ascii=False) + "\n")
        sys.stdout.flush()
"""


class LanguageSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.script = self.root / "gateway.py"
        self.script.write_text(FAKE_GATEWAY, encoding="utf-8")
        self.log = self.root / "requests.log"
        self.coding = CodingDispatcher(lambda prompt: f"已记录：{prompt}")

    def tearDown(self):
        if getattr(self, "session", None) is not None:
            self.session.close()
        self.temporary.cleanup()

    def start(self, mode: str) -> LanguageSession:
        import sys

        self.session = LanguageSession.start(
            [sys.executable, str(self.script), str(self.log), mode],
            timeout_seconds=5,
        )
        return self.session

    def test_text_selects_registered_skill_without_starting_coding_or_review(self):
        session = self.start("look")
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=LanguageAgent(session, self.coding),
            safety_reviewer=LanguageSafetyReviewer(session),
        )

        result = runtime.run_text("请抬头")

        self.assertEqual(result.task.status, "completed")
        self.assertEqual(runtime.adapter.executed_commands[0].skill, "head.look")
        self.assertEqual(self.coding.calls, [])
        self.assertEqual(self.log.read_text(encoding="utf-8").split(), ["decide"])

    def test_rejection_reason_is_preserved(self):
        session = self.start("reject")
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=LanguageAgent(session, self.coding),
        )

        result = runtime.run_text("请站起来")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(result.task.error, "没有对应的已注册技能")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_confirm_denial_uses_review_rationale_and_does_not_execute(self):
        session = self.start("move")
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=LanguageAgent(session, self.coding),
            safety_reviewer=LanguageSafetyReviewer(session),
        )

        result = runtime.run_text("前进")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(result.task.error, "姿态不适合这个动作")
        self.assertEqual(runtime.adapter.executed_commands, [])
        self.assertEqual(self.log.read_text(encoding="utf-8").split(), ["decide", "review"])

    def test_high_skill_selected_by_the_language_process_is_not_executed(self):
        session = self.start("high")
        profile = load_profile("simulator-basic")
        high = CapabilitySpec(
            "test.high",
            {"enable": ParameterSpec("boolean")},
            risk_level=RiskLevel.HIGH,
            allowed_callers=("agent", "dashboard"),
            requires_operator_confirmation=True,
        )
        runtime = RobotRuntime.from_profile(
            replace(profile, capabilities=(*profile.capabilities, high)),
            agent=LanguageAgent(session, self.coding),
            safety_reviewer=LanguageSafetyReviewer(session),
        )

        result = runtime.run_text("做危险动作")

        self.assertEqual(result.task.status, "failed")
        self.assertIn("operator confirmation", result.task.error)
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_coding_task_starts_dispatcher_once_and_does_not_call_a_robot_skill(self):
        session = self.start("coding")
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=LanguageAgent(session, self.coding),
        )

        result = runtime.run_text("请修复测试")

        self.assertEqual(self.coding.calls, ["修复启动失败"])
        self.assertIn("这是编程请求", result.task.error)
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_startup_failure_dispatches_one_coding_task(self):
        dispatcher = CodingDispatcher(lambda prompt: prompt)

        report_startup_failure(dispatcher, "adapter health failed")

        self.assertEqual(dispatcher.calls, ["模块启动失败：adapter health failed"])


class LanguageGatewayTests(unittest.TestCase):
    def test_model_json_selects_only_a_registered_skill(self):
        def complete(_messages):
            return """```json
            {"skill":"sport.stand_up","parameters":{},"rationale":"用户要站起来"}
            ```"""

        response = handle_request(
            {
                "id": "1",
                "type": "decide",
                "text": "请站起来",
                "skills": [{"name": "sport.stand_up", "risk_level": "confirm", "parameters": {}}],
            },
            complete,
        )

        self.assertEqual(response["skill"], "sport.stand_up")
        self.assertEqual(response["rationale"], "用户要站起来")

    def test_decide_prompt_includes_the_sdk_description(self):
        seen = {}

        def complete(messages):
            seen["system"] = messages[0]["content"]
            seen["user"] = messages[1]["content"]
            return json.dumps({"rejected": True, "reason": "只检查提示词"})

        handle_request(
            {
                "id": "1b",
                "type": "decide",
                "text": "请站起来",
                "skills": [{
                    "name": "sport.stand_up",
                    "description": "切换到站立锁定",
                    "parameters": {},
                }],
            },
            complete,
        )

        self.assertIn("description", seen["system"])
        self.assertIn("切换到站立锁定", seen["user"])

    def test_decide_prompt_includes_sensor_readings(self):
        seen = {}

        def complete(messages):
            seen["system"] = messages[0]["content"]
            seen["user"] = messages[1]["content"]
            return json.dumps({"rejected": True, "reason": "只检查传感器"})

        handle_request(
            {
                "id": "sensors",
                "type": "decide",
                "text": "现在电量够吗",
                "skills": [{"name": "sport.stand_up", "parameters": {}}],
                "sensors": [{
                    "event_type": "robot.low_state",
                    "observed_at": "2026-11-14T00:00:00+00:00",
                    "reading": {"bms_soc": 80},
                }],
            },
            complete,
        )

        self.assertIn("传感器摘要", seen["system"])
        self.assertIn("bms_soc", seen["user"])

    def test_language_agent_forwards_compact_sensor_readings(self):
        captured = {}

        class Session:
            def request(self, payload):
                captured["payload"] = payload
                return {"rejected": True, "reason": "只检查传感器"}

        now = utc_now()
        observation = Observation(
            event_id="event-low",
            robot_id="go2",
            device_id="unitree-go2-low-state",
            event_type="robot.low_state",
            observed_at=now,
            received_at=now,
            payload={"bms_soc": 80, "imu_rpy": [0.0, 0.0, 0.0]},
            source="unitree_sdk2",
            ttl_ms=2000,
        )
        agent = LanguageAgent(Session(), CodingDispatcher(lambda prompt: prompt))

        with self.assertRaises(AgentRejected):
            agent.plan("向前走", {}, (observation,))

        reading = captured["payload"]["sensors"][0]["reading"]
        self.assertEqual(reading["bms_soc"], 80)
        self.assertNotIn("motors", json.dumps(captured["payload"]["sensors"]))

    def test_unknown_skill_is_rejected_with_a_reason(self):
        response = handle_request(
            {"id": "2", "type": "decide", "text": "飞起来", "skills": [{"name": "sport.stand_up", "parameters": {}}]},
            lambda _messages: json.dumps({"skill": "sport.fly", "parameters": {}}),
        )

        self.assertTrue(response["rejected"])
        self.assertIn("未注册", response["reason"])

    def test_review_keeps_the_model_rationale(self):
        response = handle_request(
            {"id": "3", "type": "review", "skill": "sport.stand_up", "parameters": {}, "user_input": "请站起来"},
            lambda _messages: json.dumps({"verdict": "deny", "rationale": "电量太低"}),
        )

        self.assertEqual(response["verdict"], "deny")
        self.assertEqual(response["rationale"], "电量太低")


class ControlPageTests(unittest.TestCase):
    def test_control_page_is_a_collapsed_conversation(self):
        page = Path("robotsoul/web/index.html").read_text(encoding="utf-8")
        stylesheet = Path("robotsoul/web/dashboard.css").read_text(encoding="utf-8")

        self.assertIn('id="conversation-log"', page)
        self.assertIn("operations-panel is-collapsed", page)
        self.assertIn('id="operations-toggle"', page)
        self.assertIn("overflow-y: auto", stylesheet)
