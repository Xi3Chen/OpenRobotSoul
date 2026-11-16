import unittest

from adapters.simulator.adapter import FakeRobotAdapter
from robotsoul import InMemoryToolCallAuditSink, RobotRuntime, load_profile
from robotsoul.models import AgentDecision, AgentIntent, FeedbackStatus, Observation, TaskStatus, utc_now
from robotsoul.task_runner import TaskLimits

from tests.test_task_runner import ScriptedAgent, completion


class TaskRuntimeIntegrationTests(unittest.TestCase):
    def runtime(self, agent, *, delay=0):
        profile = load_profile("simulator-basic")
        adapter = FakeRobotAdapter(profile, execution_delay_seconds=delay)
        runtime = RobotRuntime.from_profile(profile, agent=agent, adapter=adapter,
                                            audit_sink=InMemoryToolCallAuditSink())
        self.addCleanup(runtime.close)
        return runtime

    def test_real_runtime_step_feedback_drives_second_decision(self):
        agent = ScriptedAgent(AgentDecision("act", (AgentIntent("speech.say", {"text": "hello"}),)), completion)
        runtime = self.runtime(agent)
        now = utc_now()
        observation = Observation("persistent-state", runtime.profile.robot_id, "sensor", "window", now, now,
                                  {"state": "open"}, "simulator", 30000)
        runtime.adapter.publish_observation(observation)
        result = runtime.wait_task(runtime.submit_task("say hello"), 2)
        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(len(runtime.adapter.executed_commands), 1)
        self.assertEqual(len(agent.contexts), 2)
        self.assertEqual(agent.contexts[0].observations, (observation,))
        self.assertEqual(agent.contexts[1].observations, (observation,))
        self.assertEqual(result.reply, "Simulated skill completed")

    def test_real_runtime_cancel_during_adapter_prevents_following_skill(self):
        agent = ScriptedAgent(AgentDecision("act", (
            AgentIntent("speech.say", {"text": "first"}), AgentIntent("speech.say", {"text": "second"}),
        )))
        runtime = self.runtime(agent, delay=1)
        task_id = runtime.submit_task("two messages")
        self.assertTrue(runtime.adapter.execution_started.wait(2))
        runtime.cancel_task(task_id)
        result = runtime.wait_task(task_id, 2)
        self.assertEqual(result.task.status, TaskStatus.CANCELLED)
        self.assertEqual(len(runtime.adapter.executed_commands), 1)
        self.assertEqual(result.feedback[-1].status, FeedbackStatus.CANCELLED)

    def test_real_runtime_prevalidates_confirm_action_before_plain_action(self):
        agent = ScriptedAgent(AgentDecision("act", (
            AgentIntent("speech.say", {"text": "first"}),
            AgentIntent("base.move_distance", {"distance_m": 0.1, "max_speed_mps": 0.1}),
        )))
        runtime = self.runtime(agent)
        result = runtime.wait_task(runtime.submit_task("plan lacks review"), 2)
        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertFalse(runtime.adapter.executed_commands)
        self.assertIn("reviewer unavailable", result.reply)


    def test_real_deadline_during_native_adapter_cancels_and_never_runs_next_step(self):
        agent = ScriptedAgent(AgentDecision("act", (
            AgentIntent("speech.say", {"text": "first"}), AgentIntent("speech.say", {"text": "second"}),
        )))
        runtime = self.runtime(agent, delay=0.8)
        runner = runtime._runner()
        runner.limits = TaskLimits(duration_seconds=0.15)
        task_id = runtime.submit_task("long adapter action")
        self.assertTrue(runtime.adapter.execution_started.wait(1))
        result = runtime.wait_task(task_id, 0.6)
        self.assertEqual(result.task.status, TaskStatus.INTERRUPTED)
        self.assertIn("deadline", result.reply.lower())
        runner._queue.join()
        final = runtime.get_task(task_id)
        self.assertEqual(len(runtime.adapter.executed_commands), 1)
        self.assertEqual(final.feedback[-1].status, FeedbackStatus.CANCELLED)
        self.assertEqual(final.task.status, TaskStatus.INTERRUPTED)


if __name__ == "__main__":
    unittest.main()
