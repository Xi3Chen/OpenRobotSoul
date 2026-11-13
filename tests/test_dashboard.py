from __future__ import annotations

import json
import threading
import unittest
from dataclasses import replace
from http.client import HTTPConnection

from robotsoul.adapters.base import BatteryStatus
from robotsoul.dashboard import DashboardService, _task_result_view, create_dashboard_server
from robotsoul.settings import LlmConfigStore, OperatorDirectory
from robotsoul.models import (
    AgentIntent,
    CapabilitySpec,
    ReviewVerdict,
    RiskLevel,
    SafetyReviewResult,
    ParameterSpec,
    utc_now,
)
from robotsoul.profiles import load_profile
from robotsoul.runtime import RobotRuntime
from robotsoul.voice.core import CaptureArbiter, CaptureState
from robotsoul.voice.webrtc import WebRtcUnavailable


class FakeLocalCapture:
    def __init__(self, *, available=True, fail_close=False):
        self.available = available
        self.fail_close = fail_close
        self.opened = False

    def probe(self):
        return self.available

    def open(self):
        self.opened = True

    def close(self):
        if self.fail_close:
            raise OSError("busy")
        self.opened = False


class AllowReviewer:
    def review(self, proposal):
        return SafetyReviewResult(ReviewVerdict.ALLOW, proposal.arguments_digest, "checked")


class FixedAgent:
    def __init__(self, intent):
        self.intent = intent

    def plan(self, *_args, **_kwargs):
        return self.intent


class FakeCallMedia:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.closed = []

    def answer(self, call_id, _session, offer):
        if self.fail:
            raise WebRtcUnavailable("aiortc and PyAV are required")
        return {"sdp": "answer-sdp", "type": "answer"}

    def close(self, call_id):
        self.closed.append(call_id)


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        profile = load_profile("simulator-basic")
        self.runtime = RobotRuntime.from_profile(profile, safety_reviewer=AllowReviewer())
        self.server = create_dashboard_server(self.runtime, operator_password="test-pin-4982")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers or {})
        if payload is not None:
            request_headers.setdefault("Content-Type", "application/json")
            request_headers["Content-Length"] = str(len(payload))
        self.connection.request(method, path, body=payload, headers=request_headers)
        response = self.connection.getresponse()
        raw = response.read()
        decoded = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        return response.status, response.getheaders(), decoded

    def login(self, username="operator", password="test-pin-4982"):
        status, headers, data = self.request("POST", "/api/login", {"username": username, "password": password})
        self.assertEqual(status, 200)
        cookie = next(value for name, value in headers if name.lower() == "set-cookie").split(";", 1)[0]
        return cookie, data["csrf_token"]

    def mutation_headers(self, cookie, csrf):
        return {"Cookie": cookie, "X-CSRF-Token": csrf}

    def test_call_control_requires_authentication_and_csrf(self):
        status, _headers, data = self.request("POST", "/api/voice/calls/start", {})
        self.assertEqual(status, 401)
        self.connection.close()
        self.connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        cookie, csrf = self.login()
        status, _headers, data = self.request("POST", "/api/voice/calls/start", {}, {"Cookie": cookie})
        self.assertEqual(status, 403)
        self.assertIn("CSRF", data["error"])

    def test_call_start_owns_mic_and_unavailable_media_answer_releases_it(self):
        local = FakeLocalCapture()
        arbiter = CaptureArbiter(local)
        self.assertEqual(arbiter.refresh_local(), CaptureState.PASSIVE)
        self.server.service.capture_arbiter = arbiter
        cookie, csrf = self.login()
        headers = self.mutation_headers(cookie, csrf)

        status, _headers, result = self.request("POST", "/api/voice/calls/start", {}, headers)
        self.assertEqual(status, 201)
        self.assertEqual(result["state"], "CALL_PENDING")
        self.assertTrue(result["local_mic_released"])
        self.assertFalse(local.opened)
        status, _headers, duplicate = self.request("POST", "/api/voice/calls/start", {}, headers)
        self.assertEqual(status, 409)

        status, _headers, answer = self.request(
            "POST", f"/api/voice/calls/{result['call_id']}/answer", {"sdp": "offer", "type": "offer"}, headers
        )
        self.assertEqual(status, 503)
        self.assertFalse(answer["media_available"])
        self.assertEqual(arbiter.state, CaptureState.PASSIVE)
        self.assertTrue(local.opened)

    def test_call_start_rejects_mic_release_failure(self):
        local = FakeLocalCapture(fail_close=True)
        arbiter = CaptureArbiter(local)
        arbiter.refresh_local()
        self.server.service.capture_arbiter = arbiter
        cookie, csrf = self.login()
        status, _headers, result = self.request(
            "POST", "/api/voice/calls/start", {}, self.mutation_headers(cookie, csrf)
        )
        self.assertEqual(status, 409)
        self.assertIn("release failed", result["error"])
        self.assertIsNone(self.server.service._active_call)

    def test_webrtc_answer_returns_sdp_and_keeps_capture_lease_until_hangup(self):
        local = FakeLocalCapture()
        arbiter = CaptureArbiter(local)
        arbiter.refresh_local()
        media = FakeCallMedia()
        self.server.service.capture_arbiter = arbiter
        self.server.service.call_media = media
        cookie, csrf = self.login()
        headers = self.mutation_headers(cookie, csrf)
        _status, _headers, call = self.request("POST", "/api/voice/calls/start", {}, headers)

        status, _headers, result = self.request(
            "POST", f"/api/voice/calls/{call['call_id']}/answer", {"sdp": "browser-offer", "type": "offer"}, headers
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["answer"], {"sdp": "answer-sdp", "type": "answer"})
        self.assertFalse(local.opened)
        self.assertEqual(self.server.service._active_call["call_id"], call["call_id"])
        self.request("POST", f"/api/voice/calls/{call['call_id']}/hangup", {}, headers)
        self.assertEqual(media.closed, [call["call_id"]])
        self.assertTrue(local.opened)

    def test_webrtc_negotiation_failure_returns_503_and_releases_audio_lease(self):
        local = FakeLocalCapture()
        arbiter = CaptureArbiter(local)
        arbiter.refresh_local()
        media = FakeCallMedia(fail=True)
        self.server.service.capture_arbiter = arbiter
        self.server.service.call_media = media
        cookie, csrf = self.login()
        headers = self.mutation_headers(cookie, csrf)
        _status, _headers, call = self.request("POST", "/api/voice/calls/start", {}, headers)

        status, _headers, result = self.request(
            "POST", f"/api/voice/calls/{call['call_id']}/answer", {"sdp": "browser-offer", "type": "offer"}, headers
        )

        self.assertEqual(status, 503)
        self.assertFalse(result["media_available"])
        self.assertEqual(media.closed, [call["call_id"]])
        self.assertIsNone(self.server.service._active_call)
        self.assertTrue(local.opened)

    def test_invalid_offer_releases_audio_lease(self):
        local = FakeLocalCapture()
        arbiter = CaptureArbiter(local)
        arbiter.refresh_local()
        media = FakeCallMedia()
        self.server.service.capture_arbiter = arbiter
        self.server.service.call_media = media
        cookie, csrf = self.login()
        headers = self.mutation_headers(cookie, csrf)
        _status, _headers, call = self.request("POST", "/api/voice/calls/start", {}, headers)

        status, _headers, result = self.request(
            "POST", f"/api/voice/calls/{call['call_id']}/answer", {"sdp": "bad", "type": "answer"}, headers
        )

        self.assertEqual(status, 400)
        self.assertIn("type 'offer'", result["error"])
        self.assertEqual(media.closed, [call["call_id"]])
        self.assertIsNone(self.server.service._active_call)
        self.assertTrue(local.opened)

    def test_expired_call_lease_cannot_finish_webrtc_negotiation(self):
        local = FakeLocalCapture()
        arbiter = CaptureArbiter(local)
        arbiter.refresh_local()
        media = FakeCallMedia()
        self.server.service.capture_arbiter = arbiter
        self.server.service.call_media = media
        cookie, csrf = self.login()
        headers = self.mutation_headers(cookie, csrf)
        _status, _headers, call = self.request("POST", "/api/voice/calls/start", {}, headers)
        self.server.service.capture_arbiter.end_call()
        status, _headers, result = self.request(
            "POST", f"/api/voice/calls/{call['call_id']}/answer", {"sdp": "offer", "type": "offer"}, headers
        )
        self.assertEqual(status, 409)
        self.assertIn("lease expired", result["error"])
        self.assertIsNone(self.server.service._active_call)

    def test_hangup_logout_and_server_close_release_call_lease(self):
        local = FakeLocalCapture()
        arbiter = CaptureArbiter(local)
        arbiter.refresh_local()
        self.server.service.capture_arbiter = arbiter
        cookie, csrf = self.login()
        headers = self.mutation_headers(cookie, csrf)
        status, _headers, call = self.request("POST", "/api/voice/calls/start", {}, headers)
        self.assertEqual(status, 201)
        status, _headers, result = self.request(
            "POST", f"/api/voice/calls/{call['call_id']}/hangup", {}, headers
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "PASSIVE")
        self.request("POST", "/api/voice/calls/start", {}, headers)
        self.request("POST", "/api/logout", {}, headers)
        self.assertEqual(arbiter.state, CaptureState.PASSIVE)

        cookie2, csrf2 = self.login()
        status, _headers, _call = self.request(
            "POST", "/api/voice/calls/start", {}, self.mutation_headers(cookie2, csrf2)
        )
        self.assertEqual(status, 201)
        self.server.service.close()
        self.assertEqual(arbiter.state, CaptureState.PASSIVE)

    def test_static_dashboard_serves_mobile_console(self):
        status, headers, body = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertTrue(any(name.lower() == "content-security-policy" for name, _value in headers))
        self.assertIn(b"RobotHarness", body)
        self.assertIn(b"aria-label", body)
        self.assertIn(b'id="battery-status" class="battery-status hidden"', body)
        self.assertIn('data-view="settings"'.encode(), body)
        self.assertIn("设置".encode(), body)
        self.assertIn(b'id="operator-username"', body)
        self.assertNotIn(b"operator-pin", body)

        status, _headers, body = self.request("GET", "/favicon.ico")
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")

    def test_dashboard_api_requires_authenticated_session(self):
        status, _headers, data = self.request("GET", "/api/dashboard/overview")

        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "Authentication required")

    def test_overview_exposes_base_robot_information_from_selected_profile(self):
        cookie, _csrf = self.login()

        status, _headers, data = self.request(
            "GET", "/api/dashboard/overview", headers={"Cookie": cookie}
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            data["robot_configuration"],
            {
                "robot_id": "robot-sim-basic",
                "manufacturer": "OpenRobotSoul",
                "model": "SimulatorBasic",
                "robot_module": "simulator",
                "profile_id": "simulator-basic",
                "adapter_id": "fake-simulator",
                "profile_version": "1.0.0",
            },
        )
        self.assertIsNone(data["robot"]["battery"])

    def test_overview_reports_battery_only_when_the_adapter_provides_it(self):
        cookie, _csrf = self.login()
        self.runtime.adapter.battery = lambda: BatteryStatus(76, True)

        status, _headers, data = self.request(
            "GET", "/api/dashboard/overview", headers={"Cookie": cookie}
        )

        self.assertEqual(status, 200)
        self.assertEqual(data["robot"]["battery"], {"level_percent": 76, "charging": True})
        self.assertTrue(data["robot"]["healthy"])

        self.runtime.adapter.battery = lambda: BatteryStatus(40, None)
        status, _headers, data = self.request(
            "GET", "/api/dashboard/overview", headers={"Cookie": cookie}
        )
        self.assertEqual(data["robot"]["battery"], {"level_percent": 40, "charging": None})

        def unavailable():
            raise RuntimeError("battery telemetry failed")

        self.runtime.adapter.battery = unavailable
        status, _headers, data = self.request(
            "GET", "/api/dashboard/overview", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertIsNone(data["robot"]["battery"])
        self.assertTrue(data["robot"]["healthy"])

    def test_wrong_and_rate_limited_password_are_rejected(self):
        for _ in range(5):
            status, _headers, _data = self.request(
                "POST", "/api/login", {"username": "operator", "password": "wrong-pin"}
            )
            self.assertEqual(status, 401)
        status, _headers, data = self.request(
            "POST", "/api/login", {"username": "operator", "password": "test-pin-4982"}
        )

        self.assertEqual(status, 401)
        self.assertIn("Too many", data["error"])

    def test_mutation_requires_csrf_even_with_authenticated_cookie(self):
        cookie, _csrf = self.login()

        status, _headers, data = self.request(
            "POST",
            "/api/tasks",
            {"text": "请抬头"},
            {"Cookie": cookie},
        )

        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "CSRF validation failed")
        self.assertEqual(self.runtime.adapter.executed_commands, [])

    def test_text_task_reports_the_invoked_action(self):
        cookie, csrf = self.login()

        status, _headers, data = self.request(
            "POST",
            "/api/tasks",
            {"text": "/head.look yaw_deg=0 pitch_deg=10"},
            self.mutation_headers(cookie, csrf),
        )

        self.assertEqual(status, 200)
        self.assertEqual(data["task"]["status"], "completed")
        self.assertEqual(data["task"]["invoked_action"]["skill"], "head.look")
        self.assertEqual(data["task"]["invoked_action"]["label"], "转动头部")
        self.assertIsNotNone(data["task"]["evaluation"])

    def test_denied_confirm_skill_is_still_reported(self):
        class DenyReviewer:
            def review(self, proposal):
                return SafetyReviewResult(ReviewVerdict.DENY, proposal.arguments_digest, "姿态不适合这个动作")

        runtime = RobotRuntime.from_profile(
            load_profile("simulator-basic"),
            agent=FixedAgent(AgentIntent("base.move_distance", {"distance_m": 0.2, "max_speed_mps": 0.1})),
            safety_reviewer=DenyReviewer(),
        )

        view = _task_result_view(runtime.run_text("前进"))

        self.assertEqual(view["status"], "failed")
        self.assertEqual(view["error"], "姿态不适合这个动作")
        self.assertEqual(view["invoked_action"]["skill"], "base.move_distance")
        self.assertEqual(view["invoked_action"]["label"], "直行一段距离")
        self.assertEqual(view["invoked_action"]["status"], "failed")

    def test_dashboard_control_uses_server_caller_and_executes_confirm_action(self):
        cookie, csrf = self.login()
        headers = self.mutation_headers(cookie, csrf)
        status, _response_headers, challenge = self.request(
            "POST",
            "/api/actions/prepare",
            {"skill": "base.move_distance", "parameters": {"distance_m": 0.1, "max_speed_mps": 0.1}, "caller": "agent", "risk_level": "normal"},
            headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(challenge["risk_level"], "confirm")
        self.assertEqual(challenge["label"], "直行一段距离")
        self.assertTrue(challenge["ai_review_required"])
        self.assertEqual(challenge["requires_operator_confirmation"], False)

        status, _response_headers, result = self.request(
            "POST",
            f"/api/actions/{challenge['challenge_id']}/execute",
            {"caller": "agent", "operator_confirmed": False},
            headers,
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["action"]["status"], "completed")
        self.assertEqual(result["action"]["review"]["verdict"], "allow")
        command = self.runtime.adapter.executed_commands[0]
        self.assertEqual(command.invocation_source.value, "dashboard")

    def test_prepare_rejects_invalid_parameters_without_creating_challenge(self):
        cookie, csrf = self.login()
        before = len(self.runtime.adapter.executed_commands)
        status, _response_headers, data = self.request(
            "POST",
            "/api/actions/prepare",
            {"skill": "head.look", "parameters": {"yaw_deg": 500, "pitch_deg": 0}},
            self.mutation_headers(cookie, csrf),
        )

        self.assertEqual(status, 403)
        self.assertIn("exceeds its maximum", data["error"])
        self.assertEqual(self.server.service._challenges, {})
        self.assertEqual(len(self.runtime.adapter.executed_commands), before)

    def test_policy_change_invalidates_an_unexecuted_challenge(self):
        cookie, csrf = self.login()
        headers = self.mutation_headers(cookie, csrf)
        status, _response_headers, challenge = self.request(
            "POST",
            "/api/actions/prepare",
            {"skill": "head.look", "parameters": {"yaw_deg": 0, "pitch_deg": 10}},
            headers,
        )
        self.assertEqual(status, 201)
        capability = self.runtime.capabilities["head.look"]
        self.runtime.capabilities["head.look"] = replace(capability, timeout_ms=capability.timeout_ms + 1)
        before = len(self.runtime.adapter.executed_commands)

        status, _response_headers, data = self.request(
            "POST",
            "/api/actions/execute",
            {"challenge_id": challenge["challenge_id"]},
            headers,
        )

        self.assertEqual(status, 403)
        self.assertIn("does not match", data["error"])
        self.assertEqual(len(self.runtime.adapter.executed_commands), before)

    def test_challenge_cannot_be_used_from_another_session(self):
        first_cookie, first_csrf = self.login()
        status, _headers, challenge = self.request(
            "POST",
            "/api/actions/prepare",
            {"skill": "head.look", "parameters": {"yaw_deg": 0, "pitch_deg": 10}},
            self.mutation_headers(first_cookie, first_csrf),
        )
        self.assertEqual(status, 201)
        second_cookie, second_csrf = self.login()
        before = len(self.runtime.adapter.executed_commands)

        status, _headers, data = self.request(
            "POST",
            "/api/actions/execute",
            {"challenge_id": challenge["challenge_id"]},
            self.mutation_headers(second_cookie, second_csrf),
        )

        self.assertEqual(status, 403)
        self.assertIn("invalid or already used", data["error"])
        self.assertEqual(len(self.runtime.adapter.executed_commands), before)
        self.assertIn(challenge["challenge_id"], self.server.service._challenges)

        status, _headers, result = self.request(
            "POST",
            "/api/actions/execute",
            {"challenge_id": challenge["challenge_id"]},
            self.mutation_headers(first_cookie, first_csrf),
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["action"]["status"], "completed")

    def test_challenge_is_one_use_and_bound_to_session(self):
        cookie, csrf = self.login()
        headers = self.mutation_headers(cookie, csrf)
        status, _response_headers, challenge = self.request(
            "POST", "/api/actions/prepare", {"skill": "head.look", "parameters": {"yaw_deg": 0, "pitch_deg": 10}}, headers
        )
        self.assertEqual(status, 201)
        execute_path = "/api/actions/execute"
        status, _response_headers, _data = self.request("POST", execute_path, {"challenge_id": challenge["challenge_id"]}, headers)
        self.assertEqual(status, 200)
        status, _response_headers, data = self.request("POST", execute_path, {"challenge_id": challenge["challenge_id"]}, headers)

        self.assertEqual(status, 403)
        self.assertIn("already used", data["error"])
        self.assertEqual(len(self.runtime.adapter.executed_commands), 1)

    def test_high_risk_action_needs_stepup_pin_and_cannot_be_replayed(self):
        profile = load_profile("simulator-basic")
        high = CapabilitySpec(
            "maintenance.test",
            {"enabled": ParameterSpec("boolean")},
            risk_level=RiskLevel.HIGH,
            allowed_callers=("dashboard",),
            requires_operator_confirmation=True,
        )
        profile = replace(profile, capabilities=(*profile.capabilities, high))
        self.runtime.adapter.capabilities[high.name] = high
        self.runtime = RobotRuntime.from_profile(profile, adapter=self.runtime.adapter, agent=FixedAgent(AgentIntent("head.look", {"yaw_deg": 0, "pitch_deg": 0})))
        self.server.service.runtime = self.runtime
        cookie, csrf = self.login()
        headers = self.mutation_headers(cookie, csrf)
        status, _response_headers, challenge = self.request(
            "POST", "/api/actions/prepare", {"skill": high.name, "parameters": {"enabled": False}}, headers
        )
        self.assertEqual(status, 201)
        path = "/api/actions/execute"
        before = len(self.runtime.adapter.executed_commands)
        status, _response_headers, data = self.request("POST", path, {"challenge_id": challenge["challenge_id"]}, headers)

        self.assertEqual(status, 403)
        self.assertIn("operator password", data["error"])
        self.assertEqual(len(self.runtime.adapter.executed_commands), before)

        status, _response_headers, challenge = self.request(
            "POST", "/api/actions/prepare", {"skill": high.name, "parameters": {"enabled": False}}, headers
        )
        self.assertEqual(status, 201)
        status, _response_headers, result = self.request(
            "POST",
            path,
            {"challenge_id": challenge["challenge_id"], "operator_password": "test-pin-4982"},
            headers,
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["action"]["operator_confirmed"])
        command = self.runtime.adapter.executed_commands[-1]
        self.assertEqual(command.risk_level, RiskLevel.HIGH)
        self.assertEqual(command.invocation_source.value, "dashboard")
        self.assertTrue(command.operator_confirmed)
        self.assertTrue(command.actor_hash)
        status, _response_headers, data = self.request("POST", path, {"challenge_id": challenge["challenge_id"]}, headers)
        self.assertEqual(status, 403)
        self.assertEqual(len(self.runtime.adapter.executed_commands), before + 1)

    def test_lan_bind_requires_tls(self):
        with self.assertRaisesRegex(ValueError, "requires TLS"):
            create_dashboard_server(self.runtime, operator_password="test-pin-4982", host="192.168.1.40")

    def test_runtime_log_requires_login_and_returns_prefixed_lines(self):
        import tempfile
        from pathlib import Path

        from robotsoul.runlog import configure, current

        previous = current().directory
        with tempfile.TemporaryDirectory() as directory:
            configure(directory)
            try:
                current().write("Voice", "Info", "唤醒：索拉")
                current().write("Dashboard", "Error", "启动失败")
                status, _headers, data = self.request("GET", "/api/logs")
                self.assertEqual(status, 401)
                self.assertEqual(data["error"], "Authentication required")
                self.connection.close()
                self.connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
                cookie, _csrf = self.login()

                status, _headers, data = self.request("GET", "/api/logs", headers={"Cookie": cookie})
                self.assertEqual(status, 200)
                self.assertEqual(len(data["lines"]), 2)
                self.assertIn("-Dashboard-Error] 启动失败", data["lines"][0])
                self.assertIn("-Voice-Info] 唤醒：索拉", data["lines"][1])
                self.assertTrue(any(path.suffix == ".log" for path in Path(directory).iterdir()))
                self.assertFalse(any(path.name.endswith(".jsonl.1") for path in Path(directory).iterdir()))

                status, _headers, filtered = self.request("GET", "/api/logs?level=Info", headers={"Cookie": cookie})
                self.assertEqual(status, 200)
                self.assertEqual(len(filtered["lines"]), 1)
                self.assertIn("唤醒：索拉", filtered["lines"][0])

                status, _headers, rejected = self.request("GET", "/api/logs?level=debug", headers={"Cookie": cookie})
                self.assertEqual(status, 400)

                status, _headers, overview = self.request("GET", "/api/dashboard/overview", headers={"Cookie": cookie})
                self.assertEqual(status, 200)
                self.assertNotIn("voice_events", overview)
                status, _headers, missing = self.request("GET", "/api/voice/events", headers={"Cookie": cookie})
                self.assertEqual(status, 404)
            finally:
                configure(previous)

    def test_audio_monitor_requires_login_and_returns_levels(self):
        from types import SimpleNamespace

        previous = self.server.service.capture_arbiter
        self.server.service.capture_arbiter = SimpleNamespace(monitor=SimpleNamespace(snapshot=lambda: {
            "input": [{"min": -0.2, "max": 0.4}],
            "output": [],
            "input_active": True,
            "output_active": False,
        }))
        try:
            status, _headers, data = self.request("GET", "/api/voice/monitor")
            self.assertEqual(status, 401)
            self.connection.close()
            self.connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            cookie, _csrf = self.login()
            status, _headers, data = self.request("GET", "/api/voice/monitor", headers={"Cookie": cookie})
            self.assertEqual(status, 200)
            self.assertEqual(data["input"], [{"min": -0.2, "max": 0.4}])
            self.assertTrue(data["input_active"])
            self.assertFalse(data["output_active"])
            status, _headers, filtered = self.request("GET", "/api/voice/monitor?channels=input", headers={"Cookie": cookie})
            self.assertEqual(status, 200)
            self.assertEqual(filtered["input"], [{"min": -0.2, "max": 0.4}])
            self.assertNotIn("output", filtered)
            self.assertNotIn("output_active", filtered)
            status, _headers, paused = self.request("GET", "/api/voice/monitor?channels=", headers={"Cookie": cookie})
            self.assertEqual(status, 200)
            self.assertEqual(paused, {})
        finally:
            self.server.service.capture_arbiter = previous

    def test_wildcard_bind_is_rejected_even_with_tls(self):
        with self.assertRaisesRegex(ValueError, "wildcard address"):
            create_dashboard_server(
                self.runtime,
                operator_password="test-pin-4982",
                host="0.0.0.0",
                tls_cert="cert.pem",
                tls_key="key.pem",
            )


    def test_settings_replace_pin_and_keep_secrets_out_of_responses(self):
        import tempfile
        from pathlib import Path

        status, _headers, data = self.request("POST", "/api/login", {"pin": "test-pin-4982"})
        self.assertEqual(status, 401)
        self.assertIn("username or password", data["error"])
        self.connection.close()
        self.connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        cookie, csrf = self.login()
        headers = self.mutation_headers(cookie, csrf)
        status, _headers, denied = self.request("GET", "/api/settings")
        self.assertEqual(status, 401)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operators = root / "operators.json"
            llm = root / "llm.json"
            voice = root / "voice.json"
            voice.write_text(
                (Path(__file__).parents[1] / "config" / "voice.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            self.server.service._operators.path = operators
            self.server.service._operators.upsert("operator", "test-pin-4982")
            self.server.service._llm = LlmConfigStore(llm)
            self.server.service._voice_config_path = voice
            self.connection.close()
            self.connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)

            status, _headers, settings = self.request("GET", "/api/settings", headers={"Cookie": cookie})
            self.assertEqual(status, 200)
            self.assertEqual(settings["current_username"], "operator")
            self.assertEqual(settings["accounts"], [{"username": "operator"}])
            self.assertFalse(settings["llm"]["api_key_configured"])
            self.assertEqual(settings["audio"]["robot_name"], "索拉")
            self.assertNotIn("digest", json.dumps(settings))

            secret = "sk-test-secret-value"
            status, _headers, saved = self.request(
                "POST",
                "/api/settings/llm",
                {
                    "provider": "openai-compatible",
                    "base_url": "https://example.com/v1",
                    "model": "demo-model",
                    "timeout_seconds": 20,
                    "api_key": secret,
                },
                headers,
            )
            self.assertEqual(status, 200)
            self.assertTrue(saved["llm"]["api_key_configured"])
            self.assertNotIn(secret, json.dumps(saved))
            self.assertEqual(json.loads(llm.read_text(encoding="utf-8"))["api_key"], secret)
            self.assertEqual(llm.stat().st_mode & 0o777, 0o600)

            status, _headers, audio = self.request(
                "POST",
                "/api/settings/audio",
                {
                    "robot_name": "小拉",
                    "wake_phrase": "小拉",
                    "language": "zh-CN",
                    "input_device": "usb-microphone",
                    "output_device": "usb-speaker",
                    "follow_up_seconds": 6,
                    "session_max_seconds": 90,
                    "call_reconnect_grace_seconds": 12,
                },
                headers,
            )
            self.assertEqual(status, 200)
            self.assertEqual(audio["audio"]["robot_name"], "小拉")
            self.assertEqual(audio["restart_required"], ["wake_phrase", "language", "input_device", "output_device"])
            rewritten = json.loads(voice.read_text(encoding="utf-8"))
            self.assertEqual(rewritten["robot_name"], "小拉")
            self.assertEqual(rewritten["models"]["kws"], "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20")
            status, _headers, rejected = self.request(
                "POST", "/api/settings/audio", {"input_device": "microphone"}, headers
            )
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(voice.read_text(encoding="utf-8"))["input_device"], "usb-microphone")

            status, _headers, accounts = self.request(
                "POST",
                "/api/settings/accounts",
                {"username": "maintainer", "password": "another-pass-4982", "current_password": "test-pin-4982"},
                headers,
            )
            self.assertEqual(status, 200)
            self.assertEqual([item["username"] for item in accounts["accounts"]], ["maintainer", "operator"])
            stored = operators.read_text(encoding="utf-8")
            self.assertIn('"username": "maintainer"', stored)
            self.assertNotIn("another-pass-4982", stored)
            self.assertNotIn("test-pin-4982", stored)
            self.assertIn("pbkdf2-hmac-sha256", stored)
            self.assertNotIn("md5", stored.lower())
            self.assertEqual(operators.stat().st_mode & 0o777, 0o600)

            self.connection.close()
            self.connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            maintainer_cookie, _maintainer_csrf = self.login("maintainer", "another-pass-4982")
            status, _headers, current = self.request("GET", "/api/settings", headers={"Cookie": maintainer_cookie})
            self.assertEqual(status, 200)
            self.assertEqual(current["current_username"], "maintainer")

            status, _headers, denied = self.request(
                "POST",
                "/api/settings/accounts/delete",
                {"username": "operator", "current_password": "not-the-password"},
                headers,
            )
            self.assertEqual(status, 403)
            status, _headers, removed = self.request(
                "POST",
                "/api/settings/accounts/delete",
                {"username": "maintainer", "current_password": "test-pin-4982"},
                headers,
            )
            self.assertEqual(status, 200)
            self.assertEqual(removed["accounts"], [{"username": "operator"}])
            status, _headers, kept = self.request(
                "POST",
                "/api/settings/accounts/delete",
                {"username": "operator", "current_password": "test-pin-4982"},
                headers,
            )
            self.assertEqual(status, 400)
            self.assertIn("signed-in", kept["error"])


class DashboardOperatorConfirmTests(unittest.TestCase):
    def test_confirm_actions_stay_available_without_ai_reviewer(self):
        runtime = RobotRuntime.from_profile(load_profile("simulator-basic"))
        service = DashboardService(runtime, operators=OperatorDirectory.from_credentials("operator", "test-pin-4982"))
        view = service._capability_view("base.move_distance", runtime.capabilities["base.move_distance"])

        self.assertEqual(view["label"], "直行一段距离")
        self.assertEqual(view["risk_level"], "confirm")
        self.assertTrue(view["enabled"])
        self.assertTrue(view["dashboard_allowed"])
        self.assertFalse(view["ai_review_required"])
        self.assertEqual(view["parameters"]["distance_m"]["label"], "距离（米）")


if __name__ == "__main__":
    unittest.main()
