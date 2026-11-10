from __future__ import annotations

import json
import threading
import unittest
from dataclasses import replace
from http.client import HTTPConnection

from robotsoul.dashboard import create_dashboard_server
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


class AllowReviewer:
    def review(self, proposal):
        return SafetyReviewResult(ReviewVerdict.ALLOW, proposal.arguments_digest, "checked")


class FixedAgent:
    def __init__(self, intent):
        self.intent = intent

    def plan(self, *_args, **_kwargs):
        return self.intent


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        profile = load_profile("simulator-basic")
        self.runtime = RobotRuntime.from_profile(profile, safety_reviewer=AllowReviewer())
        self.server = create_dashboard_server(self.runtime, operator_pin="test-pin-4982")
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

    def login(self, pin="test-pin-4982"):
        status, headers, data = self.request("POST", "/api/login", {"pin": pin})
        self.assertEqual(status, 200)
        cookie = next(value for name, value in headers if name.lower() == "set-cookie").split(";", 1)[0]
        return cookie, data["csrf_token"]

    def mutation_headers(self, cookie, csrf):
        return {"Cookie": cookie, "X-CSRF-Token": csrf}

    def test_static_dashboard_serves_mobile_console(self):
        status, headers, body = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertTrue(any(name.lower() == "content-security-policy" for name, _value in headers))
        self.assertIn(b"RobotHarness", body)
        self.assertIn(b"aria-label", body)

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

    def test_wrong_and_rate_limited_pin_are_rejected(self):
        for _ in range(5):
            status, _headers, _data = self.request("POST", "/api/login", {"pin": "wrong-pin"})
            self.assertEqual(status, 401)
        status, _headers, data = self.request("POST", "/api/login", {"pin": "test-pin-4982"})

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
        self.assertIn("operator PIN", data["error"])
        self.assertEqual(len(self.runtime.adapter.executed_commands), before)

        status, _response_headers, challenge = self.request(
            "POST", "/api/actions/prepare", {"skill": high.name, "parameters": {"enabled": False}}, headers
        )
        self.assertEqual(status, 201)
        status, _response_headers, result = self.request(
            "POST",
            path,
            {"challenge_id": challenge["challenge_id"], "operator_pin": "test-pin-4982"},
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
            create_dashboard_server(self.runtime, operator_pin="test-pin-4982", host="192.168.1.40")

    def test_wildcard_bind_is_rejected_even_with_tls(self):
        with self.assertRaisesRegex(ValueError, "wildcard address"):
            create_dashboard_server(
                self.runtime,
                operator_pin="test-pin-4982",
                host="0.0.0.0",
                tls_cert="cert.pem",
                tls_key="key.pem",
            )


if __name__ == "__main__":
    unittest.main()
