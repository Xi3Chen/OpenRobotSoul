import unittest
from unittest.mock import patch

from robotsoul.models import CapabilitySpec, ParameterSpec
from robotsoul.voice.commands import match_closed_command
from robotsoul.voice.transcript import wake_prefix_candidates


def _move_capability() -> CapabilitySpec:
    number = ParameterSpec("number", 200, 2000)
    speed = ParameterSpec("number", -2.5, 3.8)
    lateral = ParameterSpec("number", -1, 1)
    yaw = ParameterSpec("number", -4, 4)
    return CapabilitySpec(
        "sport.move",
        {"vx": speed, "vy": lateral, "vyaw": yaw, "duration_ms": number},
    )


class ClosedCommandTests(unittest.TestCase):
    def test_stand_and_move_phrases_use_registered_skills(self):
        capabilities = {
            "sport.stand_up": CapabilitySpec("sport.stand_up", {}),
            "sport.stand_down": CapabilitySpec("sport.stand_down", {}),
            "sport.move": _move_capability(),
            "motion.turn_around": CapabilitySpec("motion.turn_around", {}),
            "motion.turn_left": CapabilitySpec("motion.turn_left", {}),
            "motion.turn_right": CapabilitySpec("motion.turn_right", {}),
            "expression.nod": CapabilitySpec(
                "expression.nod", {"count": ParameterSpec("number", 1, 5)}
            ),
            "expression.shake_head": CapabilitySpec(
                "expression.shake_head", {"count": ParameterSpec("number", 1, 5)}
            ),
            "expression.tilt_head": CapabilitySpec(
                "expression.tilt_head", {"direction": ParameterSpec("string", max_length=5)}
            ),
            "motion.back_up": CapabilitySpec(
                "motion.back_up", {"distance_m": ParameterSpec("number", 0.2, 3.0)}
            ),
            "motion.strafe_left": CapabilitySpec(
                "motion.strafe_left", {"distance_m": ParameterSpec("number", 0.2, 1.5)}
            ),
            "motion.strafe_right": CapabilitySpec(
                "motion.strafe_right", {"distance_m": ParameterSpec("number", 0.2, 1.5)}
            ),
        }
        stand = match_closed_command("索拉，站起来", capabilities, wake_phrase="索拉")
        self.assertEqual(stand.skill, "sport.stand_up")
        self.assertEqual(stand.parameters, {})
        move = match_closed_command("向前走两步", capabilities)
        self.assertEqual(move.skill, "sport.move")
        self.assertEqual(move.parameters["vx"], 0.3)
        self.assertEqual(move.parameters["duration_ms"], 2000)
        backward = match_closed_command("向后走", capabilities)
        self.assertEqual(backward.parameters["vx"], -0.3)
        self.assertEqual(backward.parameters["duration_ms"], 1000)
        self.assertEqual(match_closed_command("趴下", capabilities).skill, "sport.stand_down")
        self.assertEqual(match_closed_command("向后转", capabilities).skill, "motion.turn_around")
        self.assertEqual(match_closed_command("左转", capabilities).skill, "motion.turn_left")
        nod = match_closed_command("点头", capabilities)
        self.assertEqual(nod.skill, "expression.nod")
        self.assertEqual(nod.parameters, {"count": 1})
        self.assertEqual(match_closed_command("点两下头", capabilities).parameters["count"], 2)
        self.assertEqual(match_closed_command("摇头", capabilities).skill, "expression.shake_head")
        self.assertEqual(
            match_closed_command("歪头", capabilities).parameters,
            {"direction": "left"},
        )
        self.assertEqual(
            match_closed_command("向右歪头", capabilities).parameters,
            {"direction": "right"},
        )
        self.assertEqual(match_closed_command("后退", capabilities).skill, "motion.back_up")
        self.assertEqual(match_closed_command("后退一米", capabilities).parameters["distance_m"], 1.0)
        self.assertEqual(match_closed_command("向后走1米", capabilities).skill, "motion.back_up")
        self.assertEqual(match_closed_command("向左侧移", capabilities).skill, "motion.strafe_left")
        self.assertEqual(
            match_closed_command("向右平移半米", capabilities),
            None,
        )
        self.assertEqual(
            match_closed_command("向右平移0.5米", capabilities).parameters,
            {"distance_m": 0.5},
        )
        plan = match_closed_command("索拉站起来，向后转", capabilities, wake_phrase="索拉")
        self.assertEqual([step.skill for step in plan.steps], ["sport.stand_up", "motion.turn_around"])

    def test_unknown_phrase_or_missing_skill_is_left_for_the_language_gateway(self):
        capabilities = {"sport.stand_up": CapabilitySpec("sport.stand_up", {})}
        self.assertIsNone(match_closed_command("向前走两步", capabilities))
        self.assertIsNone(match_closed_command("向前", capabilities))
        self.assertIsNone(match_closed_command("今天天气怎么样", {"sport.move": _move_capability()}))
        self.assertIsNone(
            match_closed_command(
                "今天向后转",
                {"motion.turn_around": CapabilitySpec("motion.turn_around", {})},
                wake_phrase="索拉",
            )
        )

    def test_misheard_wake_prefix_still_matches_turn_around(self):
        capabilities = {
            "motion.turn_around": CapabilitySpec("motion.turn_around", {}),
            "sport.stand_up": CapabilitySpec("sport.stand_up", {}),
        }
        pronunciations = {
            "索拉": ["suo", "la"],
            "左拉": ["zuo", "la"],
            "所拉": ["suo", "la"],
            "佐拉": ["zuo", "la"],
            "小月": ["xiao", "yue"],
            "晓悦": ["xiao", "yue"],
            "佐那": ["zuo", "na"],
        }
        with patch("robotsoul.voice.transcript._toneless_pinyin", side_effect=pronunciations.get):
            self.assertEqual(
                match_closed_command("左拉，向后转。", capabilities, wake_phrase="索拉").skill,
                "motion.turn_around",
            )
            self.assertEqual(
                match_closed_command("所拉向后转", capabilities, wake_phrase="索拉").skill,
                "motion.turn_around",
            )
            self.assertEqual(
                match_closed_command("佐拉站起来。", capabilities, wake_phrase="索拉").skill,
                "sport.stand_up",
            )
            self.assertEqual(
                match_closed_command("晓悦站起来", capabilities, wake_phrase="小月").skill,
                "sport.stand_up",
            )
            self.assertIsNone(
                match_closed_command("佐那站起来", capabilities, wake_phrase="索拉")
            )
            self.assertIsNone(
                match_closed_command("佐拉站起来", capabilities, wake_phrase="小月")
            )

    def test_only_configured_wake_prefix_is_removed(self):
        capabilities = {"motion.turn_around": CapabilitySpec("motion.turn_around", {})}
        self.assertIsNone(
            match_closed_command(
                "佐拉，向后转",
                capabilities,
                wake_phrase="小月",
            )
        )
        self.assertIsNone(
            match_closed_command(
                "说明一下，向后转",
                capabilities,
                wake_phrase="索拉",
            )
        )

    def test_wake_prefix_candidates_apply_similarity_threshold_and_punctuation(self):
        pronunciations = {
            "索拉": ["suo", "la"],
            "左拉": ["zuo", "la"],
            "所你": ["suo", "ni"],
        }
        with patch("robotsoul.voice.transcript._toneless_pinyin", side_effect=pronunciations.get):
            self.assertEqual(
                wake_prefix_candidates("左拉，向后转。", "索拉"),
                ["向后转", "左拉，向后转"],
            )
            self.assertEqual(
                wake_prefix_candidates("所你站起来", "索拉"),
                ["所你站起来"],
            )

    def test_meters_and_minutes_use_separate_move_skills(self):
        speed = ParameterSpec("number", -2.5, 3.8)
        lateral = ParameterSpec("number", -1, 1)
        yaw = ParameterSpec("number", -4, 4)
        capabilities = {
            "sport.move": CapabilitySpec(
                "sport.move",
                {"vx": speed, "vy": lateral, "vyaw": yaw, "duration_ms": ParameterSpec("number", 200, 120000)},
            ),
            "sport.move_distance": CapabilitySpec(
                "sport.move_distance",
                {"vx": speed, "vy": lateral, "vyaw": yaw, "distance_m": ParameterSpec("number", 0.2, 8)},
            ),
        }

        distance = match_closed_command("向前走4米", capabilities)
        minute = match_closed_command("向前走一分钟", capabilities)
        seconds = match_closed_command("向后走4秒", capabilities)

        self.assertEqual(distance.skill, "sport.move_distance")
        self.assertEqual(distance.parameters["distance_m"], 4)
        self.assertEqual(minute.skill, "sport.move")
        self.assertEqual(minute.parameters["duration_ms"], 60000)
        self.assertEqual(seconds.parameters["vx"], -0.3)
        self.assertEqual(seconds.parameters["duration_ms"], 4000)

    def test_duration_stays_inside_the_declared_range(self):
        move = match_closed_command("向前走十步", {"sport.move": _move_capability()})
        self.assertEqual(move.parameters["duration_ms"], 2000)


if __name__ == "__main__":
    unittest.main()
