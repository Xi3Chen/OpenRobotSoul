import unittest
import time
from datetime import timedelta
from dataclasses import replace

from robotsoul import RobotRuntime, load_profile
from robotsoul.agent import DemoAgent
from robotsoul.models import AgentIntent, Command, Feedback, FeedbackStatus, utc_now


class FixedIntentAgent:
    def __init__(self, intent):
        self.intent = intent

    def plan(self, *_args, **_kwargs):
        return self.intent


class NoIntentAgent:
    def plan(self, *_args, **_kwargs):
        return None


class RuntimeContractTests(unittest.TestCase):
    def test_basic_profile_runs_registered_skill_to_completion(self):
        result = RobotRuntime.from_profile(load_profile("simulator-basic")).run_text("请抬头")

        self.assertEqual(result.task.status, "completed")
        self.assertTrue(result.task.task_id)
        self.assertTrue(result.task.trace_id)
        self.assertEqual(result.feedback[-1].status, "succeeded")
        self.assertTrue(result.evaluation.success)
        self.assertTrue(all(item.task_id == result.task.task_id for item in result.feedback))
        self.assertTrue(all(item.trace_id == result.task.trace_id for item in result.feedback))
        self.assertEqual(
            [item.status for item in result.feedback],
            ["accepted", "running", "succeeded"],
        )

    def test_each_run_has_distinct_task_and_trace_ids(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))

        first = runtime.run_text("请抬头")
        second = runtime.run_text("请抬头")

        self.assertNotEqual(first.task.task_id, second.task.task_id)
        self.assertNotEqual(first.task.trace_id, second.task.trace_id)

    def test_profiles_expose_different_capabilities(self):
        basic = RobotRuntime.from_profile(load_profile("simulator-basic"))
        compact = RobotRuntime.from_profile(load_profile("simulator-compact"))

        self.assertIn("base.move_distance", basic.available_tools())
        self.assertNotIn("base.move_distance", compact.available_tools())

    def test_discovered_adapter_capability_cannot_widen_profile_limits(self):
        profile = load_profile("simulator-basic")
        adapter = RobotRuntime.from_profile(profile).adapter
        head_capability = adapter.capabilities["head.look"]
        wider_parameters = {
            name: replace(parameter, minimum=-180 if name == "yaw_deg" else parameter.minimum,
                          maximum=180 if name == "yaw_deg" else parameter.maximum)
            for name, parameter in head_capability.parameters.items()
        }
        adapter.capabilities["head.look"] = replace(head_capability, parameters=wider_parameters)

        runtime = RobotRuntime.from_profile(profile, agent=DemoAgent(), adapter=adapter)

        self.assertEqual(runtime.capabilities["head.look"].parameters["yaw_deg"].minimum, -45)
        self.assertEqual(runtime.capabilities["head.look"].parameters["yaw_deg"].maximum, 45)

    def test_unsupported_skill_from_agent_is_rejected_without_execution(self):
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=FixedIntentAgent(AgentIntent("base.fly", {})),
        )

        result = runtime.run_text("飞起来")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(result.feedback, ())
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_out_of_range_parameters_from_agent_are_rejected_without_execution(self):
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=FixedIntentAgent(
                AgentIntent("head.look", {"yaw_deg": 999, "pitch_deg": 0})
            ),
        )

        result = runtime.run_text("看向目标")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(result.feedback, ())
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_non_finite_numeric_parameter_is_rejected_without_execution(self):
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=FixedIntentAgent(
                AgentIntent("head.look", {"yaw_deg": float("nan"), "pitch_deg": 0})
            ),
        )

        result = runtime.run_text("看向目标")

        self.assertEqual(result.task.status, "failed")
        self.assertIn("finite", result.task.error)
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_compact_profile_rejects_out_of_range_parameters_from_agent(self):
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-compact"),
            agent=FixedIntentAgent(
                AgentIntent("head.look", {"yaw_deg": 50, "pitch_deg": 0})
            ),
        )

        result = runtime.run_text("看向目标")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_explicit_move_distance_over_limit_is_not_silently_clamped(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))

        result = runtime.run_text("前进 1 米")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_move_speed_is_not_mistaken_for_distance(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))

        result = runtime.run_text("前进 0.1 米每秒")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_adapter_deduplicates_command_id(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        command = Command(
            command_id="command-1",
            task_id="task-idempotent",
            trace_id="trace-idempotent",
            robot_id=runtime.profile.robot_id,
            skill="head.look",
            parameters={"yaw_deg": 10, "pitch_deg": 0},
            deadline=utc_now() + timedelta(seconds=2),
        )

        first = runtime.adapter.execute(command)
        second = runtime.adapter.execute(command)

        self.assertEqual(first[-1].status, "succeeded")
        self.assertEqual(second[-1].status, "succeeded")
        self.assertEqual(len(runtime.adapter.executed_commands), 1)

    def test_command_id_cannot_be_reused_for_different_parameters(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        command = Command(
            command_id="command-1",
            task_id="task-idempotent",
            trace_id="trace-idempotent",
            robot_id=runtime.profile.robot_id,
            skill="head.look",
            parameters={"yaw_deg": 10, "pitch_deg": 0},
            deadline=utc_now() + timedelta(seconds=2),
        )
        altered_command = Command(
            **{**command.__dict__, "parameters": {"yaw_deg": 20, "pitch_deg": 0}}
        )

        runtime.adapter.execute(command)
        result = runtime.adapter.execute(altered_command)

        self.assertEqual(result[-1].status, "failed")
        self.assertEqual(result[-1].error_code, "idempotency_conflict")
        self.assertEqual(len(runtime.adapter.executed_commands), 1)

    def test_agent_without_intent_fails_task_safely(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"), agent=NoIntentAgent())

        result = runtime.run_text("做点什么")

        self.assertEqual(result.task.status, "failed")
        self.assertIn("No supported skill", result.task.error)
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_negated_motion_request_does_not_trigger_a_skill(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))

        result = runtime.run_text("不要移动")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_malformed_agent_intent_fails_without_escaping_runtime(self):
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=FixedIntentAgent(AgentIntent("head.look", None)),
        )

        result = runtime.run_text("看向目标")

        self.assertEqual(result.task.status, "failed")
        self.assertIn("invalid skill parameters", result.task.error)
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_custom_adapter_requires_explicit_agent(self):
        profile = load_profile("simulator-basic")
        adapter = RobotRuntime.from_profile(profile).adapter

        with self.assertRaisesRegex(ValueError, "explicit AgentProvider"):
            RobotRuntime.from_profile(profile, adapter=adapter)

    def test_adapter_robot_id_mismatch_is_rejected(self):
        profile = load_profile("simulator-basic")
        adapter = RobotRuntime.from_profile(profile).adapter
        adapter.robot_id = "another-robot"

        with self.assertRaisesRegex(RuntimeError, "robot ID"):
            RobotRuntime.from_profile(profile, agent=NoIntentAgent(), adapter=adapter)

    def test_accepted_feedback_alone_does_not_complete_task(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))

        def accepted_only(command):
            return (
                Feedback(
                    feedback_id="feedback-accepted",
                    command_id=command.command_id,
                    task_id=command.task_id,
                    trace_id=command.trace_id,
                    status=FeedbackStatus.ACCEPTED,
                    timestamp=utc_now(),
                ),
            )

        runtime.adapter.execute = accepted_only
        result = runtime.run_text("请抬头")

        self.assertEqual(result.task.status, "failed")
        self.assertFalse(result.evaluation and result.evaluation.success)

    def test_adapter_feedback_after_deadline_does_not_complete_task(self):
        profile = load_profile("simulator-basic")
        short_capabilities = tuple(
            replace(capability, timeout_ms=1) if capability.name == "head.look" else capability
            for capability in profile.capabilities
        )
        profile = replace(profile, capabilities=short_capabilities)
        adapter = RobotRuntime.from_profile(profile).adapter
        execute = adapter.execute

        def delayed_execute(command):
            time.sleep(0.01)
            return execute(command)

        adapter.execute = delayed_execute
        runtime = RobotRuntime.from_profile(profile, agent=DemoAgent(), adapter=adapter)

        result = runtime.run_text("请抬头")

        self.assertEqual(result.task.status, "failed")
        self.assertIn("deadline", result.task.error)


if __name__ == "__main__":
    unittest.main()
