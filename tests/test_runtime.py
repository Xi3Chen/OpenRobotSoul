import unittest
import time
from threading import Thread
from datetime import timedelta
from dataclasses import replace

from robotsoul import RobotRuntime, load_profile
from robotsoul.agent import DemoAgent
from robotsoul.models import AgentIntent, Command, Feedback, FeedbackStatus, Observation, utc_now


class FixedIntentAgent:
    def __init__(self, intent):
        self.intent = intent

    def plan(self, *_args, **_kwargs):
        return self.intent


class NoIntentAgent:
    def plan(self, *_args, **_kwargs):
        return None


class ObservationAgent(FixedIntentAgent):
    def __init__(self, intent):
        super().__init__(intent)
        self.observations = ()

    def plan(self, _text, _capabilities, observations=()):
        self.observations = observations
        return self.intent


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

    def test_caller_supplied_task_id_cannot_be_reused(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        runtime.run_text("请抬头", task_id="caller-task")

        with self.assertRaisesRegex(ValueError, "already been used"):
            runtime.run_text("请抬头", task_id="caller-task")

    def test_profiles_expose_different_capabilities(self):
        basic = RobotRuntime.from_profile(load_profile("simulator-basic"))
        compact = RobotRuntime.from_profile(load_profile("simulator-compact"))

        self.assertIn("base.move_distance", basic.available_tools())
        self.assertNotIn("base.move_distance", compact.available_tools())

    def test_only_fresh_operational_observations_reach_agent(self):
        profile = load_profile("simulator-basic")
        agent = ObservationAgent(AgentIntent("head.look", {"yaw_deg": 0, "pitch_deg": 10}))
        runtime = RobotRuntime.from_profile(profile, agent=agent)
        now = utc_now()
        fresh = Observation(
            event_id="event-fresh",
            robot_id=profile.robot_id,
            device_id="microphone-1",
            event_type="speech.transcript",
            observed_at=now,
            received_at=now,
            payload={"text": "抬头"},
            source="fake-test",
            ttl_ms=5000,
            privacy_class="operational",
        )
        private = replace(fresh, event_id="event-private", privacy_class="personal")
        stale = replace(
            fresh,
            event_id="event-stale",
            observed_at=now - timedelta(seconds=10),
            received_at=now - timedelta(seconds=10),
        )
        wrong_robot = replace(fresh, event_id="event-other-robot", robot_id="other-robot")
        for observation in (fresh, private, stale, wrong_robot):
            runtime.adapter.publish_observation(observation)

        result = runtime.run_text("请抬头")

        self.assertEqual(result.task.status, "completed")
        self.assertEqual([item.event_id for item in agent.observations], ["event-fresh"])
        rejected = [
            (item.event_type, item.details["reason"])
            for item in result.history
            if item.event_type in {"observation.rejected", "observation.withheld"}
        ]
        self.assertCountEqual(
            rejected,
            [
                ("observation.withheld", "privacy_policy"),
                ("observation.rejected", "expired_or_future"),
                ("observation.rejected", "robot_id_mismatch"),
            ],
        )

    def test_duplicate_observation_event_id_is_not_forwarded_twice(self):
        profile = load_profile("simulator-basic")
        agent = ObservationAgent(AgentIntent("head.look", {"yaw_deg": 0, "pitch_deg": 10}))
        runtime = RobotRuntime.from_profile(profile, agent=agent)
        now = utc_now()
        observation = Observation(
            event_id="event-once",
            robot_id=profile.robot_id,
            device_id="sensor-1",
            event_type="person.presence",
            observed_at=now,
            received_at=now,
            payload={"present": True},
            source="fake-test",
            ttl_ms=5000,
        )

        runtime.adapter.publish_observation(observation)
        runtime.run_text("请抬头")
        runtime.adapter.publish_observation(observation)
        second_result = runtime.run_text("请抬头")

        self.assertEqual(agent.observations, ())
        self.assertTrue(
            any(
                item.event_type == "observation.rejected"
                and item.details == {"event_id": "event-once", "reason": "duplicate"}
                for item in second_result.history
            )
        )

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

    def test_adapter_can_cancel_a_command_before_execution_starts(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        command = Command(
            command_id="command-before-start",
            task_id="task-before-start",
            trace_id="trace-before-start",
            robot_id=runtime.profile.robot_id,
            skill="head.look",
            parameters={"yaw_deg": 10, "pitch_deg": 0},
            deadline=utc_now() + timedelta(seconds=2),
        )

        cancellation = runtime.adapter.cancel(command)
        feedback = runtime.adapter.execute(command)

        self.assertEqual(cancellation.status, "cancelled")
        self.assertEqual(feedback[-1].status, "cancelled")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_pre_start_cancellation_is_bound_to_full_command(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        command = Command(
            command_id="command-before-start",
            task_id="task-before-start",
            trace_id="trace-before-start",
            robot_id=runtime.profile.robot_id,
            skill="head.look",
            parameters={"yaw_deg": 10, "pitch_deg": 0},
            deadline=utc_now() + timedelta(seconds=2),
        )
        altered_command = replace(command, parameters={"yaw_deg": 20, "pitch_deg": 0})
        runtime.adapter.cancel(command)

        conflict = runtime.adapter.execute(altered_command)
        cancelled = runtime.adapter.execute(command)

        self.assertEqual(conflict[-1].error_code, "idempotency_conflict")
        self.assertEqual(cancelled[-1].status, "cancelled")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_cancel_racing_with_adapter_start_is_not_lost(self):
        profile = load_profile("simulator-basic")
        adapter = RobotRuntime.from_profile(profile).adapter
        runtime = RobotRuntime.from_profile(
            profile,
            agent=FixedIntentAgent(AgentIntent("head.look", {"yaw_deg": 0, "pitch_deg": 10})),
            adapter=adapter,
        )
        execute = adapter.execute

        def cancel_before_start(command):
            confirmation = runtime.cancel_task(command.task_id)
            self.assertEqual(confirmation.status, "cancelled")
            return execute(command)

        adapter.execute = cancel_before_start
        result = runtime.run_text("请抬头", task_id="task-cancel-race")

        self.assertEqual(result.task.status, "cancelled")
        self.assertEqual(adapter.executed_commands, [])

    def test_audit_does_not_store_raw_task_or_agent_parameter_values(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        private_text = "说 我的门禁密码是 1234"

        result = runtime.run_text(private_text)
        serialized_history = repr(result.history)

        self.assertNotIn(private_text, serialized_history)
        self.assertNotIn("我的门禁密码", serialized_history)
        self.assertIn("input_chars", serialized_history)

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

    def test_feedback_with_naive_timestamp_is_rejected(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))

        def naive_feedback(command):
            return (
                Feedback(
                    feedback_id="feedback-failed",
                    command_id=command.command_id,
                    task_id=command.task_id,
                    trace_id=command.trace_id,
                    status=FeedbackStatus.FAILED,
                    timestamp=utc_now().replace(tzinfo=None),
                    message="driver failed",
                ),
            )

        runtime.adapter.execute = naive_feedback
        result = runtime.run_text("请抬头")

        self.assertEqual(result.task.status, "failed")
        self.assertIn("invalid timestamp", result.task.error)

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

    def test_runtime_cancellation_requires_adapter_stop_confirmation(self):
        profile = load_profile("simulator-basic")
        adapter = RobotRuntime.from_profile(profile).adapter
        adapter.execution_delay_seconds = 0.5
        runtime = RobotRuntime.from_profile(
            profile,
            agent=FixedIntentAgent(AgentIntent("head.look", {"yaw_deg": 0, "pitch_deg": 10})),
            adapter=adapter,
        )
        results = []
        worker = Thread(target=lambda: results.append(runtime.run_text("请抬头", task_id="task-cancel")))
        worker.start()
        self.assertTrue(adapter.execution_started.wait(timeout=1))

        confirmation = runtime.cancel_task("task-cancel")
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(confirmation.status, "cancelled")
        self.assertEqual(results[0].task.status, "cancelled")
        self.assertEqual(results[0].feedback[-1].status, "cancelled")
        self.assertTrue(any(item.event_type == "command.cancel_confirmation" for item in results[0].history))

    def test_non_interruptible_skill_rejects_runtime_cancellation(self):
        profile = load_profile("simulator-basic")
        capabilities = tuple(
            replace(capability, interruptible=False) if capability.name == "head.look" else capability
            for capability in profile.capabilities
        )
        profile = replace(profile, capabilities=capabilities)
        adapter = RobotRuntime.from_profile(profile).adapter
        adapter.execution_delay_seconds = 0.1
        runtime = RobotRuntime.from_profile(
            profile,
            agent=FixedIntentAgent(AgentIntent("head.look", {"yaw_deg": 0, "pitch_deg": 10})),
            adapter=adapter,
        )
        results = []
        worker = Thread(target=lambda: results.append(runtime.run_text("请抬头", task_id="task-not-cancelable")))
        worker.start()
        self.assertTrue(adapter.execution_started.wait(timeout=1))

        confirmation = runtime.cancel_task("task-not-cancelable")
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(confirmation.status, "failed")
        self.assertEqual(confirmation.error_code, "not_interruptible")
        self.assertEqual(results[0].task.status, "completed")


if __name__ == "__main__":
    unittest.main()
