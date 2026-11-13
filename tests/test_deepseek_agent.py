from __future__ import annotations

import json
import socket
import unittest

from robotsoul.deepseek_agent import DeepSeekAgent, DeepSeekAgentError
from robotsoul.models import CapabilitySpec, ParameterSpec, RiskLevel


def _capabilities():
    return {
        "flash.toggle": CapabilitySpec(
            "flash.toggle", {"enabled": ParameterSpec("boolean")}, RiskLevel.NORMAL),
    }


def _response(content):
    return 200, json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


class DeepSeekAgentTests(unittest.TestCase):
    def _agent(self, transport):
        return DeepSeekAgent("sk-test", transport=transport)

    def test_valid_intent_is_returned_with_declared_parameters(self):
        captured = {}

        def transport(url, headers, payload, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json.loads(payload)
            captured["timeout"] = timeout
            return _response(json.dumps({"skill": "flash.toggle", "parameters": {"enabled": True},
                                         "rationale": "打开手电筒"}))

        intent = self._agent(transport).plan("打开手电筒", _capabilities())
        self.assertIsNotNone(intent)
        self.assertEqual(intent.skill, "flash.toggle")
        self.assertEqual(intent.parameters, {"enabled": True})
        self.assertEqual(intent.rationale, "打开手电筒")
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(captured["payload"]["model"], "deepseek-flash")
        self.assertEqual(captured["payload"]["response_format"], {"type": "json_object"})

    def test_unknown_skill_is_rejected(self):
        def transport(url, headers, payload, timeout):
            return _response(json.dumps({"skill": "base.move_forever", "parameters": {}}))

        self.assertIsNone(self._agent(transport).plan("向前走", _capabilities()))

    def test_wrong_parameter_type_is_rejected(self):
        def transport(url, headers, payload, timeout):
            return _response(json.dumps({"skill": "flash.toggle", "parameters": {"enabled": "yes"}}))

        self.assertIsNone(self._agent(transport).plan("打开手电筒", _capabilities()))

    def test_missing_or_null_skill_returns_none(self):
        def transport(url, headers, payload, timeout):
            return _response(json.dumps({"skill": None, "parameters": {}}))

        self.assertIsNone(self._agent(transport).plan("今天天气怎么样", _capabilities()))

    def test_http_error_raises(self):
        def transport(url, headers, payload, timeout):
            return 401, b'{"error": "invalid key"}'

        with self.assertRaises(DeepSeekAgentError):
            self._agent(transport).plan("打开手电筒", _capabilities())

    def test_timeout_raises_timeout_error(self):
        def transport(url, headers, payload, timeout):
            raise socket.timeout("slow")

        with self.assertRaises(TimeoutError):
            self._agent(transport).plan("打开手电筒", _capabilities())

    def test_malformed_response_raises(self):
        def transport(url, headers, payload, timeout):
            return 200, b"not json"

        with self.assertRaises(DeepSeekAgentError):
            self._agent(transport).plan("打开手电筒", _capabilities())

    def test_missing_api_key_is_rejected(self):
        with self.assertRaises(ValueError):
            DeepSeekAgent("")

    def test_model_and_base_url_override(self):
        captured = {}

        def transport(url, headers, payload, timeout):
            captured["url"] = url
            captured["payload"] = json.loads(payload)
            return _response(json.dumps({"skill": None, "parameters": {}}))

        agent = DeepSeekAgent("sk-test", model="deepseek-flash", base_url="https://api.deepseek.com/",
                              transport=transport)
        agent.plan("打开手电筒", _capabilities())
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["payload"]["model"], "deepseek-flash")


if __name__ == "__main__":
    unittest.main()
