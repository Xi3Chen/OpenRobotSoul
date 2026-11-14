import unittest

from robotsoul.models import CapabilitySpec, ParameterSpec
from robotsoul.voice.commands import match_closed_command


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

    def test_unknown_phrase_or_missing_skill_is_left_for_the_language_gateway(self):
        capabilities = {"sport.stand_up": CapabilitySpec("sport.stand_up", {})}
        self.assertIsNone(match_closed_command("向前走两步", capabilities))
        self.assertIsNone(match_closed_command("向前", capabilities))
        self.assertIsNone(match_closed_command("今天天气怎么样", {"sport.move": _move_capability()}))

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
