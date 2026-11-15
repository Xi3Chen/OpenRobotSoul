import ctypes
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

from robotsoul import Command, FeedbackStatus, load_profile
from robotsoul.models import RiskLevel, utc_now
from adapters.unitree_go2.sdk_driver import UnitreeGo2SdkDriver


class Go2PrecancelTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile("unitree-go2-development")
        self.calls = []
        self.driver = UnitreeGo2SdkDriver(self.profile, "eth0", bridge_library="bridge.so")
        self.driver._handle = 1
        self.driver._library = Mock()
        self.driver._library.go2_bridge_health.return_value = 1
        self.driver._library.go2_bridge_read_state.return_value = 0
        self.driver._library.go2_bridge_call.side_effect = self._native_call

    def _native_call(self, _handle, action, _numbers, _count, _has_flag, _flag,
                     output, _capacity, _error, _error_capacity):
        self.calls.append(action.decode("utf-8"))
        result = b'{"api_code":0}\0'
        ctypes.memmove(output, result, len(result))
        return 1

    def command(self, **changes):
        command = Command("command-1", "task-1", "trace-1", self.profile.robot_id,
                          "sport.stop_move", {}, utc_now() + timedelta(seconds=8),
                          risk_level=RiskLevel.NORMAL)
        return replace(command, **changes)

    def assert_prevented(self, feedback, command):
        self.assertEqual(feedback.status, FeedbackStatus.CANCELLED)
        self.assertEqual((feedback.task_id, feedback.trace_id, feedback.command_id),
                         (command.task_id, command.trace_id, command.command_id))
        self.assertEqual(feedback.error_code, "not_dispatched")
        self.assertEqual(feedback.evidence["robot_id"], command.robot_id)
        self.assertIs(feedback.evidence["outcome_known"], True)
        self.assertIs(feedback.evidence["cancellation_confirmed"], True)
        self.assertIs(feedback.evidence["native_dispatch"], False)

    def test_cancel_in_registration_to_begin_gap_prevents_native_action(self):
        command = self.command()
        entered, released = threading.Event(), threading.Event()
        original = self.driver._begin
        result = {}

        def before_begin(item):
            entered.set()
            self.assertTrue(released.wait(2))
            return original(item)

        with patch.object(self.driver, "_begin", side_effect=before_begin):
            worker = threading.Thread(target=lambda: result.update(feedback=self.driver.execute(command)))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assert_prevented(self.driver.cancel(command), command)
            finally:
                released.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assert_prevented(result["feedback"][0], command)
        self.assertEqual(self.calls, [])

    def test_precancel_is_idempotent_and_cannot_be_replayed(self):
        command = self.command()
        self.assert_prevented(self.driver.cancel(command), command)
        self.assert_prevented(self.driver.cancel(command), command)
        for _ in range(2):
            self.assert_prevented(self.driver.execute(command)[0], command)
        self.assertEqual(self.calls, [])

    def test_precancel_binds_task_trace_robot_and_full_command(self):
        command = self.command()
        self.driver.cancel(command)
        changes = ({"task_id": "other-task"}, {"trace_id": "other-trace"},
                   {"robot_id": "other-robot"}, {"skill": "sport.balance_stand"})
        for change in changes:
            with self.subTest(change=change):
                feedback = self.driver.execute(replace(command, **change))[0]
                self.assertEqual(feedback.status, FeedbackStatus.FAILED)
        self.assert_prevented(self.driver.execute(command)[0], command)
        self.assertEqual(self.calls, [])

    def test_precancel_of_next_command_does_not_stop_active_command(self):
        active = self.command()
        following = self.command(command_id="command-2", task_id="task-2", trace_id="trace-2")
        self.driver._begin(active)
        try:
            self.assert_prevented(self.driver.cancel(following), following)
            self.assertFalse(self.driver._cancel_event.is_set())
            self.assertEqual(self.driver._active_command_id, active.command_id)
            self.assertEqual(self.calls, [])
        finally:
            self.driver._end(active)
        self.assert_prevented(self.driver.execute(following)[0], following)

    def test_forged_active_identity_does_not_send_stop(self):
        active = self.command()
        self.driver._begin(active)
        try:
            for change in ({"task_id": "other-task"}, {"trace_id": "other-trace"},
                           {"robot_id": "other-robot"}, {"skill": "sport.balance_stand"}):
                with self.subTest(change=change):
                    result = self.driver.cancel(replace(active, **change))
                    self.assertEqual(result.status, FeedbackStatus.FAILED)
                    self.assertFalse(self.driver._cancel_event.is_set())
            self.assertEqual(self.calls, [])
        finally:
            self.driver._end(active)

    def test_completed_command_cannot_claim_prevented_dispatch(self):
        command = self.command()
        self.assertEqual(self.driver.execute(command)[-1].status, FeedbackStatus.SUCCEEDED)
        result = self.driver.cancel(command)
        self.assertEqual(result.status, FeedbackStatus.FAILED)
        self.assertEqual(result.error_code, "no_active_command")
        self.assertEqual(self.calls, ["stop_move"])

    def test_native_stop_finishes_before_next_command_can_begin(self):
        active = self.command()
        following = self.command(command_id="next", task_id="next-task")
        self.driver._begin(active)
        stopping, release_stop, next_started = threading.Event(), threading.Event(), threading.Event()
        original = self.driver._invoke
        outcomes = {}

        def slow_stop(*args):
            stopping.set()
            self.assertTrue(release_stop.wait(2))
            return original(*args)

        def next_command():
            self.driver._end(active)
            self.driver._begin(following)
            next_started.set()

        with patch.object(self.driver, "_invoke", side_effect=slow_stop):
            canceller = threading.Thread(target=lambda: outcomes.update(cancel=self.driver.cancel(active)))
            canceller.start()
            self.assertTrue(stopping.wait(2))
            successor = threading.Thread(target=next_command)
            successor.start()
            try:
                self.assertFalse(next_started.wait(0.05), "next command started before old native stop finished")
            finally:
                release_stop.set()
                canceller.join(2)
                successor.join(2)
        self.assertFalse(canceller.is_alive())
        self.assertFalse(successor.is_alive())
        self.assertEqual(outcomes["cancel"].status, FeedbackStatus.CANCELLED)
        self.assertEqual(self.driver._active_command_id, following.command_id)
        self.assertFalse(self.driver._cancel_event.is_set())
        self.driver._end(following)

    def test_cancel_cannot_acknowledge_stop_before_pending_native_request(self):
        command = self.command(skill="sport.balance_stand")
        entering_native, release_native, cancellation_done = threading.Event(), threading.Event(), threading.Event()
        original = self.driver._invoke
        outcomes = {}

        def before_native(action, *args):
            if action == "balance_stand":
                entering_native.set()
                self.assertTrue(release_native.wait(2))
            return original(action, *args)

        def cancel():
            outcomes["cancel"] = self.driver.cancel(command)
            cancellation_done.set()

        with patch.object(self.driver, "_invoke", side_effect=before_native):
            worker = threading.Thread(target=lambda: outcomes.update(feedback=self.driver.execute(command)))
            worker.start()
            self.assertTrue(entering_native.wait(2))
            canceller = threading.Thread(target=cancel)
            canceller.start()
            try:
                self.assertFalse(cancellation_done.wait(0.05), "stop was acknowledged before a queued physical request")
            finally:
                release_native.set()
                worker.join(2)
                canceller.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertEqual(self.calls[0], "balance_stand")
        if outcomes["cancel"].status == FeedbackStatus.CANCELLED:
            self.assertIn("stop_move", self.calls)


if __name__ == "__main__":
    unittest.main()
