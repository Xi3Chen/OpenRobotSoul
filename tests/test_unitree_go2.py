import unittest
from dataclasses import replace
from datetime import timedelta

from robotsoul import Command, Feedback, FeedbackStatus, RobotRuntime, load_profile
from robotsoul.profiles import load_selected_profile
from adapters.unitree_go2 import UnitreeGo2Adapter
from adapters.unitree_go2.sdk_driver import (
    UnitreeGo2SdkDriver,
    _battery_from_state,
    _validate_state,
    create_sdk_driver,
)
from robotsoul.adapters.base import BatteryStatus
from adapters.unitree_go2.actions import SPORT_ACTIONS, sport_capabilities
from robotsoul.models import CapabilitySpec, ParameterSpec, RiskLevel, utc_now


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

    def test_development_profile_matches_the_sport_catalog(self):
        declared = {item.name: item for item in self.profile.capabilities}
        catalog = {item.name: item for item in sport_capabilities()}

        self.assertEqual(declared, catalog)
        self.assertEqual(self.profile.required_capabilities, ("sport.stop_move",))
        self.assertNotIn("sport.damp", declared)
        high = {item.name for item in self.profile.capabilities if item.risk_level == RiskLevel.HIGH}
        self.assertEqual(
            high,
            {
                "sport.recovery_stand",
                "sport.front_flip",
                "sport.front_jump",
                "sport.front_pounce",
                "sport.dance1",
                "sport.dance2",
                "sport.left_flip",
                "sport.back_flip",
                "sport.handstand",
                "sport.free_bound",
                "sport.free_jump",
                "sport.switch_joystick",
                "sport.auto_recover_set",
            },
        )
        self.assertEqual(declared["sport.stand_up"].description, catalog["sport.stand_up"].description)
        self.assertIn("站立锁定", declared["sport.stand_up"].description)
        self.assertIn("速度控制", declared["sport.move"].description)
        self.assertIn("Hello()", declared["sport.hello"].description)
        self.assertTrue(all(item.description for item in self.profile.capabilities))
        self.assertEqual(declared["sport.switch_avoid_mode"].risk_level, RiskLevel.CONFIRM)
        self.assertIn("agent", declared["sport.switch_avoid_mode"].allowed_callers)
        for name in ("sport.free_avoid", "sport.walk_upright", "sport.cross_step", "sport.stand_up", "sport.move"):
            self.assertEqual(declared[name].risk_level, RiskLevel.NORMAL)
            self.assertIn("agent", declared[name].allowed_callers)
        source = __import__("pathlib").Path("adapters/unitree_go2/bridge/bridge.cpp").read_text(encoding="utf-8")
        for action in SPORT_ACTIONS:
            self.assertIn(f'"{action.bridge_name}"', source)
        volume = declared["vui.set_volume"]
        self.assertEqual(volume.risk_level, RiskLevel.NORMAL)
        self.assertIn("agent", volume.allowed_callers)
        self.assertEqual(volume.parameters["volume"].minimum, 0)
        self.assertEqual(volume.parameters["volume"].maximum, 10)
        self.assertIn("damp is not exposed", source)
        adapter = UnitreeGo2Adapter(self.profile, FakeGo2Driver(self.profile.robot_id))
        adapter.connect()

        self.assertTrue(adapter.health())
        self.assertIsNone(adapter.battery())
        self.assertEqual(adapter.discover_capabilities(), {})

    def test_speaker_failure_does_not_drop_the_motion_connection(self):
        from unittest.mock import patch

        adapter = UnitreeGo2Adapter(self.profile, FakeGo2Driver(self.profile.robot_id))
        with patch.dict("os.environ", {"OPENROBOTSOUL_GO2_WEBRTC_IP": "192.0.2.10"}):
            with patch(
                "adapters.unitree_go2.adapter.start_go2_speaker",
                side_effect=RuntimeError("robot busy"),
            ):
                adapter.connect()
        self.assertTrue(adapter.health())
        self.assertIsNone(adapter._speaker)
        adapter.close()
        self.assertTrue(adapter.driver.closed)
        self.assertFalse(adapter.health())

    def test_connected_speaker_closes_with_the_adapter(self):
        from unittest.mock import patch

        class Speaker:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        speaker = Speaker()
        adapter = UnitreeGo2Adapter(self.profile, FakeGo2Driver(self.profile.robot_id))
        with patch("adapters.unitree_go2.adapter.start_go2_speaker", return_value=speaker):
            adapter.connect()
        self.assertIs(adapter._speaker, speaker)
        adapter.close()
        self.assertTrue(speaker.closed)
        self.assertTrue(adapter.driver.closed)

    def test_deployment_config_selects_robot_module_default_profile(self):
        from pathlib import Path

        profile = load_selected_profile(config_path=Path("config/robot.json"))

        self.assertEqual(profile.robot_module, "unitree_go2")
        self.assertEqual(profile.profile_id, "unitree-go2-development")

    def test_default_robot_selection_falls_back_to_packaged_config(self):
        from unittest.mock import patch

        with patch("robotsoul.profiles.PROJECT_ROOT", __import__("pathlib").Path("missing-root")):
            profile = load_selected_profile()

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

    def test_registered_go2_module_requires_explicit_network_interface(self):
        from unittest.mock import patch

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GO2_NETWORK_INTERFACE"):
                RobotRuntime.from_profile(self.profile, agent=_FixedAgent())

    def test_sdk_factory_does_not_guess_robot_network_interface(self):
        from unittest.mock import patch

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GO2_NETWORK_INTERFACE"):
                create_sdk_driver(self.profile)

    def test_disconnected_driver_advertises_nothing_and_rejects_motion(self):
        driver = UnitreeGo2SdkDriver(
            self.profile, "eth0", bridge_library="bridge.so", library_loader=lambda _path: None
        )
        self.assertEqual(driver.discover_capabilities(), {})
        command = Command(
            "command-1", "task-1", "trace-1", self.profile.robot_id,
            "sport.stand_up", {},
            utc_now() + timedelta(seconds=2),
            risk_level=RiskLevel.NORMAL,
        )
        self.assertEqual(driver.execute(command)[0].error_code, "bridge_unhealthy")
        self.assertEqual(driver.cancel(command).error_code, "no_active_command")

    def test_sdk_driver_requires_bridge_library_and_linux(self):
        from unittest.mock import patch

        driver = UnitreeGo2SdkDriver(self.profile, "eth0", bridge_library="missing-bridge.so")
        with patch("adapters.unitree_go2.sdk_driver.platform.system", return_value="Linux"):
            with self.assertRaisesRegex(RuntimeError, "bridge library was not found"):
                driver.connect()

        with patch("adapters.unitree_go2.sdk_driver.platform.system", return_value="Windows"):
            with self.assertRaisesRegex(RuntimeError, "requires Linux"):
                driver.connect()

    def test_sdk_driver_releases_native_handle_when_initial_health_check_fails(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock, patch

        library = Mock()
        library.go2_bridge_create.return_value = 123
        library.go2_bridge_health.return_value = 0
        with tempfile.TemporaryDirectory() as temporary:
            bridge_path = Path(temporary) / "bridge.so"
            bridge_path.touch()
            driver = UnitreeGo2SdkDriver(
                self.profile,
                "enp2s0",
                bridge_library=bridge_path,
                library_loader=lambda _path: library,
            )
            with patch("adapters.unitree_go2.sdk_driver.platform.system", return_value="Linux"):
                with self.assertRaisesRegex(RuntimeError, "state is unavailable"):
                    driver.connect()

        library.go2_bridge_close.assert_called_once()
        library.go2_bridge_destroy.assert_called_once_with(123)
        self.assertFalse(driver.health())

    def test_sdk_driver_closes_native_handle_once(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock

        library = Mock()
        library.go2_bridge_create.return_value = 123
        library.go2_bridge_health.return_value = 1
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            bridge_path = Path(temporary) / "bridge.so"
            bridge_path.touch()
            driver = UnitreeGo2SdkDriver(
                self.profile,
                "enp2s0",
                bridge_library=bridge_path,
                library_loader=lambda _path: library,
            )
            with patch("adapters.unitree_go2.sdk_driver.platform.system", return_value="Linux"):
                driver.connect()
            driver.close()
            driver.close()

        library.go2_bridge_close.assert_called_once()
        library.go2_bridge_destroy.assert_called_once_with(123)

    def test_sdk_state_schema_requires_matching_api_versions_and_finite_data(self):
        state = {
            "api_version_client": "1.0.0.1",
            "api_version_server": "1.0.0.1",
            "observed_at": utc_now().isoformat(),
            "state_stamp_sec": 1,
            "state_stamp_nanosec": 2,
            "mode": 1,
            "velocity": [0.0, 0.0, 0.0],
            "yaw_speed": 0.0,
            "position": [1.0, 2.0, 0.0],
            "error_code": 0,
        }
        _validate_state(state)
        with self.assertRaisesRegex(RuntimeError, "API versions do not match"):
            _validate_state({**state, "api_version_server": "9.9"})
        with self.assertRaisesRegex(RuntimeError, "vector is invalid"):
            _validate_state({**state, "velocity": [float("nan"), 0.0, 0.0]})

    def test_sdk_driver_emits_only_fresh_read_only_state_observation(self):
        import json
        from unittest.mock import Mock

        state = {
            "api_version_client": "1.0.0.1",
            "api_version_server": "1.0.0.1",
            "observed_at": utc_now().isoformat(),
            "state_stamp_sec": 1,
            "state_stamp_nanosec": 2,
            "mode": 2,
            "velocity": [0.1, 0.0, 0.0],
            "yaw_speed": 0.0,
            "position": [1.0, 2.0, 0.0],
            "error_code": 0,
        }
        driver = UnitreeGo2SdkDriver(self.profile, "eth0", bridge_library="bridge.so")
        driver._handle = 1
        driver._library = Mock()
        driver._library.go2_bridge_health.return_value = 1

        def read_state(_handle, output, _capacity, _error, _error_capacity):
            ctypes.memmove(output, json.dumps(state).encode("utf-8") + b"\0", len(json.dumps(state)) + 1)
            return 1

        import ctypes
        driver._library.go2_bridge_read_state.side_effect = read_state

        observations = driver.drain_observations()

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].robot_id, self.profile.robot_id)
        self.assertEqual(observations[0].event_type, "robot.sport_state")
        self.assertEqual(observations[0].payload["velocity"], [0.1, 0.0, 0.0])
        self.assertIn("sport.stop_move", driver.discover_capabilities())
        self.assertNotIn("sport.damp", driver.discover_capabilities())

        state["observed_at"] = (utc_now() - timedelta(seconds=3)).isoformat()
        self.assertEqual(driver.drain_observations(), ())

    def test_sdk_driver_publishes_sensor_readings_and_keeps_the_full_snapshot(self):
        import ctypes
        import json
        from unittest.mock import Mock

        observed = utc_now().isoformat()
        snapshot = {
            "streams": [
                {
                    "id": "sport",
                    "topic": "rt/sportmodestate",
                    "event_type": "robot.sport_state",
                    "language_summary": True,
                    "available": True,
                    "observed_at": observed,
                    "payload": {
                        "mode": 2,
                        "progress": 0.4,
                        "velocity": [0.1, 0.0, 0.0],
                        "range_obstacle": [1.0, 1.0, 1.0, 1.0],
                        "imu": {"rpy": [0.0, 0.2, 0.0]},
                        "path_point": [{"x": 1.0}] * 10,
                    },
                },
                {
                    "id": "low",
                    "topic": "rt/lowstate",
                    "event_type": "robot.low_state",
                    "language_summary": True,
                    "available": True,
                    "observed_at": observed,
                    "payload": {
                        "bms": {"soc": 77, "status": 0, "current": -4},
                        "imu": {"rpy": [0.0, 0.0, 0.1], "temperature": 36},
                        "motors": [{"temperature": 40}, {"temperature": 55}],
                        "power_v": 28.0,
                    },
                },
                {
                    "id": "wireless_controller",
                    "topic": "rt/wirelesscontroller",
                    "event_type": "robot.wireless_controller",
                    "language_summary": False,
                    "available": True,
                    "observed_at": observed,
                    "payload": {"lx": 0.2, "keys": 3},
                },
                {
                    "id": "cloud",
                    "topic": "rt/utlidar/cloud",
                    "event_type": "robot.lidar_cloud",
                    "language_summary": True,
                    "available": False,
                    "observed_at": None,
                    "payload": None,
                },
            ]
        }
        state = {
            "api_version_client": "1.0.0.1",
            "api_version_server": "1.0.0.1",
            "observed_at": observed,
            "state_stamp_sec": 1,
            "state_stamp_nanosec": 2,
            "mode": 2,
            "velocity": [0.1, 0.0, 0.0],
            "yaw_speed": 0.0,
            "position": [1.0, 2.0, 0.0],
            "error_code": 0,
        }
        driver = UnitreeGo2SdkDriver(self.profile, "eth0", bridge_library="bridge.so")
        driver._handle = 1
        driver._library = Mock()
        driver._library.go2_bridge_health.return_value = 1
        driver._library.go2_bridge_read_sensors.argtypes = ()

        def write_json(payload):
            def _write(_handle, output, _capacity, _error, _error_capacity):
                encoded = json.dumps(payload).encode("utf-8") + b"\0"
                ctypes.memmove(output, encoded, len(encoded))
                return 1
            return _write

        driver._library.go2_bridge_read_state.side_effect = write_json(state)
        driver._library.go2_bridge_read_sensors.side_effect = write_json(snapshot)

        observations = driver.drain_observations()
        by_type = {item.event_type: item for item in observations}

        self.assertEqual(set(by_type), {"robot.sport_state", "robot.low_state"})
        self.assertEqual(by_type["robot.sport_state"].payload["range_obstacle"], [1.0, 1.0, 1.0, 1.0])
        self.assertEqual(by_type["robot.sport_state"].payload["imu_rpy"], [0.0, 0.2, 0.0])
        self.assertNotIn("path_point", by_type["robot.sport_state"].payload)
        self.assertEqual(by_type["robot.low_state"].payload["bms_soc"], 77)
        self.assertEqual(by_type["robot.low_state"].payload["motor_temperature_max"], 55)
        self.assertNotIn("motors", by_type["robot.low_state"].payload)

        published = driver.sensor_snapshot()
        wireless = next(item for item in published["streams"] if item["id"] == "wireless_controller")
        low = next(item for item in published["streams"] if item["id"] == "low")
        self.assertEqual(wireless["payload"]["keys"], 3)
        self.assertEqual(low["payload"]["motors"][1]["temperature"], 55)

    def test_unadvertised_action_is_rejected_without_calling_the_driver(self):
        driver = FakeGo2Driver(self.profile.robot_id)
        adapter = UnitreeGo2Adapter(self.profile, driver)
        adapter.connect()
        adapter.discover_capabilities()
        command = Command(
            "command-1", "task-1", "trace-1", self.profile.robot_id,
            "sport.stand_up", {},
            utc_now() + timedelta(seconds=2),
            risk_level=RiskLevel.NORMAL,
        )

        feedback = adapter.execute(command)

        self.assertEqual(feedback[0].status, FeedbackStatus.FAILED)
        self.assertEqual(feedback[0].error_code, "command_rejected")
        self.assertEqual(driver.executed, [])

    def test_runtime_requires_the_stop_action(self):
        driver = FakeGo2Driver(self.profile.robot_id)
        adapter = UnitreeGo2Adapter(self.profile, driver)

        with self.assertRaisesRegex(RuntimeError, "sport.stop_move"):
            RobotRuntime.from_profile(self.profile, agent=_FixedAgent(), adapter=adapter)

        self.assertTrue(driver.closed)

    def test_runtime_exposes_advertised_sport_actions_by_risk(self):
        capabilities = {item.name: item for item in sport_capabilities()}
        driver = FakeGo2Driver(self.profile.robot_id, capabilities)
        adapter = UnitreeGo2Adapter(self.profile, driver)
        runtime = RobotRuntime.from_profile(self.profile, agent=_FixedAgent(), adapter=adapter)
        self.addCleanup(runtime.close)

        self.assertIn("sport.stand_up", runtime.available_tools())
        self.assertIn("sport.move", runtime.available_tools())
        self.assertIn("vui.set_volume", runtime.available_tools())
        self.assertNotIn("sport.front_flip", runtime.available_tools())
        self.assertIn("sport.front_flip", runtime.dashboard_tools())

    def test_sdk_driver_dispatches_stand_up_and_rejects_ranges_locally(self):
        calls = []
        driver = _connected_driver(self.profile, calls)
        accepted = driver.execute(_sport_command(
            self.profile, "sport.stand_up", {}, RiskLevel.NORMAL,
        ))
        rejected = driver.execute(_sport_command(
            self.profile, "sport.move",
            {"vx": 9, "vy": 0, "vyaw": 0, "duration_ms": 200},
            RiskLevel.NORMAL,
        ))

        self.assertEqual([item.status for item in accepted], [
            FeedbackStatus.ACCEPTED, FeedbackStatus.RUNNING, FeedbackStatus.SUCCEEDED,
        ])
        self.assertTrue(accepted[-1].evidence["condition_met"])
        self.assertEqual(accepted[-1].evidence["completion"], "nominal")
        self.assertFalse(accepted[-1].evidence["completion_verified"])
        self.assertIn("estimated", accepted[-1].message)
        self.assertEqual(calls, ["stand_up"])
        self.assertEqual(rejected[0].error_code, "invalid_parameters")

    def test_sdk_driver_sets_volume_inside_the_documented_range(self):
        calls = []
        driver = _connected_driver(self.profile, calls)
        accepted = driver.execute(_sport_command(
            self.profile, "vui.set_volume", {"volume": 6}, RiskLevel.NORMAL,
        ))
        rejected = driver.execute(_sport_command(
            self.profile, "vui.set_volume", {"volume": 11}, RiskLevel.NORMAL,
        ))
        fractional = driver.execute(_sport_command(
            self.profile, "vui.set_volume", {"volume": 1.5}, RiskLevel.NORMAL,
        ))

        self.assertEqual(accepted[-1].status, FeedbackStatus.SUCCEEDED)
        self.assertIn("volume", accepted[-1].message)
        self.assertEqual(accepted[-1].evidence["bridge_action"], "set_volume")
        self.assertEqual(calls, ["set_volume"])
        self.assertEqual(rejected[0].error_code, "invalid_parameters")
        self.assertEqual(fractional[0].error_code, "invalid_parameters")

    def test_sdk_driver_bounds_move_with_stop_and_cancel(self):
        import threading

        calls = []
        entered = threading.Event()
        driver = _connected_driver(self.profile, calls, on_move=entered.set)
        command = _sport_command(
            self.profile, "sport.move",
            {"vx": 0.2, "vy": 0.0, "vyaw": 0.0, "duration_ms": 1500},
            RiskLevel.NORMAL,
        )
        box = {}

        def run():
            box["feedback"] = driver.execute(command)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(1))
        cancellation = driver.cancel(command)
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(cancellation.status, FeedbackStatus.CANCELLED)
        self.assertEqual(box["feedback"][0].status, FeedbackStatus.CANCELLED)
        self.assertEqual(calls[0], "move")
        self.assertIn("stop_move", calls)

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

    def test_battery_is_shown_only_when_the_connected_driver_reports_it(self):
        driver = FakeGo2Driver(self.profile.robot_id)
        adapter = UnitreeGo2Adapter(self.profile, driver)
        self.assertIsNone(adapter.battery())
        adapter.connect()

        driver.battery = lambda: BatteryStatus(64, False)
        self.assertEqual(adapter.battery(), BatteryStatus(64, False))

        driver.battery = lambda: {"level_percent": 64, "charging": False}
        self.assertIsNone(adapter.battery())

        def unavailable():
            raise RuntimeError("bms unavailable")

        driver.battery = unavailable
        self.assertIsNone(adapter.battery())
        adapter.close()
        driver.battery = lambda: BatteryStatus(64, True)
        self.assertIsNone(adapter.battery())

    def test_battery_state_uses_documented_charge_status_and_rejects_stale_samples(self):
        now = utc_now()
        sample = {
            "soc": 81,
            "status": 7,
            "current_ma": 1400,
            "observed_at": now.isoformat(),
        }

        self.assertEqual(_battery_from_state(sample, now=now), BatteryStatus(81, True))
        self.assertEqual(
            _battery_from_state({**sample, "status": 6, "current_ma": 80}, now=now),
            BatteryStatus(81, True),
        )
        self.assertEqual(
            _battery_from_state({**sample, "status": 8, "current_ma": -900}, now=now),
            BatteryStatus(81, False),
        )
        self.assertIsNone(_battery_from_state({**sample, "status": 4}, now=now).charging)
        with self.assertRaisesRegex(RuntimeError, "level is invalid"):
            _battery_from_state({**sample, "soc": 101}, now=now)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            _battery_from_state(sample, now=now + timedelta(seconds=3))

    def test_sdk_driver_reads_battery_without_requiring_sport_state(self):
        import ctypes
        import json
        from unittest.mock import Mock

        now = utc_now()
        sample = {
            "soc": 42,
            "status": 8,
            "current_ma": -300,
            "observed_at": now.isoformat(),
        }
        driver = UnitreeGo2SdkDriver(self.profile, "eth0", bridge_library="bridge.so")
        driver._handle = 1
        driver._library = Mock()

        def read_battery(_handle, output, _capacity, _error, _error_capacity):
            payload = json.dumps(sample).encode("utf-8") + b"\0"
            ctypes.memmove(output, payload, len(payload))
            return 1

        driver._library.go2_bridge_read_battery.side_effect = read_battery
        self.assertEqual(driver.battery(), BatteryStatus(42, False))

        driver._library.go2_bridge_read_battery.side_effect = lambda *_args: 0
        self.assertIsNone(driver.battery())
        driver._library = object()
        self.assertIsNone(driver.battery())


    def test_timed_move_repeats_native_calls_across_two_second_cycles(self):
        from unittest.mock import patch

        from adapters.unitree_go2 import motion
        from adapters.unitree_go2 import sdk_driver as driver_module

        calls = []
        driver = _connected_driver(self.profile, calls)
        with patch.object(driver_module, "NATIVE_MOVE_MS", 80), patch.object(motion, "MOVE_RESEND_MS", 20):
            feedback = driver.execute(_sport_command(
                self.profile, "sport.move",
                {"vx": 0.3, "vy": 0, "vyaw": 0, "duration_ms": 200},
                RiskLevel.NORMAL,
            ))

        self.assertEqual(feedback[-1].status, FeedbackStatus.SUCCEEDED)
        self.assertEqual(feedback[-1].evidence["cycles"], 3)
        self.assertGreaterEqual(feedback[-1].evidence["move_sends"], 3)
        self.assertGreaterEqual(calls.count("move"), 3)
        self.assertEqual(calls[-1], "stop_move")

    def test_distance_move_stops_on_fused_position_and_fails_when_sensors_disagree(self):
        calls = []
        driver = _connected_driver(self.profile, calls)
        seen = {"n": 0}

        def pose(distance):
            return {
                "position": [distance, 0.0, 0.0],
                "mode": 1,
                "error_code": 0,
                "velocity": [0.3, 0.0, 0.0],
                "yaw_speed": 0.0,
            }

        def read_state():
            seen["n"] += 1
            return pose(0.0 if seen["n"] < 3 else 4.0)

        driver._read_state = read_state
        driver._try_read_sensors = lambda: {
            "streams": [{
                "id": "odometry",
                "available": True,
                "payload": {"position": [0.0 if seen["n"] < 3 else 4.0, 0.0, 0.0]},
            }]
        }
        reached = driver.execute(_sport_command(
            self.profile, "sport.move_distance",
            {"vx": 0.4, "vy": 0, "vyaw": 0, "distance_m": 4},
            RiskLevel.NORMAL,
            seconds=5,
        ))
        self.assertEqual(reached[-1].status, FeedbackStatus.SUCCEEDED)
        self.assertTrue(reached[-1].evidence["condition_met"])
        self.assertGreaterEqual(reached[-1].evidence["traveled_m"], 4)
        self.assertIn("stop_move", calls)

        calls.clear()
        seen["n"] = 0

        def disagree():
            seen["n"] += 1
            return pose(0.0 if seen["n"] < 2 else 1.0)

        driver._read_state = disagree
        driver._try_read_sensors = lambda: {
            "streams": [{
                "id": "odometry",
                "available": True,
                "payload": {"position": [0.0 if seen["n"] < 2 else 4.0, 0.0, 0.0]},
            }]
        }
        missed = driver.execute(_sport_command(
            self.profile, "sport.move_distance",
            {"vx": 0.4, "vy": 0, "vyaw": 0, "distance_m": 4},
            RiskLevel.NORMAL,
            seconds=2.5,
        ))
        self.assertEqual(missed[-1].status, FeedbackStatus.FAILED)
        self.assertEqual(missed[-1].error_code, "distance_timeout")
        self.assertIn("stop_move", calls)

    def test_distance_move_does_not_start_without_a_position(self):
        calls = []
        driver = _connected_driver(self.profile, calls)
        driver._read_state = lambda: (_ for _ in ()).throw(RuntimeError("missing"))
        driver._try_read_sensors = lambda: {"streams": []}
        feedback = driver.execute(_sport_command(
            self.profile, "sport.move_distance",
            {"vx": 0.4, "vy": 0, "vyaw": 0, "distance_m": 1},
            RiskLevel.NORMAL,
        ))
        self.assertEqual(feedback[-1].error_code, "distance_unavailable")
        self.assertEqual(calls, [])


class _FixedAgent:
    def plan(self, *_args, **_kwargs):
        return None


def _sport_command(profile, skill, parameters, risk, seconds=8):
    return Command(
        "command-1", "task-1", "trace-1", profile.robot_id,
        skill, parameters,
        utc_now() + timedelta(seconds=seconds),
        risk_level=risk,
    )


def _connected_driver(profile, calls, on_move=None):
    import ctypes
    from unittest.mock import Mock

    def call(_handle, action, _numbers, _count, _has_flag, _flag, output, _output_capacity, _error, _error_capacity):
        name = action.decode("utf-8") if isinstance(action, (bytes, bytearray)) else str(action)
        calls.append(name)
        if name == "move" and on_move is not None:
            on_move()
        payload = b'{"api_code":0}\0'
        ctypes.memmove(output, payload, len(payload))
        return 1

    driver = UnitreeGo2SdkDriver(profile, "eth0", bridge_library="bridge.so")
    driver._handle = 1
    driver._library = Mock()
    driver._library.go2_bridge_health.return_value = 1
    driver._library.go2_bridge_call.side_effect = call
    driver._library.go2_bridge_read_state.return_value = 0
    return driver


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
