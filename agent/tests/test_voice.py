"""Voice: download readiness, the wake word on real (synthesized) speech, and audio helpers."""
from __future__ import annotations

import array
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_agent import voice  # noqa: E402


def say_to_wav(text: str, path: str) -> None:
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
          f"[System.Speech.AudioFormat.AudioChannel]::Mono); $s.SetOutputToWaveFile('{path}', $f); $s.Speak('{text}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True, timeout=60)


def wake_lines(wav: str) -> list[str]:
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", voice._WAKE_PS],
                       env={**os.environ, "JARVIS_WAKE_WAV": wav}, capture_output=True, text=True, timeout=120)
    return [line for line in r.stdout.splitlines() if line.startswith("WAKE")]


class ReadinessTests(unittest.TestCase):
    def test_partial_download_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"JARVIS_DATA_DIR": d}):
            dl = Path(d) / "voice" / "downloads"
            dl.mkdir(parents=True)
            (dl / voice.FILES["whisper_model"]).write_bytes(b"x" * 1000)  # still downloading
            state = voice.prepare()
            self.assertFalse(state["ready"])
            self.assertIn(voice.FILES["whisper_model"], state["missing"])
            self.assertIn(voice.FILES["piper_zip"], state["missing"])


class AudioTests(unittest.TestCase):
    def test_rms_and_wav(self) -> None:
        loud = array.array("h", [1000, -1000] * 400).tobytes()
        self.assertAlmostEqual(voice._rms(loud), 1000.0)
        self.assertEqual(voice._rms(b""), 0.0)
        with wave.open(__import__("io").BytesIO(voice.to_wav(loud))) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()), (16000, 1, 800))


@unittest.skipUnless(sys.platform == "win32", "uses Windows speech")
class WakeWordTests(unittest.TestCase):
    def test_wake_word_fires_only_for_jarvis(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            for text, expect in [("Hey Jarvis", True), ("Jarvis", True), ("What is the weather like in Mumbai today", False),
                                 ("Please open the downloads folder for me", False), ("Travis", False)]:
                with self.subTest(text=text):
                    wav = os.path.join(d, "t.wav")
                    say_to_wav(text, wav)
                    hits = [float(line.split()[1]) for line in wake_lines(wav)]
                    self.assertEqual(any(c >= voice.WAKE_CONFIDENCE for c in hits), expect, f"{text}: {hits}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
