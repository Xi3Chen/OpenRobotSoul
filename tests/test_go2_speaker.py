from __future__ import annotations

import threading
import time
import unittest

import robotsoul.voice.outputs  # noqa: F401  # initialize robotsoul before the Go2 package
from adapters.unitree_go2.speaker import Go2PcmOutput, Go2PcmQueue, _attach_pcm_track
from robotsoul.voice.audio import InterruptibleOutput


class Go2PcmQueueTests(unittest.TestCase):
    def test_replacement_and_stop_drop_the_previous_utterance(self):
        queue = Go2PcmQueue()
        first = queue.submit((0.1,) * 1000, 16000)
        self.assertTrue(queue.pending(first))
        self.assertTrue(all(chunk[0] == 0.1 for chunk in queue.chunk_samples()))

        second = queue.submit((0.3,) * 1000, 16000)
        self.assertFalse(queue.pending(first))
        self.assertTrue(queue.pending(second))
        self.assertTrue(all(chunk[0] == 0.3 for chunk in queue.chunk_samples()))

        Go2PcmOutput(queue).stop()
        self.assertFalse(queue.pending(second))
        self.assertIsNone(queue.pop())
        self.assertEqual(queue.chunk_samples(), ())

    def test_interruptible_playback_cancels_the_blocked_utterance(self):
        class Backend:
            def __init__(self):
                self.started = threading.Event()

            def play(self, samples, _rate, cancel=None):
                if samples[0] == 0.1:
                    self.started.set()
                    while cancel is not None and not cancel.is_set():
                        time.sleep(0.005)

            def stop(self):
                return

        output = InterruptibleOutput(Backend())
        finished = []
        thread = threading.Thread(target=lambda: finished.append(output.play((0.1, 0.1), 16000)))
        thread.start()
        try:
            self.assertTrue(output._backend.started.wait(2))
            self.assertFalse(output.play((0.3, 0.3), 16000))
            thread.join(2)
            self.assertEqual(finished, [True])
            self.assertFalse(thread.is_alive())
        finally:
            output.stop()
            thread.join(1)

    def test_pcm_track_attaches_to_a_fake_audio_sender(self):
        class Sender:
            def __init__(self):
                self.track = None

            def replaceTrack(self, track):
                self.track = track

        class Transceiver:
            def __init__(self, kind):
                self.kind = kind
                self.sender = Sender()

        class Peer:
            def __init__(self):
                self.transceivers = [Transceiver("video"), Transceiver("audio")]

            def getTransceivers(self):
                return self.transceivers

        peer = Peer()
        track = object()
        _attach_pcm_track(peer, track)
        self.assertIs(peer.transceivers[1].sender.track, track)
        self.assertIsNone(peer.transceivers[0].sender.track)

        with self.assertRaisesRegex(RuntimeError, "no audio transceiver"):
            _attach_pcm_track(type("Empty", (), {"getTransceivers": lambda self: []})(), track)


if __name__ == "__main__":
    unittest.main()
