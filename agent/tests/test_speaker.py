"""Only the user's voice: the voiceprint tells voices apart, and the live gate passes only the user's speech."""
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

from jarvis_agent import live_audio, speaker  # noqa: E402

HAVE_MODEL = speaker.model_path().is_file()


def tts(text: str, voice: str) -> bytes:
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SelectVoice('{voice}'); $f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
          "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
          f"$s.SetOutputToWaveFile('{path}', $f); $s.Speak('{text}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
    try:
        with wave.open(path) as w:
            return w.readframes(w.getnframes())
    finally:
        os.remove(path)


@unittest.skipUnless(HAVE_MODEL, "speaker model not downloaded")
class Voiceprint(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patch = mock.patch.object(speaker, "print_path", lambda: Path(cls.tmp.name) / "voiceprint.json")
        cls.patch.start()
        cls.v = speaker.Verifier()
        talk = ("The quick brown fox jumps over the lazy dog. I am setting up my assistant so that it listens to me and nobody else. "
                "Today I might ask it to check the weather, play some music, read my messages or open a file on my computer.")
        cls.enrolled = cls.v.enroll(tts(talk, "Microsoft David Desktop"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.patch.stop()
        cls.tmp.cleanup()

    def test_enrollment(self) -> None:
        self.assertTrue(self.enrolled["enrolled"])
        self.assertGreaterEqual(self.enrolled["pieces"], 3)

    def test_the_user_passes_others_do_not(self) -> None:
        self.assertTrue(self.v.is_user(tts("Jarvis, what is the weather like tomorrow?", "Microsoft David Desktop")))
        self.assertFalse(self.v.is_user(tts("Jarvis, what is the weather like tomorrow?", "Microsoft Zira Desktop")))
        self.assertFalse(self.v.is_user(tts("Jarvis, what is the weather like tomorrow?", "Microsoft Hazel Desktop")))

    def test_too_short_is_not_proof(self) -> None:
        self.assertEqual(self.v.score(b"\0" * 8000), 0.0)


class FakeVerifier:
    def __init__(self, user_amp: int) -> None:
        self.user_amp = user_amp

    def is_user(self, pcm: bytes) -> bool:
        a = array.array("h", pcm)
        return max(abs(x) for x in a) == self.user_amp


def frame(amp: int) -> bytes:
    return array.array("h", (amp if i % 2 else -amp for i in range(live_audio.MIC_FRAME))).tobytes()


class LiveGate(unittest.TestCase):
    def run_burst(self, amp: int) -> bytes:
        g = live_audio.VoiceGate(FakeVerifier(user_amp=3000))
        out = []
        for f in [frame(20)] * 20 + [frame(amp)] * 60 + [frame(20)] * 30:
            out += g.feed(f)
        return b"".join(out)

    def test_the_users_speech_goes_through(self) -> None:
        sent = self.run_burst(3000)
        self.assertEqual(sent.count(frame(3000)), 60)  # all of it, including what was held while judging

    def test_someone_elses_speech_is_silence(self) -> None:
        sent = self.run_burst(2000)
        self.assertEqual(sent.count(frame(2000)), 0)
        self.assertEqual(set(sent), {0})


if __name__ == "__main__":
    unittest.main()
