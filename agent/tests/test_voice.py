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


def best_wake_score(wav: str) -> float:
    """Run a WAV through the wake detector in 80 ms chunks, with a little silence either side."""
    from jarvis_agent.wakeword import CHUNK, WakeWord

    det = WakeWord(voice.voice_dir() / "downloads")
    with wave.open(wav) as w:
        pcm = w.readframes(w.getnframes())
    pcm = bytes(32000) + pcm + bytes(32000)  # a second of silence either side
    best = 0.0
    for i in range(0, len(pcm) - CHUNK * 2, CHUNK * 2):
        best = max(best, det.process(pcm[i:i + CHUNK * 2]))
    return best


def wake_ready() -> bool:
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return all((voice.voice_dir() / "downloads" / f).is_file() for f in ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx"))


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


class TranscriptTests(unittest.TestCase):
    def test_non_speech_and_the_name_are_dropped(self) -> None:
        cases = {"(birds chirping)": "", "[Music]": "", " [BLANK_AUDIO] ": "", "*sigh* What time is it?": "What time is it?",
                 "Open (inaudible) the downloads folder.": "Open the downloads folder", "Hey Jarvis, play some music.": "play some music",
                 "Jarvis.": "", "Jarvis, what's the weather?": "what's the weather?"}
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(voice.clean_transcript(raw), want)


class AudioTests(unittest.TestCase):
    def test_rms_and_wav(self) -> None:
        loud = array.array("h", [1000, -1000] * 400).tobytes()
        self.assertAlmostEqual(voice._rms(loud), 1000.0)
        self.assertEqual(voice._rms(b""), 0.0)
        with wave.open(__import__("io").BytesIO(voice.to_wav(loud))) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()), (16000, 1, 800))


@unittest.skipUnless(sys.platform == "win32" and wake_ready(), "needs Windows speech, numpy, onnxruntime and the wake models")
class WakeWordTests(unittest.TestCase):
    def test_hey_jarvis_wakes_and_other_speech_does_not(self) -> None:
        from jarvis_agent.wakeword import THRESHOLD

        with tempfile.TemporaryDirectory() as d:
            for text, expect in [("Hey Jarvis", True), ("Hey Jarvis, what time is it?", True),
                                 ("What is the weather like in Mumbai today", False), ("Please open the downloads folder for me", False)]:
                with self.subTest(text=text):
                    wav = os.path.join(d, "t.wav")
                    say_to_wav(text, wav)
                    score = best_wake_score(wav)
                    self.assertEqual(score >= THRESHOLD, expect, f"{text}: best score {score:.3f}")


def tone(seconds: float, amp: int) -> bytes:
    n = int(voice.RATE * seconds)
    return array.array("h", (amp if (i // 20) % 2 else -amp for i in range(n))).tobytes()


class WakeOnSpeech(unittest.TestCase):
    def test_gate_cuts_one_utterance_with_preroll(self) -> None:
        gate = voice.SpeechGate()
        stream = tone(1.0, 20) + tone(0.24, 400) + tone(1.0, 2000) + tone(1.0, 20)
        out = []
        for i in range(0, len(stream), 2560):  # 80 ms chunks, like the wake loop
            pcm = gate.feed(stream[i:i + 2560])
            if pcm:
                out.append(pcm)
        self.assertEqual(len(out), 1)
        secs = len(out[0]) / 2 / voice.RATE
        self.assertGreater(secs, 1.6)  # pre-roll + speech + the closing pause
        self.assertLess(secs, 2.7)

    def test_gate_ignores_clicks(self) -> None:
        gate = voice.SpeechGate()
        stream = tone(1.0, 20) + tone(0.08, 3000) + tone(2.0, 20)
        self.assertFalse(any(gate.feed(stream[i:i + 2560]) for i in range(0, len(stream), 2560)))

    def test_only_the_name_wakes_it(self) -> None:
        for text in ("Jarvis", "Jarvis, open Spotify", "What's the time, Jarvis?", "Hey Jervis play music"):
            self.assertTrue(voice.addressed(text), text)
        for text in ("Hello.", "hey there", "Good morning", "what's the weather like", "open YouTube", "um", "Travis is here"):
            self.assertFalse(voice.addressed(text), text)  # someone else's conversation, the TV, a greeting without the name


if __name__ == "__main__":
    unittest.main(verbosity=2)
