import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from robotsoul.picoclaw import PROJECT_ROOT, PicoClawCodingAgent, PicoClawError


class PicoClawCodingAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "worktree"
        self.workspace.mkdir()
        self.binary = self.root / ("picoclaw.exe" if os.name == "nt" else "picoclaw")
        self.binary.touch()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_runs_single_task_with_scoped_session_and_workspace(self):
        process = Mock()
        process.communicate.return_value = ("done\n", "")
        process.returncode = 0

        def start_process(_command, **options):
            config_path = Path(options["env"]["PICOCLAW_CONFIG"])
            self.assertTrue(config_path.is_file())
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["version"], 3)
            self.assertTrue((config_path.parent / ".security.yml").is_file())
            return process

        with patch.dict(
            os.environ,
            {
                "PICOCLAW_TOOLS_EXEC_ENABLED": "true",
                "OPENAI_API_KEY": "host-secret",
                "HTTPS_PROXY": "http://user:secret@proxy.invalid",
            },
        ):
            with patch("robotsoul.picoclaw.subprocess.Popen", side_effect=start_process) as popen:
                self._assert_task_uses_scoped_process(popen)

    def _assert_task_uses_scoped_process(self, popen):
        config_path = self.root / "config.json"
        config_path = self._write_config()
        (self.root / ".security.yml").write_text("model_list: []\n", encoding="utf-8")
        agent = PicoClawCodingAgent(binary=self.binary, model="test-model")

        with patch.dict(os.environ, {"PICOCLAW_CONFIG": str(config_path)}):
            result = agent.run_task("edit this file", self.workspace)

        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        child_env = options["env"]
        self.assertEqual(command[1:3], ["agent", "--message=edit this file"])
        self.assertEqual(command[4], "--model=test-model")
        self.assertEqual(options["cwd"], self.workspace.resolve())
        self.assertTrue(command[3].startswith("--session=openrobotsoul-"))
        self.assertEqual(child_env["PICOCLAW_AGENTS_DEFAULTS_WORKSPACE"], str(self.workspace.resolve()))
        self.assertEqual(child_env["PICOCLAW_AGENTS_DEFAULTS_RESTRICT_TO_WORKSPACE"], "true")
        self.assertEqual(child_env["PICOCLAW_TOOLS_EXEC_ENABLED"], "false")
        self.assertEqual(child_env["PICOCLAW_MEDIA_CLEANUP_ENABLED"], "false")
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("HTTPS_PROXY", child_env)
        self.assertNotEqual(child_env["PICOCLAW_CONFIG"], str(config_path))
        self.assertFalse(Path(child_env["PICOCLAW_CONFIG"]).exists())
        self.assertTrue(result.succeeded)
        self.assertEqual(result.output, "done\n")

    @patch("robotsoul.picoclaw.subprocess.Popen")
    def test_shell_access_requires_explicit_opt_in(self, popen):
        process = Mock()
        process.communicate.return_value = ("", "")
        process.returncode = 0
        popen.return_value = process
        agent = PicoClawCodingAgent(binary=self.binary, allow_shell=True)
        config_path = self._write_config()

        with patch.dict(os.environ, {"PICOCLAW_CONFIG": str(config_path)}):
            agent.run_task("run tests", self.workspace)

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["PICOCLAW_TOOLS_EXEC_ENABLED"], "true")
        self.assertEqual(env["PICOCLAW_TOOLS_EXEC_ENABLE_DENY_PATTERNS"], "true")
        self.assertEqual(env["PICOCLAW_TOOLS_EXEC_ALLOW_REMOTE"], "false")

    @patch("robotsoul.picoclaw.subprocess.Popen")
    def test_timeout_is_returned_as_a_failed_result(self, popen):
        process = Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired([str(self.binary)], 5), ("partial", "slow")]
        process.returncode = -9
        popen.return_value = process
        agent = PicoClawCodingAgent(binary=self.binary, timeout_seconds=5)
        config_path = self._write_config()

        with patch.dict(os.environ, {"PICOCLAW_CONFIG": str(config_path)}):
            result = agent.run_task("inspect", self.workspace)

        self.assertTrue(result.timed_out)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.returncode, -9)
        self.assertEqual(result.output, "partial")
        self.assertEqual(result.stderr, "slow")
        process.kill.assert_called_once()

    @patch("robotsoul.picoclaw.subprocess.Popen")
    def test_keyboard_interrupt_cancels_the_child_process(self, popen):
        process = Mock()
        process.communicate.side_effect = [KeyboardInterrupt(), ("partial", "")]
        process.returncode = -9
        popen.return_value = process
        agent = PicoClawCodingAgent(binary=self.binary)
        config_path = self._write_config()

        with patch.dict(os.environ, {"PICOCLAW_CONFIG": str(config_path)}):
            result = agent.run_task("inspect", self.workspace)

        self.assertTrue(result.cancelled)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.output, "partial")
        process.kill.assert_called_once()

    def test_rejects_empty_prompts_and_missing_workspaces(self):
        agent = PicoClawCodingAgent(binary=self.binary)

        with self.assertRaisesRegex(ValueError, "prompt"):
            agent.run_task("  ", self.workspace)
        with self.assertRaisesRegex(ValueError, "workspace"):
            agent.run_task("inspect", self.root / "missing")
        with self.assertRaisesRegex(ValueError, "outside"):
            agent.run_task("inspect", PROJECT_ROOT)

    def test_missing_executable_has_actionable_error(self):
        agent = PicoClawCodingAgent(binary=self.root / "missing.exe")

        with self.assertRaisesRegex(PicoClawError, "not found"):
            agent.run_task("inspect", self.workspace)

    def test_resolves_binary_from_unified_build_directory(self):
        executable_name = "picoclaw.exe" if os.name == "nt" else "picoclaw"
        target = _host_target()
        build_binary = PROJECT_ROOT / "build" / target / "bin" / executable_name
        build_binary.parent.mkdir(parents=True, exist_ok=True)
        build_binary.write_bytes(b"test binary")
        self.addCleanup(build_binary.unlink, missing_ok=True)

        agent = PicoClawCodingAgent()

        self.assertEqual(agent._resolve_binary(), build_binary)

    @patch("robotsoul.picoclaw.subprocess.Popen")
    def test_missing_config_fails_before_starting_picoclaw(self, popen):
        agent = PicoClawCodingAgent(binary=self.binary)
        missing_home = self.root / "unconfigured-home"

        with patch.dict(
            os.environ,
            {"PICOCLAW_HOME": str(missing_home), "PICOCLAW_CONFIG": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(PicoClawError, "not configured"):
                agent.run_task("inspect", self.workspace)

        popen.assert_not_called()

    def test_rejects_non_positive_timeout(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            PicoClawCodingAgent(binary=self.binary, timeout_seconds=0)

    @patch("robotsoul.picoclaw.subprocess.Popen")
    def test_refuses_configs_with_hooks_or_evolution_enabled(self, popen):
        config_path = self.root / "picoclaw.json"
        agent = PicoClawCodingAgent(binary=self.binary)

        configurations = (
            (
                '{"version":3,"model_list":[],"hooks":{"enabled":true},'
                '"evolution":{"enabled":false}}',
                "hooks.enabled to false",
            ),
            (
                '{"version":3,"model_list":[],"hooks":{"enabled":false},'
                '"evolution":{"enabled":true}}',
                "evolution.enabled to false",
            ),
        )
        for contents, expected_error in configurations:
            config_path.write_text(contents, encoding="utf-8")
            with patch.dict(os.environ, {"PICOCLAW_CONFIG": str(config_path)}):
                with self.assertRaisesRegex(PicoClawError, expected_error):
                    agent.run_task("inspect", self.workspace)
        popen.assert_not_called()

    @patch("robotsoul.picoclaw.subprocess.Popen")
    def test_requires_hooks_and_evolution_settings_to_be_explicit(self, popen):
        config_path = self.root / "incomplete-config.json"
        config_path.write_text(
            '{"version":3,"model_list":[],"hooks":{},"evolution":{}}',
            encoding="utf-8",
        )
        agent = PicoClawCodingAgent(binary=self.binary)

        with patch.dict(os.environ, {"PICOCLAW_CONFIG": str(config_path)}):
            with self.assertRaisesRegex(PicoClawError, "hooks.enabled to false"):
                agent.run_task("inspect", self.workspace)

        popen.assert_not_called()

    @patch("robotsoul.picoclaw.subprocess.Popen")
    def test_requires_a_configured_model_before_starting(self, popen):
        config_path = self.root / "empty-model-config.json"
        config_path.write_text(
            '{"version":3,"hooks":{"enabled":false},"evolution":{"enabled":false},'
            '"model_list":[]}',
            encoding="utf-8",
        )
        agent = PicoClawCodingAgent(binary=self.binary)

        with patch.dict(os.environ, {"PICOCLAW_CONFIG": str(config_path)}):
            with self.assertRaisesRegex(PicoClawError, "at least one model"):
                agent.run_task("inspect", self.workspace)

        popen.assert_not_called()

    @patch("robotsoul.picoclaw.subprocess.run")
    def test_setup_check_reports_missing_config_without_starting_agent(self, run):
        run.return_value = Mock(returncode=0, stdout="PicoClaw v0.1\n")
        agent = PicoClawCodingAgent(binary=self.binary)

        with patch.dict(
            os.environ,
            {"PICOCLAW_HOME": str(self.root / "unconfigured-home"), "PICOCLAW_CONFIG": ""},
            clear=False,
        ):
            report = agent.check_setup()

        self.assertFalse(report["ready_to_launch"])
        self.assertFalse(report["config_found"])
        self.assertFalse(report["credentials_validated"])
        self.assertTrue(any("run picoclaw onboard" in issue for issue in report["issues"]))
        run.assert_called_once()

    @patch("robotsoul.picoclaw.subprocess.run")
    def test_setup_check_reports_safe_configuration_without_secrets(self, run):
        run.return_value = Mock(returncode=0, stdout="PicoClaw v0.1\n")
        config_path = self._write_config()
        config_path.write_text(
            '{"version":3,"hooks":{"enabled":false},"evolution":{"enabled":false},'
            '"model_list":[{"model_name":"test","api_keys":["secret-value"]}]}',
            encoding="utf-8",
        )
        agent = PicoClawCodingAgent(binary=self.binary)

        with patch.dict(os.environ, {"PICOCLAW_CONFIG": str(config_path)}):
            report = agent.check_setup()

        self.assertTrue(report["ready_to_launch"])
        self.assertTrue(report["hooks_disabled"])
        self.assertTrue(report["evolution_disabled"])
        self.assertFalse(report["credentials_validated"])
        self.assertNotIn("secret-value", repr(report))

    def _write_config(self):
        config_path = self.root / "safe-config.json"
        config_path.write_text(
            '{"version":3,"model_list":[{"model_name":"test"}],'
            '"hooks":{"enabled":false},"evolution":{"enabled":false}}',
            encoding="utf-8",
        )
        return config_path


if __name__ == "__main__":
    unittest.main()


def _host_target():
    from robotsoul.build import detect_host_architecture, detect_host_platform

    return f"{detect_host_platform()}-{detect_host_architecture()}"
