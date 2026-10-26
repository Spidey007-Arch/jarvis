"""Live audio: nothing the mic hears cuts JARVIS off while it talks; replies are buffered."""
from __future__ import annotations

import array
import sys
import time
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_agent import live_audio  # noqa: E402


def frame(amp: int) -> bytes:
    return array.array("h", (amp if i % 2 else -amp for i in range(live_audio.MIC_FRAME))).tobytes()


class MicGate(unittest.TestCase):
    def setUp(self) -> None:
        self.a = live_audio.LiveAudio(lambda m: None)

    def test_passes_the_mic_while_jarvis_is_quiet(self) -> None:
        self.assertEqual(self.a.gate(frame(900)), frame(900))

    def test_silences_everything_while_jarvis_talks(self) -> None:
        self.a._playing_until = time.monotonic() + 5
        for amp in (150, 900, 5000):  # its own echo, the user, a shout: JARVIS finishes in one go
            self.assertEqual(self.a.gate(frame(amp)), bytes(len(frame(amp))))

    def test_opens_again_after_a_flush(self) -> None:
        self.a._playing_until = time.monotonic() + 5
        self.a.flush()  # "Hey JARVIS" over the reply
        self.assertEqual(self.a.gate(frame(900)), frame(900))


class Priming(unittest.TestCase):
    def test_collects_a_buffer_before_playing(self) -> None:
        a = live_audio.LiveAudio(lambda m: None)
        piece = bytes(int(live_audio.BYTES_PER_S * 0.1))  # 100 ms chunks, as Gemini sends them
        for _ in range(5):
            a._play.put(piece)
        out = a._prime(piece)
        self.assertGreaterEqual(len(out), live_audio.PRIME_S * live_audio.BYTES_PER_S)

    def test_short_reply_is_not_held_forever(self) -> None:
        a = live_audio.LiveAudio(lambda m: None)
        t = time.monotonic()
        out = a._prime(bytes(4800))
        self.assertEqual(len(out), 4800)
        self.assertLess(time.monotonic() - t, live_audio.PRIME_WAIT_S + 0.2)


if __name__ == "__main__":
    unittest.main()
