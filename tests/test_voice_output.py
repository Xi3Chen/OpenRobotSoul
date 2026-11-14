from __future__ import annotations

import unittest


class CallTrackTests(unittest.TestCase):
    def test_new_utterance_drops_frames_already_queued_for_the_browser(self):
        try:
            import aiortc  # noqa: F401
            import av  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("aiortc, av and numpy are required")
        from robotsoul.voice.webrtc import AiortcCallMedia

        media = AiortcCallMedia(object())
        try:
            if not media.available:
                self.skipTest("WebRTC media loop is unavailable")

            async def play_two():
                track = media._track_type()
                await track.feed(tuple(0.2 for _ in range(1600)), 16000)
                first = track._queue.qsize()
                await track.feed((0.1,), 16000)
                second = track._queue.qsize()
                track.clear()
                return first, second, track._queue.qsize()

            first, second, emptied = media._call(play_two())
            self.assertGreater(first, 1)
            self.assertEqual(second, 1)
            self.assertLess(second, first)
            self.assertEqual(emptied, 0)
        finally:
            media.shutdown()


if __name__ == "__main__":
    unittest.main()
