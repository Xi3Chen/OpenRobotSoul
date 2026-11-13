from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from robotsoul.settings import (
    LlmConfigStore,
    OperatorDirectory,
    save_audio_settings,
)


class OperatorDirectoryTests(unittest.TestCase):
    def test_username_is_plaintext_and_password_is_salted_pbkdf2(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "operators.json"
            directory = OperatorDirectory(path)
            directory.upsert("操作员", "local-pass-4982")
            raw = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(raw["accounts"][0]["username"], "操作员")
            record = raw["accounts"][0]["password"]
            self.assertEqual(record["kdf"], "pbkdf2-hmac-sha256")
            self.assertEqual(record["iterations"], 200_000)
            self.assertNotIn("local-pass-4982", path.read_text(encoding="utf-8"))
            self.assertFalse(stat.S_IMODE(path.stat().st_mode) & 0o077)
            self.assertTrue(directory.verify("操作员", "local-pass-4982"))
            self.assertFalse(directory.verify("操作员", "local-pass-4983"))
            self.assertFalse(directory.verify("missing", "local-pass-4982"))

    def test_md5_password_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "operators.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "accounts": [
                            {
                                "username": "operator",
                                "password": {"kdf": "md5", "iterations": 1, "salt": "aa", "digest": "bb"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "pbkdf2-hmac-sha256"):
                OperatorDirectory(path)

    def test_last_account_cannot_be_removed(self):
        directory = OperatorDirectory.from_credentials("operator", "local-pass-4982")
        with self.assertRaisesRegex(ValueError, "At least one"):
            directory.delete("operator")


class LlmAndAudioSettingsTests(unittest.TestCase):
    def test_public_llm_view_omits_api_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "llm.json"
            store = LlmConfigStore(path)
            public = store.update(
                {
                    "provider": "openai-compatible",
                    "base_url": "https://example.com/v1",
                    "model": "demo-model",
                    "timeout_seconds": 30,
                    "api_key": "sk-secret",
                }
            )

            self.assertNotIn("sk-secret", json.dumps(public))
            self.assertTrue(public["api_key_configured"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["api_key"], "sk-secret")
            self.assertFalse(stat.S_IMODE(path.stat().st_mode) & 0o077)

    def test_audio_settings_cannot_replace_pinned_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "voice.json"
            path.write_text(
                (Path(__file__).parents[1] / "config" / "voice.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Unsupported audio field"):
                save_audio_settings(path, {"models": {"kws": "other"}})
            self.assertIn("sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
