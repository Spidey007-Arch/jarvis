"""Live audio: JARVIS's own voice and room noise must not count as the user interrupting; replies are buffered."""
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

    def test_passes_everything_while_jarvis_is_quiet(self) -> None:
        self.assertEqual(self.a.gate(frame(50)), frame(50))

    def test_silences_echo_and_noise_while_jarvis_talks(self) -> None:
        self.a._playing_until = time.monotonic() + 5
        for _ in range(30):
            self.assertEqual(self.a.gate(frame(150)), bytes(len(frame(150))))  # its own voice, faintly
        self.assertEqual(self.a.gate(frame(900)), frame(900))  # the user talking over it still gets through

    def test_loud_echo_raises_the_bar(self) -> None:
        self.a._playing_until = time.monotonic() + 5
        for _ in range(60):
            self.a.gate(frame(250))  # loud speakers right next to the mic
        self.assertEqual(self.a.gate(frame(500)), bytes(len(frame(500))))
        self.assertEqual(self.a.gate(frame(1200)), frame(1200))


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
