from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from robotsoul.language_gateway import _identity_preamble, _resolve_identity
from robotsoul.robot_config import load_robot_deployment_config, parse_wake_phrases, save_robot_deployment_config
from robotsoul.voice.config import load_voice_config


class RobotConfigTests(unittest.TestCase):
    def test_wake_phrase_defaults_to_robot_name_when_empty(self):
        phrases = parse_wake_phrases("", "索拉")
        self.assertEqual(phrases, ("索拉",))
        multi = parse_wake_phrases("小拉,索拉", "索拉")
        self.assertEqual(multi, ("小拉", "索拉"))

    def test_voice_config_allows_empty_wake_phrase_without_robot_name(self):
        root = Path(__file__).parents[1]
        config = load_voice_config(root / "config" / "voice.json")
        self.assertEqual(config.wake_phrase, "索拉")
        self.assertNotIn("robot_name", {item.name for item in fields(config)})

    def test_robot_deployment_config_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "robot.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "robot_module": "simulator",
                        "robot_name": "小贝",
                        "soul": {"personality": "稳重、简洁"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            loaded = load_robot_deployment_config(path)
            self.assertEqual(loaded.robot_name, "小贝")
            self.assertEqual(loaded.soul.personality, "稳重、简洁")
            updated = save_robot_deployment_config(
                path,
                {"robot_name": "小拉", "soul": {"personality": "活泼"}},
            )
            self.assertEqual(updated.robot_name, "小拉")
            self.assertEqual(updated.soul.personality, "活泼")

    def test_identity_preamble_includes_name_and_soul(self):
        request = {"identity": {"robot_name": "索拉", "soul": "活泼"}}
        identity = _resolve_identity(request)
        preamble = _identity_preamble(identity)
        self.assertIn("索拉", preamble)
        self.assertIn("活泼", preamble)


if __name__ == "__main__":
    unittest.main()