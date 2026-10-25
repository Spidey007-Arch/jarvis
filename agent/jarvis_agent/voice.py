"""Voice: wake word, listening, transcription and speech, all on this PC.

- Wake word: "Hey JARVIS", detected by openWakeWord's neural models on onnxruntime (see wakeword.py).
  Windows' built-in recognizer was tried first and could not pick the name out of quiet laptop audio.
- Listening: the default microphone at 16 kHz until about a second of silence.
- Transcription: whisper.cpp with the base English model.
- Speech: Piper with a British male voice; saying the wake word while JARVIS talks stops it.

The programs and models live in <data>/voice, unpacked from <data>/voice/downloads on first use."""
from __future__ import annotations

import array
import io
import logging
import math
import os
import subprocess
import tempfile
import threading
import time
import wave
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import win

log = logging.getLogger("jarvis.voice")

RATE = 16000
MAX_UTTERANCE_S = 15.0
NO_SPEECH_S = 5.0  # give up if nothing is said this long after the wake word
END_SILENCE_S = 1.0

FILES = {
    "whisper_zip": "whisper-bin-x64.zip",
    "whisper_model": "ggml-base.en-q5_1.bin",
    "piper_zip": "piper_windows_amd64.zip",
    "voice_model": "en_GB-alan-medium.onnx",
    "voice_config": "en_GB-alan-medium.onnx.json",
    "wake_mel": "melspectrogram.onnx",
    "wake_embedding": "embedding_model.onnx",
    "wake_model": "hey_jarvis_v0.1.onnx",
}
# A download still in progress already exists as a file; only a complete one counts.
MIN_BYTES = {"whisper_zip": 8_000_000, "whisper_model": 59_000_000, "piper_zip": 22_000_000, "voice_model": 63_000_000, "voice_config": 4_000,
             "wake_mel": 1_000_000, "wake_embedding": 1_000_000, "wake_model": 1_000_000}

def voice_dir() -> Path:
    base = Path(os.environ.get("JARVIS_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JARVIS")
    return base / "voice"


def _unzip(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(src) as z:
        for m in z.infolist():
            target = (dest / m.filename).resolve()
            if not str(target).startswith(str(root)):
                raise ValueError(f"unsafe path in {src.name}: {m.filename}")
        z.extractall(dest)


def _find(root: Path, *names: str) -> Path | None:
    for name in names:
        hit = next(root.rglob(name), None) if root.exists() else None
        if hit:
            return hit
    return None


def prepare() -> dict[str, Any]:
    """Unpack what has been downloaded; report what is still missing."""
    vd = voice_dir()
    dl = vd / "downloads"
    missing = [f for k, f in FILES.items() if not (dl / f).is_file() or (dl / f).stat().st_size < MIN_BYTES[k]]
    whisper_dir, piper_dir = vd / "whisper", vd / "piper"
    try:
        if not _find(whisper_dir, "whisper-cli.exe", "main.exe") and FILES["whisper_zip"] not in missing:
            _unzip(dl / FILES["whisper_zip"], whisper_dir)
        if not _find(piper_dir, "piper.exe") and FILES["piper_zip"] not in missing:
            _unzip(dl / FILES["piper_zip"], piper_dir)
    except (zipfile.BadZipFile, ValueError) as e:
        return {"ready": False, "missing": missing, "error": f"A download is damaged or incomplete: {e}"}
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        missing.append("numpy + onnxruntime (pip install numpy onnxruntime)")
    whisper = _find(whisper_dir, "whisper-cli.exe", "main.exe")
    piper = _find(piper_dir, "piper.exe")
    return {
        "ready": not missing and bool(whisper and piper),
        "missing": missing,
        "whisper": str(whisper) if whisper else None,
        "piper": str(piper) if piper else None,
        "whisper_model": str(dl / FILES["whisper_model"]),
        "voice_model": str(dl / FILES["voice_model"]),
    }


def _rms(chunk: bytes) -> float:
    a = array.array("h", chunk)
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


def record_utterance(stop: threading.Event) -> bytes | None:
    """Record from the default microphone until the speaker pauses. None if nothing was said."""
    import sounddevice as sd

    frames: list[bytes] = []
    block = int(RATE * 0.05)
    noise: list[float] = []
    spoken = False
    quiet_for = 0.0
    started = time.monotonic()
    with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=block) as stream:
        while not stop.is_set():
            data, _ = stream.read(block)
            data = bytes(data)
            level = _rms(data)
            elapsed = time.monotonic() - started
            if elapsed < 0.3:
                noise.append(level)  # the room's background level, to judge what counts as speech
                continue
            threshold = max(160.0, (sum(noise) / max(1, len(noise))) * 3.5)  # laptop mic arrays can be quiet
            frames.append(data)
            if level > threshold:
                spoken, quiet_for = True, 0.0
            else:
                quiet_for += 0.05
            if spoken and quiet_for >= END_SILENCE_S:
                break
            if not spoken and elapsed >= NO_SPEECH_S:
                return None
            if elapsed >= MAX_UTTERANCE_S:
                break
    return b"".join(frames) if spoken else None


def to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def transcribe(wav: bytes, setup: dict[str, Any]) -> str:
    fd, path = tempfile.mkstemp(prefix="jarvis-voice-", suffix=".wav")
    os.close(fd)
    try:
        Path(path).write_bytes(wav)
        threads = str(max(2, (os.cpu_count() or 4) // 2))
        r = subprocess.run([setup["whisper"], "-m", setup["whisper_model"], "-f", path, "-l", "en", "-nt", "-np", "-t", threads],
                           capture_output=True, text=True, timeout=90, creationflags=win.CREATE_NO_WINDOW | win.ABOVE_NORMAL_PRIORITY_CLASS)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else f"whisper exited {r.returncode}")
        text = " ".join(line.strip() for line in r.stdout.splitlines() if line.strip())
        return "" if text.strip("[]() ").lower() in {"blank_audio", "silence", "music", ""} else text.strip()
    finally:
        Path(path).unlink(missing_ok=True)


def synthesize(text: str, setup: dict[str, Any]) -> Path:
    fd, path = tempfile.mkstemp(prefix="jarvis-speech-", suffix=".wav")
    os.close(fd)
    r = subprocess.run([setup["piper"], "--model", setup["voice_model"], "--output_file", path],
                       input=text, capture_output=True, text=True, timeout=120, encoding="utf-8",
                       cwd=str(Path(setup["piper"]).parent), creationflags=win.CREATE_NO_WINDOW | win.ABOVE_NORMAL_PRIORITY_CLASS)
    if r.returncode != 0 or not Path(path).stat().st_size:
        Path(path).unlink(missing_ok=True)
        raise RuntimeError((r.stderr or "piper failed").strip().splitlines()[-1])
    return Path(path)


class Voice:
    """Owns the wake listener, listening/transcribing and speech. `emit` sends events to the backend."""

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self.emit = emit
        self.enabled = False
        self._wake: threading.Thread | None = None
        self._wake_stop = threading.Event()
        self._busy = threading.Lock()
        self._stop_listening = threading.Event()
        self._speech: Path | None = None
        self._speech_token = 0

    # ----- wake word -----

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        setup = prepare()
        if enabled and not setup["ready"]:
            self.enabled = False
            return {"enabled": False, **setup}
        self.enabled = enabled
        if enabled and (self._wake is None or not self._wake.is_alive()):
            self._wake_stop = threading.Event()
            self._wake = threading.Thread(target=self._wake_loop, args=(self._wake_stop,), daemon=True)
            self._wake.start()
        if not enabled and self._wake is not None:
            self._wake_stop.set()  # the loop closes the microphone within 80 ms
            self._wake = None
        return {"enabled": self.enabled, **setup}

    def _wake_loop(self, stop: threading.Event) -> None:
        import sounddevice as sd

        from .wakeword import CHUNK, WakeWord

        try:
            detector = WakeWord(voice_dir() / "downloads")
        except Exception as e:  # noqa: BLE001
            log.exception("wake word models could not load")
            self.emit({"event": "error", "message": f"Wake word could not start: {e}"})
            return
        log.info("wake word listening (openWakeWord: hey jarvis)")
        while not stop.is_set():
            try:
                with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK) as mic:
                    while not stop.is_set():
                        data, _ = mic.read(CHUNK)
                        if self._busy.locked():  # already taking a request: do not trigger on it
                            detector.reset()
                            continue
                        score = detector.heard(bytes(data))
                        if score is not None:
                            log.info("wake word heard (%.2f)", score)
                            self.on_wake(score)
            except Exception as e:  # noqa: BLE001 - device unplugged or busy: try again shortly
                log.warning("microphone unavailable (%s); retrying in 3s", e)
                stop.wait(3)

    def on_wake(self, confidence: float = 1.0) -> None:
        self.stop_speaking()
        if not self._busy.acquire(blocking=False):
            return  # already listening
        threading.Thread(target=self._listen, args=(confidence,), daemon=True).start()

    def _listen(self, confidence: float) -> None:
        try:
            setup = prepare()
            win_beep()
            self.emit({"event": "listening", "confidence": confidence})
            self._stop_listening.clear()
            pcm = record_utterance(self._stop_listening)
            if not pcm:
                self.emit({"event": "idle", "reason": "no speech heard"})
                return
            self.emit({"event": "transcribing"})
            text = transcribe(to_wav(pcm), setup)
            if text:
                self.emit({"event": "transcript", "text": text})
            else:
                self.emit({"event": "idle", "reason": "could not make out any words"})
        except Exception as e:  # noqa: BLE001 - report, never kill the agent
            log.exception("voice capture failed")
            self.emit({"event": "error", "message": f"Voice input failed: {e}"})
        finally:
            self._busy.release()

    # ----- speech -----

    def speak(self, text: str) -> dict[str, Any]:
        setup = prepare()
        if not setup["ready"]:
            return {"spoken": False, **setup}
        self.stop_speaking()
        self._speech_token += 1
        token = self._speech_token
        path = synthesize(text, setup)
        with wave.open(str(path), "rb") as w:
            seconds = w.getnframes() / float(w.getframerate())
        import winsound

        self._speech = path
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        self.emit({"event": "speaking", "seconds": round(seconds, 1)})

        def finished() -> None:
            time.sleep(seconds + 0.3)
            if self._speech_token == token:
                self._speech = None
                path.unlink(missing_ok=True)
                self.emit({"event": "idle", "reason": "spoken"})

        threading.Thread(target=finished, daemon=True).start()
        return {"spoken": True, "seconds": round(seconds, 1)}

    def stop_speaking(self) -> None:
        if self._speech is None:
            return
        import winsound

        winsound.PlaySound(None, 0)
        self._speech_token += 1
        path, self._speech = self._speech, None
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        self.emit({"event": "idle", "reason": "interrupted"})


_VOICE: Voice | None = None


def attach(emit: Callable[[dict[str, Any]], None]) -> None:
    global _VOICE
    _VOICE = Voice(emit)


def instance() -> Voice:
    if _VOICE is None:
        raise RuntimeError("voice is not initialised")
    return _VOICE


def win_beep() -> None:
    """A soft two-tone cue that JARVIS is listening."""
    try:
        import winsound

        winsound.Beep(880, 70)
        winsound.Beep(1320, 90)
    except RuntimeError:
        pass
