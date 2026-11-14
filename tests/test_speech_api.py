from __future__ import annotations

import json
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from robotsoul.voice.speech_api import SpeechApiError, SpeechClient, SpeechConfig, SpeechEndpoint


def _config(base_url: str) -> SpeechConfig:
    return SpeechConfig(
        asr=SpeechEndpoint(base_url, "asr-model", 5, api_key_env="ROBOTSOUL_ASR_API_KEY"),
        tts=SpeechEndpoint(base_url, "tts-model", 5, voice="alloy", api_key_env="ROBOTSOUL_TTS_API_KEY"),
    )


class _Handler(BaseHTTPRequestHandler):
    seen: dict = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _Handler.seen = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
        }
        if self.path.endswith("/audio/transcriptions"):
            payload = json.dumps({"text": "趴下去"}).encode("utf-8")
        elif self.path.endswith("/audio/speech"):
            from robotsoul.voice.speech_api import _wav_bytes
            payload = _wav_bytes((0.25, -0.25), 16000)
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


class SpeechApiTests(unittest.TestCase):
    def test_openai_compatible_routes_serve_cloud_or_local_hosts(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
            client = SpeechClient(_config(base_url))
            with mock.patch.dict("os.environ", {"ROBOTSOUL_ASR_API_KEY": "secret-asr"}, clear=False):
                text = client.transcribe((0.1, -0.1), 16000, language="zh-CN")
            self.assertEqual(text, "趴下去")
            self.assertEqual(_Handler.seen["path"], "/v1/audio/transcriptions")
            self.assertEqual(_Handler.seen["authorization"], "Bearer secret-asr")
            self.assertIn(b"name=\"model\"", _Handler.seen["body"])
            self.assertIn(b"asr-model", _Handler.seen["body"])
            self.assertIn(b"audio/wav", _Handler.seen["body"])
            samples, rate = client.synthesize("你好")
            self.assertEqual(rate, 16000)
            self.assertEqual(len(samples), 2)
            self.assertEqual(_Handler.seen["path"], "/v1/audio/speech")
            self.assertIn(b"tts-model", _Handler.seen["body"])
        finally:
            server.shutdown()
            server.server_close()

    def test_missing_endpoint_is_rejected_before_the_network(self):
        client = SpeechClient(_config(""))
        with self.assertRaisesRegex(SpeechApiError, "ASR API"):
            client.transcribe((0.1,), 16000)


if __name__ == "__main__":
    unittest.main()
