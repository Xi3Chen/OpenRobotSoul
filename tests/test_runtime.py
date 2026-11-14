import unittest
import time
import json
import tempfile
from threading import Thread
from datetime import timedelta
from dataclasses import replace
from pathlib import Path

from robotsoul import InMemoryToolCallAuditSink, JsonlToolCallAuditSink, RobotRuntime, load_profile
from robotsoul.agent import DemoAgent
from robotsoul.models import (
    AgentIntent, CapabilitySpec, Command, Feedback, FeedbackStatus, InvocationContext,
    InvocationSource, Observation, ParameterSpec, ReviewVerdict, RiskLevel,
    SafetyReviewResult, utc_now,
)


class FixedIntentAgent:
    def __init__(self, intent):
        self.intent = intent

    def plan(self, *_args, **_kwargs):
        return self.intent


class NoIntentAgent:
    def plan(self, *_args, **_kwargs):
        return None


class AllowSafetyReviewer:
    def __init__(self, verdict=ReviewVerdict.ALLOW, alter_digest=False):
        self.verdict = verdict
        self.alter_digest = alter_digest
        self.proposals = []

    def review(self, proposal):
        self.proposals.append(proposal)
        digest = "bad" if self.alter_digest else proposal.arguments_digest
        return SafetyReviewResult(self.verdict, digest, "Reviewed exact proposal")


class MutatingSafetyReviewer:
    def review(self, proposal):
        proposal.command.parameters["distance_m"] = 0.4
        return SafetyReviewResult(ReviewVerdict.ALLOW, proposal.arguments_digest, "Reviewed")


class ObservationAgent(FixedIntentAgent):
    def __init__(self, intent):
        super().__init__(intent)
        self.observations = ()

    def plan(self, _text, _capabilities, observations=()):
        self.observations = observations
        return self.intent


class RuntimeContractTests(unittest.TestCase):
    def test_close_releases_adapter_once(self):
        from unittest.mock import Mock

        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        close = runtime.adapter.close

        runtime.adapter.close = Mock(wraps=close)
        runtime.close()
        runtime.close()

        runtime.adapter.close.assert_called_once_with()

    def test_preempted_voice_task_does_not_execute(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        calls = []
        execute = runtime.adapter.execute
        runtime.adapter.execute = lambda command: calls.append(command.skill) or execute(command)
        runtime.preempt_task("voice-task")
        result = runtime.run_text(
            "看一下",
            task_id="voice-task",
            _intent=AgentIntent("head.look", {"yaw_deg": 0, "pitch_deg": 10}),
        )
        self.assertEqual(calls, [])
        self.assertEqual(result.task.status, "failed")
        self.assertEqual(result.task.error, "Voice command was replaced")

    def test_basic_profile_runs_registered_skill_to_completion(self):
        result = RobotRuntime.from_profile(load_profile("simulator-basic")).run_text("/head.look yaw_deg=0 pitch_deg=10")

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

    def test_run_text_records_the_decision_chain(self):
        from robotsoul.runlog import configure, current

        with tempfile.TemporaryDirectory() as tmp:
            previous = current().directory
            configure(tmp)
            try:
                completed = RobotRuntime.from_profile(load_profile("simulator-basic")).run_text(
                    "/head.look yaw_deg=0 pitch_deg=10"
                )
                denied = RobotRuntime.from_profile(
                    load_profile("simulator-basic"),
                    agent=NoIntentAgent(),
                ).run_text("站起来")
                reviewed = RobotRuntime.from_profile(
                    load_profile("simulator-basic"),
                    safety_reviewer=AllowSafetyReviewer(ReviewVerdict.DENY),
                ).run_text("/base.move_distance distance_m=0.2 max_speed_mps=0.1")
                lines = current().recent(80)
            finally:
                configure(previous)

        self.assertEqual(completed.task.status, "completed")
        self.assertTrue(any(f"id={completed.task.task_id} 收到指令：/head.look" in line for line in lines))
        self.assertTrue(any(f"id={completed.task.task_id} 使用直接指令" in line for line in lines))
        self.assertTrue(any(f"id={completed.task.task_id} 已选择技能 head.look" in line for line in lines))
        self.assertTrue(any(f"id={completed.task.task_id} 开始执行 技能=head.look" in line for line in lines))
        self.assertTrue(any(f"id={completed.task.task_id} 执行完成 技能=head.look" in line for line in lines))
        self.assertEqual(denied.task.status, "failed")
        self.assertTrue(any(f"id={denied.task.task_id} 收到指令：站起来" in line for line in lines))
        self.assertTrue(any(f"id={denied.task.task_id} 等待语言决策" in line for line in lines))
        self.assertTrue(any(f"id={denied.task.task_id} 指令未执行 原因=No supported skill matched the request" in line for line in lines))
        self.assertEqual(reviewed.task.status, "failed")
        self.assertTrue(any(f"id={reviewed.task.task_id} 开始安全复核 技能=base.move_distance" in line for line in lines))
        self.assertTrue(any(f"id={reviewed.task.task_id} 指令未执行 原因=Reviewed exact proposal" in line for line in lines))
        self.assertFalse(any(f"id={reviewed.task.task_id} 开始执行" in line for line in lines))

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

    def test_confirm_capability_requires_matching_ai_review_before_execution(self):
        profile = load_profile("simulator-basic")
        reviewer = AllowSafetyReviewer()
        runtime = RobotRuntime.from_profile(profile, safety_reviewer=reviewer)

        result = runtime.run_text("/base.move_distance distance_m=0.2 max_speed_mps=0.1")

        self.assertEqual(result.task.status, "completed")
        self.assertEqual(len(reviewer.proposals), 1)
        self.assertEqual(reviewer.proposals[0].risk_level, RiskLevel.CONFIRM)
        self.assertEqual(runtime.adapter.executed_commands[0].parameters["distance_m"], 0.2)
        self.assertTrue(any(item.event_type == "safety.review.completed" for item in result.history))

    def test_confirm_denial_reports_the_review_rationale(self):
        profile = load_profile("simulator-basic")
        runtime = RobotRuntime.from_profile(profile, safety_reviewer=AllowSafetyReviewer(ReviewVerdict.DENY))

        result = runtime.run_text("/base.move_distance distance_m=0.2 max_speed_mps=0.1")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(result.task.error, "Reviewed exact proposal")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_confirm_review_wait_does_not_use_the_action_timeout(self):
        profile = load_profile("simulator-basic")
        capabilities = tuple(
            replace(capability, timeout_ms=50) if capability.name == "base.move_distance" else capability
            for capability in profile.capabilities
        )
        profile = replace(profile, capabilities=capabilities)

        class SlowAllow:
            def review(self, proposal):
                time.sleep(0.08)
                return SafetyReviewResult(ReviewVerdict.ALLOW, proposal.arguments_digest, "ok")

        runtime = RobotRuntime.from_profile(profile, safety_reviewer=SlowAllow())

        result = runtime.run_text("/base.move_distance distance_m=0.2 max_speed_mps=0.1")

        self.assertEqual(result.task.status, "completed")
        self.assertEqual(len(runtime.adapter.executed_commands), 1)

    def test_mismatched_review_names_the_selected_skill(self):
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            safety_reviewer=AllowSafetyReviewer(alter_digest=True),
        )

        result = runtime.run_text("/base.move_distance distance_m=0.2 max_speed_mps=0.1")

        self.assertEqual(result.task.status, "failed")
        self.assertIn("base.move_distance", result.task.error)
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_confirm_capability_fails_closed_without_valid_review(self):
        profile = load_profile("simulator-basic")
        for reviewer in (None, AllowSafetyReviewer(ReviewVerdict.DENY), AllowSafetyReviewer(alter_digest=True)):
            runtime = RobotRuntime.from_profile(profile, safety_reviewer=reviewer)
            result = runtime.run_text("/base.move_distance distance_m=0.2 max_speed_mps=0.1")
            self.assertEqual(result.task.status, "failed")
            self.assertEqual(runtime.adapter.executed_commands, [])

    def test_reviewer_cannot_mutate_parameters_after_review(self):
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"), safety_reviewer=MutatingSafetyReviewer()
        )

        result = runtime.run_text("/base.move_distance distance_m=0.2 max_speed_mps=0.1")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_dashboard_skill_invocation_returns_task_result_and_uses_policy(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        context = InvocationContext(InvocationSource.DASHBOARD, "operator-1", "session-1", "operator")

        result = runtime.invoke_skill("head.look", {"yaw_deg": 0, "pitch_deg": 10}, invocation_context=context)

        self.assertEqual(result.task.status, "completed")
        self.assertIn("head.look", runtime.dashboard_tools())

    def test_validate_skill_request_checks_policy_and_parameters_without_execution(self):
        profile = load_profile("simulator-basic")
        high = CapabilitySpec(
            "test.high", {"enabled": ParameterSpec("boolean")}, risk_level=RiskLevel.HIGH,
            allowed_callers=("dashboard",), requires_operator_confirmation=True,
        )
        profile = replace(profile, capabilities=(*profile.capabilities, high))
        runtime = RobotRuntime.from_profile(profile)

        self.assertIsNotNone(runtime.validate_skill_request(
            "test.high", {"enabled": True}, InvocationContext(InvocationSource.AGENT)
        ))
        self.assertIsNotNone(runtime.validate_skill_request(
            "test.high", {"enabled": True}, InvocationContext(InvocationSource.DASHBOARD, role="viewer", principal_id="u", session_id="s")
        ))
        self.assertIsNotNone(runtime.validate_skill_request(
            "test.high", {"enabled": 1}, InvocationContext(InvocationSource.DASHBOARD, role="operator", principal_id="u", session_id="s")
        ))
        self.assertIsNone(runtime.validate_skill_request(
            "test.high", {"enabled": True}, InvocationContext(InvocationSource.DASHBOARD, role="operator", principal_id="u", session_id="s")
        ))
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_high_risk_skill_requires_dashboard_operator_and_confirmation(self):
        profile = load_profile("simulator-basic")
        high = CapabilitySpec(
            "test.high",
            {"enable": ParameterSpec("boolean")},
            risk_level=RiskLevel.HIGH,
            allowed_callers=("dashboard",),
            requires_operator_confirmation=True,
        )
        profile = replace(profile, capabilities=(high,), required_capabilities=())
        runtime = RobotRuntime.from_profile(profile)
        self.assertNotIn("test.high", runtime.available_tools())
        self.assertIn("test.high", runtime.dashboard_tools())
        parameters = {"enable": False}

        agent_result = runtime.run_text("high call", task_id="high-agent", _intent=AgentIntent("test.high", parameters))
        dashboard_context = InvocationContext(
            InvocationSource.DASHBOARD, "operator-1", "session-1", "operator", operator_confirmed=True
        )
        unconfirmed = runtime.invoke_skill("test.high", parameters, invocation_context=dashboard_context, task_id="high-unconfirmed")
        confirmed = runtime.invoke_skill("test.high", parameters, invocation_context=dashboard_context, operator_confirmed=True, task_id="high-confirmed")

        self.assertEqual(agent_result.task.status, "failed")
        self.assertEqual(unconfirmed.task.status, "failed")
        self.assertEqual(confirmed.task.status, "completed")
        self.assertEqual(len(runtime.adapter.executed_commands), 1)
        audit = next(item for item in confirmed.history if item.event_type == "operator.confirmed")
        self.assertEqual(audit.details["actor_hash"], __import__("hashlib").sha256(b"operator-1").hexdigest())
        self.assertNotIn("session_id", audit.details)

    def test_tool_audit_includes_invocation_source_risk_digest_and_actor_hash(self):
        sink = InMemoryToolCallAuditSink()
        reviewer = AllowSafetyReviewer()
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"), safety_reviewer=reviewer, audit_sink=sink
        )
        context = InvocationContext(
            InvocationSource.DASHBOARD, "operator-17", "session-secret", "operator", operator_confirmed=True
        )

        result = runtime.invoke_skill(
            "base.move_distance", {"distance_m": 0.2, "max_speed_mps": 0.1},
            invocation_context=context, user_input="move",
        )

        self.assertEqual(result.task.status, "completed")
        for event in sink.events:
            self.assertEqual(event["risk_level"], "confirm")
            self.assertEqual(event["invocation_source"], "dashboard")
            self.assertTrue(event["review_digest"])
            self.assertTrue(event["actor_hash"])
            self.assertNotIn("session-secret", repr(event))

    def test_dashboard_operator_runs_confirm_skill_without_ai_reviewer(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        result = runtime.invoke_skill(
            "base.move_distance",
            {"distance_m": 0.1, "max_speed_mps": 0.1},
            invocation_context=InvocationContext(
                InvocationSource.DASHBOARD, "operator-1", "session-1", "operator"
            ),
        )

        self.assertEqual(result.task.status, "completed")
        review = next(item for item in result.history if item.event_type == "safety.review.completed")
        self.assertEqual(review.details["reviewer"], "dashboard_operator")
        self.assertEqual(review.details["verdict"], "allow")
        self.assertEqual(len(runtime.adapter.executed_commands), 1)

    def test_agent_confirm_skill_still_requires_reviewer(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        result = runtime.invoke_skill(
            "base.move_distance",
            {"distance_m": 0.1, "max_speed_mps": 0.1},
            invocation_context=InvocationContext(InvocationSource.AGENT),
        )

        self.assertEqual(result.task.status, "failed")
        self.assertIn("Safety reviewer unavailable", result.task.error)
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_boolean_parameter_rejects_integer(self):
        profile = load_profile("simulator-basic")
        capability = CapabilitySpec("test.bool", {"enabled": ParameterSpec("boolean")})
        profile = replace(profile, capabilities=(capability,), required_capabilities=())
        runtime = RobotRuntime.from_profile(profile)

        result = runtime.invoke_skill(
            "test.bool",
            {"enabled": 1},
            invocation_context=InvocationContext(InvocationSource.DASHBOARD, "op", "sess", "operator"),
        )

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_adapter_rejects_confirm_command_without_review_binding(self):
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"), safety_reviewer=AllowSafetyReviewer()
        )
        command = Command(
            command_id="unreviewed-confirm",
            task_id="unreviewed-confirm-task",
            trace_id="unreviewed-confirm-trace",
            robot_id=runtime.profile.robot_id,
            skill="base.move_distance",
            parameters={"distance_m": 0.1, "max_speed_mps": 0.1},
            deadline=utc_now() + timedelta(seconds=2),
            risk_level=RiskLevel.CONFIRM,
            invocation_source=InvocationSource.AGENT,
        )

        feedback = runtime.adapter.execute(command)

        self.assertEqual(feedback[-1].status, "failed")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_command_review_digest_binds_task_policy_risk_and_arguments(self):
        from robotsoul.models import command_proposal_digest

        command = Command(
            command_id="digest-command", task_id="digest-task", trace_id="digest-trace",
            robot_id="robot", skill="skill", parameters={"enabled": True},
            deadline=utc_now() + timedelta(seconds=1), risk_level=RiskLevel.CONFIRM,
            invocation_source=InvocationSource.AGENT,
        )
        base = command_proposal_digest(command, "profile-v1")
        self.assertNotEqual(base, command_proposal_digest(replace(command, task_id="other-task"), "profile-v1"))
        self.assertNotEqual(base, command_proposal_digest(replace(command, trace_id="other-trace"), "profile-v1"))
        self.assertNotEqual(base, command_proposal_digest(replace(command, parameters={"enabled": False}), "profile-v1"))
        self.assertNotEqual(base, command_proposal_digest(replace(command, risk_level=RiskLevel.NORMAL), "profile-v1"))
        self.assertNotEqual(base, command_proposal_digest(command, "profile-v2"))

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

        result = runtime.run_text("/base.move_distance distance_m=1 max_speed_mps=0.1")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(runtime.adapter.executed_commands, [])

    def test_slash_suffix_selects_the_registered_skill(self):
        from robotsoul.agent import resolve_slash_command

        capabilities = {"sport.stand_up": CapabilitySpec("sport.stand_up", {})}
        intent = resolve_slash_command("/stand-up", capabilities)

        self.assertEqual(intent.skill, "sport.stand_up")
        self.assertEqual(intent.parameters, {})
        self.assertIsNone(resolve_slash_command("站起来", capabilities))

    def test_plain_text_is_not_locally_keyword_matched(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))

        result = runtime.run_text("前进 0.1 米每秒")

        self.assertEqual(result.task.status, "failed")
        self.assertIn("No supported skill", result.task.error)
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

    def test_tool_call_audit_records_arguments_and_feedback_without_text_values(self):
        profile = load_profile("simulator-basic")
        sink = InMemoryToolCallAuditSink()
        secret_text = "我的门禁密码是 1234"
        runtime = RobotRuntime.from_profile(
            profile,
            agent=FixedIntentAgent(AgentIntent("speech.say", {"text": secret_text})),
            audit_sink=sink,
        )

        result = runtime.run_text("触发一条语音工具调用")

        self.assertEqual(result.task.status, "completed")
        self.assertEqual([event["phase"] for event in sink.events], ["started", "completed"])
        self.assertEqual(sink.events[0]["tool"], "speech.say")
        self.assertEqual(sink.events[0]["arguments"]["text"], {"redacted": True, "length": len(secret_text)})
        self.assertEqual(sink.events[1]["response"]["feedback"][-1]["status"], "succeeded")
        self.assertEqual(sink.events[0]["call_id"], sink.events[1]["call_id"])
        self.assertEqual(sink.events[0]["command_id"], sink.events[1]["command_id"])
        self.assertGreaterEqual(sink.events[1]["duration_ms"], 0)
        self.assertNotIn(secret_text, repr(sink.events))
        self.assertTrue(any(event.event_type == "tool.call.started" for event in result.history))
        self.assertTrue(any(event.event_type == "tool.call.completed" for event in result.history))

    def test_tool_call_audit_records_adapter_exceptions(self):
        sink = InMemoryToolCallAuditSink()
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=FixedIntentAgent(AgentIntent("head.look", {"yaw_deg": 0, "pitch_deg": 10})),
            audit_sink=sink,
        )

        def fail_execute(_command):
            raise RuntimeError("bridge unavailable")

        runtime.adapter.execute = fail_execute
        result = runtime.run_text("请抬头")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(sink.events[-1]["phase"], "completed")
        self.assertEqual(sink.events[-1]["outcome"], "raised")
        self.assertEqual(sink.events[-1]["error"]["type"], "RuntimeError")
        self.assertIn("bridge unavailable", sink.events[-1]["error"]["message"])

    def test_tool_call_audit_redacts_task_text_echoed_by_adapter_exception(self):
        private_text = "我的保险柜密码是 9876"
        sink = InMemoryToolCallAuditSink()
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=FixedIntentAgent(AgentIntent("head.look", {"yaw_deg": 0, "pitch_deg": 10})),
            audit_sink=sink,
        )

        def fail_with_task_echo(_command):
            raise RuntimeError(f"adapter rejected request: {private_text}")

        runtime.adapter.execute = fail_with_task_echo
        runtime.run_text(private_text)

        self.assertNotIn(private_text, repr(sink.events))
        self.assertIn("[REDACTED]", sink.events[-1]["error"]["message"])

    def test_tool_call_audit_keeps_numeric_command_arguments(self):
        sink = InMemoryToolCallAuditSink()
        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=FixedIntentAgent(AgentIntent("head.look", {"yaw_deg": 12, "pitch_deg": -3})),
            audit_sink=sink,
        )

        runtime.run_text("看向左侧")

        self.assertEqual(sink.events[0]["arguments"], {"yaw_deg": 12, "pitch_deg": -3})
        self.assertEqual(sink.events[0]["call_id"], sink.events[1]["call_id"])

    def test_tool_call_jsonl_sink_persists_parseable_events(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "calls.jsonl"
            sink = JsonlToolCallAuditSink(log_path)
            runtime = RobotRuntime.from_profile(load_profile("simulator-basic"), audit_sink=sink)

            result = runtime.run_text("/head.look yaw_deg=0 pitch_deg=10")

            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result.task.status, "completed")
            self.assertEqual([event["phase"] for event in events], ["started", "completed"])
            self.assertEqual(events[0]["trace_id"], result.task.trace_id)
            self.assertEqual(events[1]["task_id"], result.task.task_id)

    def test_tool_is_not_executed_when_start_audit_fails(self):
        class FailingSink:
            def write(self, _event):
                raise OSError("audit storage is unavailable")

        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"), audit_sink=FailingSink())

        result = runtime.run_text("/head.look yaw_deg=0 pitch_deg=10")

        self.assertEqual(result.task.status, "failed")
        self.assertEqual(runtime.adapter.executed_commands, [])
        self.assertTrue(any(event.event_type == "tool.audit_write_failed" for event in result.history))

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
        result = runtime.run_text("/head.look yaw_deg=0 pitch_deg=10")

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
        result = runtime.run_text("/head.look yaw_deg=0 pitch_deg=10")

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

        result = runtime.run_text("/head.look yaw_deg=0 pitch_deg=10")

        self.assertEqual(result.task.status, "failed")
        self.assertIn("deadline", result.task.error)

    def test_runtime_cancellation_requires_adapter_stop_confirmation(self):
        profile = load_profile("simulator-basic")
        adapter = RobotRuntime.from_profile(profile).adapter
        adapter.execution_delay_seconds = 0.5
        audit_sink = InMemoryToolCallAuditSink()
        runtime = RobotRuntime.from_profile(
            profile,
            agent=FixedIntentAgent(AgentIntent("head.look", {"yaw_deg": 0, "pitch_deg": 10})),
            adapter=adapter,
            audit_sink=audit_sink,
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
        cancellation_events = [event for event in audit_sink.events if event["method"] == "adapter.cancel"]
        self.assertEqual([event["phase"] for event in cancellation_events], ["started", "completed"])
        self.assertEqual(cancellation_events[0]["call_id"], cancellation_events[1]["call_id"])

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
