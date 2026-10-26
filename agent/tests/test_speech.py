"""Offline speech: replies split into sentences, voice levels ignore background noise, Whisper stays the fallback."""
from __future__ import annotations

import array
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_agent import asr, speech  # noqa: E402


def tone(amp: int, secs: float, rate: int = 16000) -> bytes:
    return array.array("h", (int(amp * math.sin(i / 7)) for i in range(int(rate * secs)))).tobytes()


class Sentences(unittest.TestCase):
    def test_split_and_join_short_ones(self) -> None:
        self.assertEqual(speech.sentences("Yes, sir. The system is healthy. Memory is at 60 percent!\nShall I continue?"),
                         ["Yes, sir. The system is healthy.", "Memory is at 60 percent! Shall I continue?"])
        self.assertEqual(speech.sentences(""), [])


class Levels(unittest.TestCase):
    def test_background_noise_is_silence_and_speech_shows(self) -> None:
        ev: list[dict] = []
        lv = speech.Levels(ev.append)
        lv.feed("user", tone(150, 2.0), 16000)  # a quiet room
        quiet = list(ev)
        self.assertLessEqual(len(quiet), 1)  # at most one "went quiet" batch, not a stream of them
        self.assertTrue(all(max(e["v"]) < 0.05 for e in quiet))
        lv.feed("user", tone(3000, 0.5), 16000)  # someone talking
        loud = ev[len(quiet):]
        self.assertTrue(loud and max(loud[0]["v"]) > 0.5)
        self.assertTrue(all(len(e["v"]) == speech.BATCH and e["who"] == "user" for e in loud))


class Recognizer(unittest.TestCase):
    def test_no_model_means_whisper(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.object(asr, "_voice_dir", lambda: Path(d)), \
                mock.patch.object(asr, "_MODEL", None), mock.patch.object(asr, "_TRIED", None):
            self.assertIsNone(asr.recognizer())
            self.assertEqual(asr.status(), {"asr": "Whisper base"})


if __name__ == "__main__":
    unittest.main()
