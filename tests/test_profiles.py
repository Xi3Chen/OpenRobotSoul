from __future__ import annotations

import unittest

from robotsoul.models import RiskLevel
from robotsoul.profiles import _parse_capability, load_profile


class CapabilityProfileTests(unittest.TestCase):
    def test_simulator_profiles_explicitly_classify_all_skills(self):
        for profile_id in ("simulator-basic", "simulator-compact"):
            profile = load_profile(profile_id)
            self.assertTrue(all(capability.allowed_callers for capability in profile.capabilities))
            self.assertTrue(all(capability.risk_level in RiskLevel for capability in profile.capabilities))

    def test_capability_requires_explicit_risk_and_caller_policy(self):
        with self.assertRaisesRegex(ValueError, "Missing capability fields"):
            _parse_capability({"name": "test", "parameters": {}})

    def test_unknown_risk_and_callers_fail_closed(self):
        base = {
            "name": "test",
            "parameters": {},
            "risk_level": "unexpected",
            "allowed_callers": ["agent"],
            "requires_operator_confirmation": False,
        }
        with self.assertRaisesRegex(ValueError, "risk_level"):
            _parse_capability(base)
        base["risk_level"] = "normal"
        base["allowed_callers"] = ["browser"]
        with self.assertRaisesRegex(ValueError, "allowed_callers"):
            _parse_capability(base)

    def test_high_risk_must_exclude_agent_and_require_operator_confirmation(self):
        base = {
            "name": "dangerous",
            "parameters": {},
            "risk_level": "high",
            "allowed_callers": ["agent", "dashboard"],
            "requires_operator_confirmation": True,
        }
        with self.assertRaisesRegex(ValueError, "cannot allow Agent"):
            _parse_capability(base)
        base["allowed_callers"] = ["dashboard"]
        base["requires_operator_confirmation"] = False
        with self.assertRaisesRegex(ValueError, "must require operator confirmation"):
            _parse_capability(base)

    def test_boolean_parameters_have_no_numeric_bounds(self):
        base = {
            "name": "switch",
            "parameters": {"enable": {"type": "boolean"}},
            "risk_level": "normal",
            "allowed_callers": ["agent", "dashboard"],
            "requires_operator_confirmation": False,
        }
        self.assertEqual(_parse_capability(base).parameters["enable"].value_type, "boolean")
        base["parameters"]["enable"]["min"] = 0
        with self.assertRaisesRegex(ValueError, "cannot declare numeric"):
            _parse_capability(base)


if __name__ == "__main__":
    unittest.main()
