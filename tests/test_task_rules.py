import unittest

from robotsoul.models import AgentDecision, AgentIntent, Evaluation, FeedbackStatus, TaskStatus
from robotsoul.task_runner import TaskLimits, TaskRunner

from test_task_runner import CheckedRuntime, FakeClock, ScriptedAgent, completion


def action(skill="light", **parameters):
    return {"skill": skill, "parameters": parameters}


class WindowAgent:
    def __init__(self):
        self.contexts = []

    def decide(self, context, _capabilities):
        self.contexts.append(context)
        if not context.feedback:
            return AgentDecision("act", (AgentIntent("navigate", {"destination": "balcony"}),))
        navigation = context.feedback[0]
        if navigation.status != FeedbackStatus.SUCCEEDED:
            return AgentDecision("reject", reply="无法到达阳台。")
        inspections = [item for item in context.feedback if "window_state" in item.evidence]
        if inspections and inspections[-1].evidence["window_state"] in {"open", "closed"}:
            return AgentDecision("complete", evidence_refs=(inspections[-1].feedback_id,), evaluator="window_check")
        if len(inspections) == 3:
            return AgentDecision("clarify", reply="补看三次仍无法判断，请调整视角。")
        return AgentDecision("act", (AgentIntent("inspect", {}),))


def window_check(context, refs):
    navigation = next(item for item in context.feedback if item.message == "navigate finished")
    selected = next(item for item in context.feedback if item.feedback_id in refs)
    state = selected.evidence.get("window_state", "unknown")
    inspected = selected.evidence.get("inspection_complete") is True
    success = navigation.status == FeedbackStatus.SUCCEEDED and inspected and state in {"open", "closed"}
    return Evaluation(context.task.task_id, context.task.trace_id, success,
                      f"检查完成；窗户状态：{state}。" if success else "检查未取得确定结果。",
                      (selected.feedback_id,), 1.0 if success else 0.0)


class TaskRuleTests(unittest.TestCase):
    def runner(self, runtime, **kwargs):
        runner = TaskRunner(runtime, **kwargs)
        self.addCleanup(runner.close)
        return runner

    def rule_runtime(self, rule, *, clock=None):
        return CheckedRuntime(ScriptedAgent(AgentDecision("run_rule", rule=rule), completion), clock=clock)

    def test_window_open_and_closed_complete_after_navigation(self):
        for state in ("open", "closed"):
            with self.subTest(state=state):
                runtime = CheckedRuntime(WindowAgent())
                runtime.responses["inspect"].append({"evidence": {"inspection_complete": True, "window_state": state}})
                runner = self.runner(runtime)
                runner.register_evaluator("window_check", window_check)
                result = runner.wait_task(runner.submit_task("去阳台看看窗户有没有开", expected_evaluator="window_check"), 2)
                self.assertEqual(result.task.status, TaskStatus.COMPLETED)
                self.assertIn(state, result.reply)
                self.assertEqual([command.skill for command, _ in runtime.commands], ["navigate", "inspect"])
                self.assertTrue(result.evaluation.success)

    def test_unknown_window_has_bounded_reinspection_and_separate_question(self):
        runtime = CheckedRuntime(WindowAgent())
        runtime.responses["inspect"].extend([
            {"evidence": {"inspection_complete": True, "window_state": "unknown"}}
        ] * 3)
        runner = self.runner(runtime)
        runner.register_evaluator("window_check", window_check)
        result = runner.wait_task(runner.submit_task("检查窗户"), 2)
        self.assertEqual(result.task.status, TaskStatus.AWAITING_INPUT)
        self.assertIn("三次", result.reply)
        self.assertIsNone(result.task.error)
        self.assertEqual([command.skill for command, _ in runtime.commands], ["navigate", "inspect", "inspect", "inspect"])

    def test_failed_navigation_does_not_observe_window(self):
        runtime = CheckedRuntime(WindowAgent())
        runtime.responses["navigate"].append({"status": FeedbackStatus.FAILED, "error_code": "blocked"})
        runner = self.runner(runtime)
        result = runner.wait_task(runner.submit_task("检查窗户"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertEqual([command.skill for command, _ in runtime.commands], ["navigate"])

    def test_sequence_and_condition_use_real_result(self):
        rule = {"template": "sequence", "steps": [
            action("inspect"),
            {"template": "if_result", "field": "evidence.window_state", "equals": "open",
             "then": action("light", enabled=True), "else": action("light", enabled=False)},
        ]}
        runtime = self.rule_runtime(rule)
        runtime.responses["inspect"].append({"evidence": {"window_state": "closed"}})
        runner = self.runner(runtime)
        result = runner.wait_task(runner.submit_task("condition"), 2)
        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(runtime.commands[-1][0].parameters, {"enabled": False})

    def test_invalid_later_rule_or_permission_is_rejected_before_any_action(self):
        for last in ({"template": "arbitrary_script", "script": "danger()"}, action("missing"), action("inspect")):
            rule = {"template": "sequence", "steps": [action("navigate"), last]}
            runtime = self.rule_runtime(rule)
            runtime.forbidden.add("inspect")
            runner = self.runner(runtime)
            result = runner.wait_task(runner.submit_task("invalid"), 2)
            self.assertEqual(result.task.status, TaskStatus.FAILED)
            self.assertFalse(runtime.commands)

    def test_retry_is_explicit_bounded_and_known(self):
        rule = {"template": "retry_limited", "step": action("inspect"), "attempts": 3}
        runtime = self.rule_runtime(rule)
        runtime.responses["inspect"].extend([
            {"status": FeedbackStatus.FAILED, "error_code": "empty_frame"},
            {"status": FeedbackStatus.FAILED, "error_code": "empty_frame"}, {},
        ])
        runner = self.runner(runtime)
        self.assertEqual(runner.wait_task(runner.submit_task("retry"), 2).task.status, TaskStatus.COMPLETED)
        self.assertEqual(len(runtime.commands), 3)
        for data in (
            {"status": FeedbackStatus.FAILED, "error_code": "timeout"},
            {"status": FeedbackStatus.FAILED, "evidence": {"outcome_known": False}},
        ):
            runtime = self.rule_runtime(rule)
            runtime.responses["inspect"].append(data)
            runner = self.runner(runtime)
            self.assertEqual(runner.wait_task(runner.submit_task("uncertain retry"), 2).task.status, TaskStatus.INTERRUPTED)
            self.assertEqual(len(runtime.commands), 1)

    def test_non_retry_safe_or_more_than_three_attempts_never_dispatches(self):
        for skill, attempts in (("navigate", 2), ("inspect", 4)):
            runtime = self.rule_runtime({"template": "retry_limited", "step": action(skill), "attempts": attempts})
            runner = self.runner(runtime)
            self.assertEqual(runner.wait_task(runner.submit_task("unsafe retry"), 2).task.status, TaskStatus.FAILED)
            self.assertFalse(runtime.commands)

    def test_two_retry_rules_cannot_exceed_three_total_attempts(self):
        rule = {"template": "retry_limited", "step": action("inspect"), "attempts": 3}
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("run_rule", rule=rule),
                                             AgentDecision("run_rule", rule=rule)))
        runtime.responses["inspect"].extend([
            {"status": FeedbackStatus.FAILED, "error_code": "empty_frame"}
        ] * 4)
        runner = self.runner(runtime)
        result = runner.wait_task(runner.submit_task("exhaust retries"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertEqual(len(runtime.commands), 3)

    def test_end_timeline_skips_remaining_events(self):
        clock = FakeClock()
        rule = {"template": "timeline", "events": [
            {"at_ms": at, **action("light", pulse=at)} for at in (0, 100, 200)
        ]}
        runtime = self.rule_runtime(rule, clock=clock)
        runner = self.runner(runtime, clock=clock)
        task_id = "end-beats"
        clock.on_sleep = lambda _clock: runner.resume_task(
            task_id, event={"timeline_action": "end"}, conversation=runner.default_conversation(), event_id="end")
        result = runner.wait_task(runner.submit_task("end", task_id=task_id), 2)
        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(len(runtime.commands), 1)

    def test_nested_rules_cannot_exceed_remaining_command_budget(self):
        rule = {"template": "sequence", "steps": [
            {"template": "sequence", "steps": [action("inspect"), action("inspect")]}
        ] * 17}
        runtime = self.rule_runtime(rule)
        runner = self.runner(runtime)
        result = runner.wait_task(runner.submit_task("huge"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertFalse(runtime.commands)

    def test_128_beat_timeline_is_driven_by_playback_clock_with_two_model_calls(self):
        clock = FakeClock()
        rule = {"template": "timeline", "events": [
            {"at_ms": index * 25, **action("light", enabled=index % 2 == 0)} for index in range(128)
        ]}
        runtime = self.rule_runtime(rule, clock=clock)
        runner = self.runner(runtime, clock=clock)
        result = runner.wait_task(runner.submit_task("闪光灯按音频节奏闪烁"), 3)
        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(len(runtime.agent.contexts), 2)
        self.assertEqual(len(runtime.commands), 128)
        for index, (_command, at) in enumerate(runtime.commands):
            self.assertAlmostEqual(at, index * 0.025, places=6)

    def test_late_events_are_skipped_without_a_burst(self):
        clock = FakeClock()
        rule = {"template": "timeline", "late_tolerance_ms": 20,
                "events": [{"at_ms": at, **action("light", pulse=at)} for at in (0, 10, 20, 200)]}
        runtime = self.rule_runtime(rule, clock=clock)
        runtime.after_step = lambda _command: clock.sleep(0.1)
        runner = self.runner(runtime, clock=clock)
        result = runner.wait_task(runner.submit_task("late beats"), 2)
        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual([command.parameters["pulse"] for command, _ in runtime.commands], [0, 200])
        self.assertEqual(sum(record.event_type == "timeline.event_skipped" for record in result.history), 2)

    def test_pause_seek_resume_skips_obsolete_and_never_replays(self):
        clock = FakeClock()
        rule = {"template": "timeline", "events": [
            {"at_ms": at, **action("light", pulse=at)} for at in (0, 100, 200, 300)
        ]}
        runtime = self.rule_runtime(rule, clock=clock)
        runner = self.runner(runtime, clock=clock)
        task_id = "timeline-controls"
        done = set()

        def control(fake):
            for threshold, operation in ((0.02, "pause"), (0.12, "seek"), (0.22, "resume")):
                if fake.value + 1e-9 >= threshold and operation not in done:
                    done.add(operation)
                    event = {"timeline_action": operation}
                    if operation == "seek":
                        event["position_ms"] = 200
                    runner.resume_task(task_id, event=event, conversation=runner.default_conversation(), event_id=operation)

        clock.on_sleep = control
        result = runner.wait_task(runner.submit_task("controls", task_id=task_id), 2)
        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual([command.parameters["pulse"] for command, _ in runtime.commands], [0, 200, 300])
        self.assertGreaterEqual(runtime.commands[1][1], 0.22 - 1e-9)

    def test_cancel_between_beats_does_not_dispatch_cleanup_or_remaining_events(self):
        clock = FakeClock()
        rule = {"template": "timeline", "events": [
            {"at_ms": at, **action("light", enabled=True)} for at in (0, 100, 200)
        ], "cleanup": [action("light", enabled=False)]}
        runtime = self.rule_runtime(rule, clock=clock)
        runner = self.runner(runtime, clock=clock)
        task_id = "cancel-beats"
        clock.on_sleep = lambda _clock: runner.cancel_task(task_id)
        result = runner.wait_task(runner.submit_task("cancel", task_id=task_id), 2)
        self.assertEqual(result.task.status, TaskStatus.CANCELLED)
        self.assertEqual(len(runtime.commands), 1)

    def test_timeline_event_limit_is_per_task_across_decisions(self):
        clock = FakeClock()
        first = {"template": "timeline", "events": [{"at_ms": 0, **action("light")} for _ in range(200)]}
        second = {"template": "timeline", "events": [{"at_ms": 0, **action("light")} for _ in range(100)]}
        runtime = CheckedRuntime(ScriptedAgent(AgentDecision("run_rule", rule=first), AgentDecision("run_rule", rule=second)), clock=clock)
        runner = self.runner(runtime, clock=clock)
        result = runner.wait_task(runner.submit_task("too many beats"), 3)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertIn("timeline budget", result.reply)
        self.assertEqual(len(runtime.commands), 200)

    def test_injected_clock_enforces_task_duration(self):
        clock = FakeClock()
        rule = {"template": "timeline", "events": [{"at_ms": 1000, **action("light")}]}
        runtime = self.rule_runtime(rule, clock=clock)
        runner = self.runner(runtime, clock=clock, limits=TaskLimits(duration_seconds=0.1))
        result = runner.wait_task(runner.submit_task("deadline"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertIn("deadline", result.reply)
        self.assertFalse(runtime.commands)


if __name__ == "__main__":
    unittest.main()
