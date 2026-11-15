import unittest
from robotsoul import (
    ConfirmationManager,
    ConfirmationRequest,
    ConfirmationStatus,
    EvidenceRequirement,
    ExecutionCondition,
    GestureSample,
    LocalSafetySupervisor,
    RobotRuntime,
    ReviewVerdict,
    SafetyReviewResult,
    load_profile,
)
from robotsoul.safety import DirectionalEvidenceTable
class AllowWithCondition:
    def __init__(self, condition):
        self.condition = condition

    def review(self, proposal):
        return SafetyReviewResult(
            ReviewVerdict.PASS,
            proposal.arguments_digest,
            "确认了动作风险和本地落地条件",
            conditions=(self.condition,),
        )


class SafetyConfirmationTests(unittest.TestCase):
    def test_directional_evidence_rotates_and_sensor_contradiction_wins(self):
        table = DirectionalEvidenceTable()
        table.update("front", 2.0, source="sensor", negative=True)
        requirement = EvidenceRequirement("front", quantity=0, range_m=1.0, valid_for_seconds=10)
        self.assertTrue(table.check(requirement).met)

        table.rotate(90)
        self.assertFalse(table.check(requirement).met)
        self.assertTrue(table.check(EvidenceRequirement("right", 0, 1.0, 10)).met)

        table.update("right", 0.5, quantity=1, source="sensor", negative=False)
        table.human_statement("right", 2.0, quantity=0)
        check = table.check(EvidenceRequirement("right", 0, 1.0, 10))
        self.assertFalse(check.met)
        self.assertIn("contradictory", check.reason)

    def test_local_supervisor_fails_closed_for_speed_zone_watchdog_and_estop(self):
        now = [100.0]
        stopped = []
        supervisor = LocalSafetySupervisor(
            "robot-1",
            max_speed_mps=0.5,
            watchdog_timeout_seconds=1.0,
            zone={"min_x": -1.0, "max_x": 1.0, "min_y": -1.0, "max_y": 1.0},
            clock=lambda: now[0],
            stop_callback=lambda reason: stopped.append(reason),
        )
        supervisor.heartbeat()
        command = type("Command", (), {
            "robot_id": "robot-1",
            "parameters": {"vx": 0.6, "vy": 0.0, "duration_ms": 1000},
            "command_id": "c1",
        })()
        self.assertEqual(supervisor.check(command).error_code, "speed_limit")

        command.parameters = {"vx": 0.5, "vy": 0.0, "duration_ms": 5000}
        self.assertEqual(supervisor.check(command).error_code, "zone_limit")

        now[0] = 102.0
        self.assertEqual(supervisor.check(command).error_code, "watchdog_expired")
        result = supervisor.trigger_emergency_stop("有人进入区域")
        self.assertEqual(result.error_code, "emergency_stop")
        self.assertEqual(stopped, ["有人进入区域"])
        self.assertEqual(supervisor.check(command).error_code, "emergency_stop")
        self.assertFalse(supervisor.clear_emergency_stop().allowed)
        self.assertTrue(supervisor.clear_emergency_stop(operator_authenticated=True).allowed)

    def test_confirmation_window_mutes_prompt_accepts_first_local_answer_and_times_out(self):
        now = [10.0]
        prompts, cues = [], []
        manager = ConfirmationManager(
            clock=lambda: now[0],
            prompt_callback=prompts.append,
            cue_callback=cues.append,
        )
        manager.open(ConfirmationRequest("id-1", "digest", "sport.front_flip", "请确认动作", timeout_seconds=10))
        self.assertTrue(manager.muted)
        now[0] = 10.21
        result = manager.accept_voice("确认", source="local", speaker_count=1)
        self.assertEqual(result.status, ConfirmationStatus.CONFIRMED)
        self.assertEqual(prompts, ["请确认动作"])
        self.assertEqual(cues, ["confirmation_open"])

        manager.open(ConfirmationRequest("id-2", "digest", "sport.front_flip", "请确认动作", timeout_seconds=10))
        now[0] = 20.0
        ambiguous = manager.accept_voice("我觉得可以", speaker_count=2)
        self.assertEqual(ambiguous.status, ConfirmationStatus.REJECTED)
        self.assertFalse(manager.active)

        manager.open(ConfirmationRequest("id-3", "digest", "sport.front_flip", "请确认动作", timeout_seconds=10))
        now[0] = 31.0
        expired = manager.tick()
        self.assertEqual(expired.status, ConfirmationStatus.EXPIRED)

    def test_irreversible_confirmation_requires_wake_prefix_and_gesture(self):
        now = [0.0]
        stopped = []
        manager = ConfirmationManager(clock=lambda: now[0], stop_callback=stopped.append)
        manager.open(ConfirmationRequest("id", "digest", "sport.front_flip", "请确认", irreversible=True, wake_phrase="索拉"))
        now[0] = 0.21
        self.assertEqual(manager.accept_voice("确认").status, ConfirmationStatus.REJECTED)

        manager.open(ConfirmationRequest("id-2", "digest", "sport.front_flip", "请确认", irreversible=True, wake_phrase="索拉"))
        now[0] = 0.42
        waiting = manager.accept_voice("索拉，确认")
        self.assertEqual(waiting.status, ConfirmationStatus.OPEN)
        confirmed = manager.accept_gesture(GestureSample("ok", 0.5, True, 1.0))
        self.assertEqual(confirmed.status, ConfirmationStatus.CONFIRMED)

        manager.open(ConfirmationRequest("id-3", "digest", "sport.front_flip", "请确认"))
        stop = manager.accept_gesture(GestureSample("palm_up", 0.1, True, 1.0))
        self.assertEqual(stop.status, ConfirmationStatus.CANCELLED)
        self.assertEqual(stopped, ["gesture_global_stop"])

    def test_runtime_rechecks_review_condition_before_and_after_dispatch(self):
        table = DirectionalEvidenceTable()
        table.update("rear", 2.0, source="sensor", negative=True)
        supervisor = LocalSafetySupervisor("robot-sim-basic", evidence_table=table)
        supervisor.heartbeat()
        condition = ExecutionCondition(
            "rear_clearance",
            evidence=EvidenceRequirement("rear", quantity=0, range_m=1.0, valid_for_seconds=10),
        )
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            safety_reviewer=AllowWithCondition(condition),
            safety_supervisor=supervisor,
        )
        result = runtime.run_text("/base.move_distance distance_m=0.2 max_speed_mps=0.1")
        self.assertEqual(result.task.status.value, "completed")


if __name__ == "__main__":
    unittest.main()
