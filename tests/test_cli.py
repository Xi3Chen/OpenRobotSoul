import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from robotsoul.cli import main
from robotsoul.profiles import load_profile


class StatusCommandTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile("unitree-go2-development")

    def run_status(self, adapter):
        output = io.StringIO()
        with patch("sys.argv", ["robotsoul", "--robot-module", "unitree_go2", "status"]), \
             patch("robotsoul.cli.load_selected_profile", return_value=self.profile), \
             patch("robotsoul.cli.create_adapter", return_value=adapter), \
             contextlib.redirect_stdout(output):
            try:
                main()
            except SystemExit as exc:
                return exc.code, json.loads(output.getvalue())
        return 0, json.loads(output.getvalue())

    def test_status_returns_structured_connection_failure_and_closes_adapter(self):
        adapter = Mock()
        adapter.connect.side_effect = RuntimeError("DDS state is unavailable")

        exit_code, result = self.run_status(adapter)

        self.assertEqual(exit_code, 1)
        self.assertFalse(result["connected"])
        self.assertEqual(result["error"], "DDS state is unavailable")
        adapter.close.assert_called_once_with()

    def test_status_returns_structured_state_read_failure(self):
        adapter = Mock()
        adapter.health.return_value = True
        adapter.discover_capabilities.return_value = {}
        adapter.drain_observations.side_effect = RuntimeError("invalid state payload")

        exit_code, result = self.run_status(adapter)

        self.assertEqual(exit_code, 1)
        self.assertFalse(result["connected"])
        self.assertEqual(result["error"], "invalid state payload")
        adapter.close.assert_called_once_with()

    def test_status_reports_connected_read_only_robot(self):
        adapter = Mock()
        adapter.health.return_value = True
        adapter.discover_capabilities.return_value = {}
        adapter.drain_observations.return_value = ()

        exit_code, result = self.run_status(adapter)

        self.assertEqual(exit_code, 0)
        self.assertTrue(result["connected"])
        self.assertEqual(result["capabilities"], [])
        adapter.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
