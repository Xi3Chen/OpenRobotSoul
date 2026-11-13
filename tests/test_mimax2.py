from __future__ import annotations

from datetime import timedelta
import unittest
from unittest.mock import patch

from adapters.mimax2.adapter import MiMax2Adapter
from adapters.mimax2.driver import MiMax2FlashDriver
from robotsoul.agent import DemoAgent
from robotsoul.models import Command, InvocationSource, RiskLevel, utc_now
from robotsoul.profiles import load_profile


class FakeFlashDriver:
    robot_id = "phone-mimax2"

    def __init__(self):
        self.connected = False
        self.enabled = False

    def connect(self):
        self.connected = True

    def health(self):
        return self.connected

    def identity(self):
        return "Xiaomi", "MiMax2"

    def set_flash(self, enabled):
        self.enabled = enabled
        return 4 if enabled else 0

    def close(self):
        self.enabled = False
        self.connected = False


class MiMax2Tests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile("mimax2-basic")
        self.driver = FakeFlashDriver()
        self.adapter = MiMax2Adapter(self.profile, self.driver)
        self.adapter.connect()
        self.adapter.discover_capabilities()

    def tearDown(self):
        self.adapter.close()

    def test_profile_and_capability_are_registered(self):
        self.assertEqual(self.profile.model, "MiMax2")
        self.assertIsNone(self.adapter.battery())
        self.assertEqual(tuple(self.adapter.discover_capabilities()), ("flash.toggle",))

    def test_flash_command_changes_device_and_returns_success(self):
        command = Command(
            "cmd-1", "task-1", "trace-1", self.profile.robot_id, "flash.toggle", {"enabled": True},
            utc_now() + timedelta(seconds=2), risk_level=RiskLevel.NORMAL,
            invocation_source=InvocationSource.AGENT,
        )
        feedback = self.adapter.execute(command)
        self.assertTrue(self.driver.enabled)
        self.assertEqual(feedback[-1].status.value, "succeeded")
        self.assertEqual(
            feedback[-1].evidence,
            {"enabled": True, "condition_met": True, "brightness": 4},
        )

    def test_succeeded_feedback_carries_completion_evidence(self):
        command = Command(
            "cmd-2", "task-2", "trace-2", self.profile.robot_id, "flash.toggle", {"enabled": False},
            utc_now() + timedelta(seconds=2), risk_level=RiskLevel.NORMAL,
            invocation_source=InvocationSource.AGENT,
        )
        feedback = self.adapter.execute(command)
        self.assertIs(feedback[-1].evidence.get("condition_met"), True)

    def test_driver_reports_unverified_when_readback_mismatches(self):
        driver = MiMax2FlashDriver("phone-mimax2")
        path_type = type(driver.flash_path)
        with patch.object(path_type, "is_file", return_value=True), \
             patch.object(path_type, "read_text", side_effect=["4\n", "0\n"]), \
             patch.object(path_type, "write_bytes", return_value=None):
            driver.connect()
            with self.assertRaises(RuntimeError):
                driver.set_flash(True)

    def test_slash_command_toggles_flash_without_matching_plain_text(self):
        from robotsoul.agent import resolve_slash_command

        capabilities = self.adapter.discover_capabilities()
        on = resolve_slash_command("/flash.toggle enabled=true", capabilities)
        off = resolve_slash_command("/flash.toggle enabled=false", capabilities)
        self.assertEqual(on.parameters, {"enabled": True})
        self.assertEqual(off.parameters, {"enabled": False})
        self.assertIsNone(DemoAgent().plan("打开闪光灯", capabilities))

    def test_driver_uses_fixed_path_and_sudo_fallback(self):
        driver = MiMax2FlashDriver("phone-mimax2")
        path_type = type(driver.flash_path)
        with patch.object(path_type, "is_file", return_value=True), \
             patch.object(path_type, "read_text", return_value="4\n"), \
             patch.object(path_type, "write_bytes", side_effect=PermissionError), \
             patch("adapters.mimax2.driver.shutil.which", side_effect=lambda name: "/usr/bin/" + name), \
             patch("adapters.mimax2.driver.subprocess.run", return_value=type("Result", (), {"returncode": 0})()) as run:
            driver.connect()
            driver.set_flash(True)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["-n", "\\usr\\local\\sbin\\voice-assistant-flashlight"])
        self.assertEqual(command[3], "on")


if __name__ == "__main__":
    unittest.main()
