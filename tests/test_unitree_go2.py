import unittest
from dataclasses import replace
from datetime import timedelta

from robotsoul import Command, Feedback, FeedbackStatus, RobotRuntime, load_profile
from robotsoul.profiles import load_selected_profile
from adapters.unitree_go2 import UnitreeGo2Adapter
from robotsoul.models import CapabilitySpec, ParameterSpec, utc_now


class FakeGo2Driver:
    def __init__(self, robot_id, capabilities=None, identity=("Unitree", "Go2")):
        self.robot_id = robot_id
        self.capabilities = capabilities or {}
        self.expected_identity = identity
        self.connected = False
        self.closed = False
        self.executed = []

    def connect(self):
        self.connected = True

    def health(self):
        return self.connected

    def identity(self):
        return self.expected_identity

    def discover_capabilities(self):
        return dict(self.capabilities)

    def drain_observations(self):
        return ()

    def execute(self, command):
        self.executed.append(command)
        now = utc_now()
        return (
            Feedback("accepted", command.command_id, command.task_id, command.trace_id,
                     FeedbackStatus.ACCEPTED, now),
            Feedback("running", command.command_id, command.task_id, command.trace_id,
                     FeedbackStatus.RUNNING, now),
            Feedback("succeeded", command.command_id, command.task_id, command.trace_id,
                     FeedbackStatus.SUCCEEDED, now, evidence={"condition_met": True}),
        )

    def cancel(self, command):
        return Feedback(
            "cancelled", command.command_id, command.task_id, command.trace_id,
            FeedbackStatus.CANCELLED, utc_now(),
        )

    def close(self):
        self.connected = False
        self.closed = True


class UnitreeGo2AdapterTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile("unitree-go2-development")

    def test_development_profile_loads_without_declaring_motion_skills(self):
        adapter = UnitreeGo2Adapter(self.profile, FakeGo2Driver(self.profile.robot_id))

        adapter.connect()

        self.assertTrue(adapter.health())
        self.assertEqual(adapter.discover_capabilities(), {})

    def test_deployment_config_selects_robot_module_default_profile(self):
        from pathlib import Path

        profile = load_selected_profile(config_path=Path("config/robot.json"))

        self.assertEqual(profile.robot_module, "unitree_go2")
        self.assertEqual(profile.profile_id, "unitree-go2-development")

    def test_deployment_config_selects_profile_within_registered_module(self):
        from pathlib import Path

        config_path = _write_robot_config("simulator", "simulator-compact")
        self.addCleanup(config_path.unlink, missing_ok=True)
        profile = load_selected_profile(config_path=config_path)

        self.assertEqual(profile.robot_module, "simulator")
        self.assertEqual(profile.profile_id, "simulator-compact")

    def test_unknown_configured_robot_module_is_rejected(self):
        config_path = _write_robot_config("../../not-a-module")
        self.addCleanup(config_path.unlink, missing_ok=True)

        with self.assertRaisesRegex(ValueError, "Unknown robot module"):
            load_selected_profile(config_path=config_path)

    def test_cli_style_robot_module_override_selects_registered_go2_module(self):
        profile = load_selected_profile(robot_module="unitree_go2")

        self.assertEqual(profile.robot_module, "unitree_go2")
        self.assertEqual(profile.profile_id, "unitree-go2-development")

    def test_config_switch_to_simulator_uses_simulator_default_profile(self):
        config_path = _write_robot_config("simulator")
        self.addCleanup(config_path.unlink, missing_ok=True)

        profile = load_selected_profile(config_path=config_path)

        self.assertEqual(profile.robot_module, "simulator")
        self.assertEqual(profile.profile_id, "simulator-basic")

    def test_registered_go2_module_fails_clearly_until_sdk_bridge_exists(self):
        with self.assertRaisesRegex(RuntimeError, "SDK bridge is not configured"):
            RobotRuntime.from_profile(self.profile, agent=_FixedAgent())

    def test_empty_profile_rejects_commands_without_calling_vendor_bridge(self):
        driver = FakeGo2Driver(self.profile.robot_id)
        adapter = UnitreeGo2Adapter(self.profile, driver)
        runtime = RobotRuntime.from_profile(self.profile, agent=_FixedAgent(), adapter=adapter)
        command = Command(
            "command-1", "task-1", "trace-1", self.profile.robot_id,
            "base.move_distance", {"distance_m": 0.1, "max_speed_mps": 0.1},
            utc_now() + timedelta(seconds=1),
        )

        feedback = adapter.execute(command)

        self.assertEqual(feedback[0].status, FeedbackStatus.FAILED)
        self.assertEqual(feedback[0].error_code, "command_rejected")
        self.assertEqual(driver.executed, [])
        self.assertEqual(runtime.available_tools(), {})

    def test_profile_bounds_intersect_driver_capability_bounds(self):
        move = CapabilitySpec(
            "base.move_distance",
            {
                "distance_m": ParameterSpec("number", -0.5, 0.5),
                "max_speed_mps": ParameterSpec("number", 0.01, 0.2),
            },
            timeout_ms=5000,
        )
        profile = replace(
            self.profile,
            capabilities=(move,),
            required_capabilities=("base.move_distance",),
        )
        wider_move = replace(
            move,
            parameters={
                "distance_m": ParameterSpec("number", -2.0, 2.0),
                "max_speed_mps": ParameterSpec("number", 0.01, 1.0),
            },
            timeout_ms=10000,
        )
        adapter = UnitreeGo2Adapter(
            profile,
            FakeGo2Driver(profile.robot_id, {"base.move_distance": wider_move}),
        )

        adapter.connect()
        effective = adapter.discover_capabilities()["base.move_distance"]

        self.assertEqual(effective.parameters["distance_m"].minimum, -0.5)
        self.assertEqual(effective.parameters["distance_m"].maximum, 0.5)
        self.assertEqual(effective.parameters["max_speed_mps"].maximum, 0.2)
        self.assertEqual(effective.timeout_ms, 5000)

    def test_identity_mismatch_closes_driver_and_fails_connection(self):
        driver = FakeGo2Driver(self.profile.robot_id, identity=("Unitree", "Go2-Pro"))
        adapter = UnitreeGo2Adapter(self.profile, driver)

        with self.assertRaisesRegex(RuntimeError, "identity"):
            adapter.connect()

        self.assertTrue(driver.closed)
        self.assertFalse(adapter.connected)


class _FixedAgent:
    def plan(self, *_args, **_kwargs):
        return None


def _write_robot_config(robot_module, profile_id=None):
    import json
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        config = {"robot_module": robot_module}
        if profile_id is not None:
            config["profile_id"] = profile_id
        json.dump(config, handle)
        return Path(handle.name)


if __name__ == "__main__":
    unittest.main()
