"""Voice: wake word, listening, transcription and speech, all on this PC.

- Waking: "Hey JARVIS" is caught instantly by openWakeWord's neural models (see wakeword.py). Besides
  that, any speech is cut out of the microphone stream (SpeechGate) and transcribed here; JARVIS wakes
  when it hears its name or a greeting ("Jarvis", "hello", "you there?"), or, while the PC itself is
  silent, anything said at all. While a video or music is playing the name or a greeting is needed,
  so JARVIS does not answer the TV.
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
import re
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
GATE_ONSET_S = 0.24  # this much continuous sound starts an utterance
GATE_PREROLL_S = 0.5
GATE_END_S = 0.7
GATE_MAX_S = 8.0
GATE_MIN_LEVEL = 110.0  # laptop mic arrays can be very quiet
MEDIA_PEAK = 0.02  # output louder than this means the PC is playing something

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
    loud_for = 0.0  # speech must last a moment: a door or a bird is not a request
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
            threshold = max(110.0, (sum(noise) / max(1, len(noise))) * 2.5)  # laptop mic arrays can be very quiet
            frames.append(data)
            if level > threshold:
                loud_for += 0.05
                quiet_for = 0.0
                if loud_for >= 0.3:
                    spoken = True
            else:
                quiet_for += 0.05
            if spoken and quiet_for >= END_SILENCE_S:
                break
            if not spoken and elapsed >= NO_SPEECH_S:
                return None
            if elapsed >= MAX_UTTERANCE_S:
                break
    return b"".join(frames) if spoken else None


class SpeechGate:
    """Cuts utterances out of a continuous 16 kHz stream fed in chunks. Tracks the room's noise floor,
    starts on GATE_ONSET_S of sound above it (keeping GATE_PREROLL_S before, so the first word is whole)
    and ends after GATE_END_S of quiet or GATE_MAX_S. feed() returns the utterance when one ends."""

    def __init__(self) -> None:
        self.floor = 60.0
        self.reset()

    def reset(self) -> None:
        self.pre: list[bytes] = []
        self.frames: list[bytes] = []
        self.loud = self.quiet = 0.0
        self.active = False

    def threshold(self) -> float:
        return max(GATE_MIN_LEVEL, self.floor * 2.5)

    def feed(self, chunk: bytes, level: float | None = None) -> bytes | None:
        level = _rms(chunk) if level is None else level
        secs = len(chunk) / 2 / RATE
        loud = level > self.threshold()
        if not self.active:
            if loud:
                self.loud += secs
            else:
                self.floor += (level - self.floor) * 0.05  # follow the room, slowly
                self.loud = 0.0
            self.pre.append(chunk)
            while len(self.pre) * secs > GATE_PREROLL_S + GATE_ONSET_S:
                self.pre.pop(0)
            if self.loud >= GATE_ONSET_S:
                self.active, self.frames, self.pre, self.quiet = True, self.pre, [], 0.0
            return None
        self.frames.append(chunk)
        self.quiet = 0.0 if loud else self.quiet + secs
        if self.quiet >= GATE_END_S or len(self.frames) * secs >= GATE_MAX_S:
            pcm = b"".join(self.frames)
            self.reset()
            return pcm
        return None


_NAME = re.compile(r"\b(jarvis|jarvys|jervis|jarvas|jarves|javis)\b", re.I)
_GREETING = re.compile(
    r"^(hello|hi|hey|hiya|yo|ok|okay|good (morning|afternoon|evening)|you there|are you there|wake up|listen)\b", re.I)


def addressed(text: str, media_playing: bool) -> bool:
    """Was this speech meant for JARVIS? Its name or a greeting always counts; with nothing playing on
    the PC, any real sentence does too."""
    t = text.strip()
    if _NAME.search(t) or _GREETING.search(t):
        return True
    return not media_playing and len(re.findall(r"[A-Za-z']{2,}", t)) >= 2


def normalise(pcm: bytes, target_peak: int = 20000) -> bytes:
    """Bring quiet speech up to a healthy level (at most 30x) so Whisper hears it clearly."""
    a = array.array("h", pcm)
    peak = max((abs(x) for x in a), default=0)
    if not peak or peak >= target_peak:
        return pcm
    gain = min(30.0, target_peak / peak)
    return array.array("h", (max(-32768, min(32767, int(x * gain))) for x in a)).tobytes()


def to_wav(pcm: bytes) -> bytes:
    pcm = normalise(pcm)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def clean_transcript(text: str, strip_name: bool = True) -> str:
    """Drop Whisper's descriptions of non-speech, e.g. "(birds chirping)", "[Music]", "*sigh*"."""
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]|\*[^*]*\*", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,-")
    if strip_name:
        text = re.sub(r"^(hey|ok|okay)?\s*jarvis\b[\s,.!?-]*", "", text, flags=re.I)  # the name is not part of the request
    return text if re.search(r"[A-Za-z]{2,}", text) else ""


def transcribe(wav: bytes, setup: dict[str, Any], strip_name: bool = True) -> str:
    fd, path = tempfile.mkstemp(prefix="jarvis-voice-", suffix=".wav")
    os.close(fd)
    try:
        Path(path).write_bytes(wav)
        threads = str(max(2, (os.cpu_count() or 4) // 2))
        # Whisper always encodes a 30 s window; a short clip only needs the part it fills (3x faster).
        secs = max(0.0, (len(wav) - 44) / 2 / RATE)
        ctx = str(min(1500, max(256, int(secs / 30 * 1500) + 128)))
        r = subprocess.run([setup["whisper"], "-m", setup["whisper_model"], "-f", path, "-l", "en", "-nt", "-np", "-t", threads, "-ac", ctx],
                           capture_output=True, text=True, timeout=90, creationflags=win.CREATE_NO_WINDOW | win.ABOVE_NORMAL_PRIORITY_CLASS)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else f"whisper exited {r.returncode}")
        return clean_transcript(" ".join(line.strip() for line in r.stdout.splitlines() if line.strip()), strip_name)
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
        self.mode = "live"  # live: the backend runs a Gemini Live session; local: Whisper + Piper here
        self.live_busy: Callable[[], bool] = lambda: False
        self.live_talking: Callable[[], bool] = lambda: False  # JARVIS's live reply is coming out of the speakers
        self.on_barge: Callable[[], None] = lambda: None  # stop that reply now
        self._wake: threading.Thread | None = None
        self._wake_stop = threading.Event()
        self._busy = threading.Lock()
        self._stop_listening = threading.Event()
        self._speech: Path | None = None
        self._speech_token = 0
        self.any_speech = True  # wake on any speech meant for JARVIS, not only "Hey JARVIS"
        self._hearing = threading.Lock()  # one transcription at a time
        self._quiet_until = 0.0

    # ----- wake word -----

    def set_enabled(self, enabled: bool, mode: str | None = None) -> dict[str, Any]:
        if mode in ("live", "local"):
            self.mode = mode
        setup = prepare()
        if enabled and self.mode == "live":
            # live mode only needs the wake word models here; Gemini hears and speaks
            wake_missing = [m for m in setup["missing"] if m.endswith(".onnx") and "alan" not in m or "numpy" in m]
            setup = {**setup, "ready": not wake_missing, "missing": wake_missing}
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
        gate, meter = SpeechGate(), win.PlaybackMeter()
        media_until = 0.0
        log.info("listening for JARVIS (wake word%s)", " and any speech" if self.any_speech else "")
        while not stop.is_set():
            try:
                with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK) as mic:
                    while not stop.is_set():
                        data, _ = mic.read(CHUNK)
                        chunk = bytes(data)
                        now = time.monotonic()
                        if self.live_busy() and self.live_talking():
                            # JARVIS is talking: nothing the mic hears cuts it off, except "Hey JARVIS"
                            gate.reset()
                            if detector.heard(chunk) is not None:
                                log.info("wake word heard over the reply: stopping it")
                                self.on_barge()
                                self.emit({"event": "barge"})
                            continue
                        if self._busy.locked() or self.live_busy() or now < self._quiet_until:
                            detector.reset()  # already taking a request: do not trigger on it
                            gate.reset()
                            continue
                        if meter.peak() > MEDIA_PEAK:
                            media_until = now + 1.5  # playing now, or was a moment ago
                        score = detector.heard(chunk)
                        if score is not None:
                            log.info("wake word heard (%.2f)", score)
                            gate.reset()
                            self._quiet_until = now + 3.0
                            self.on_wake(score)
                            continue
                        if not self.any_speech or self._hearing.locked():
                            continue
                        pcm = gate.feed(chunk)
                        if pcm:
                            threading.Thread(target=self._consider, args=(pcm, now < media_until), daemon=True).start()
            except Exception as e:  # noqa: BLE001 - device unplugged or busy: try again shortly
                log.warning("microphone unavailable (%s); retrying in 3s", e)
                stop.wait(3)
        meter.close()

    def _consider(self, pcm: bytes, media_playing: bool) -> None:
        """Transcribe an overheard utterance and wake if it was meant for JARVIS."""
        if not self._hearing.acquire(blocking=False):
            return
        try:
            setup = prepare()
            if not setup.get("whisper"):
                return  # without Whisper only "Hey JARVIS" wakes it
            raw = transcribe(to_wav(pcm), setup, strip_name=False)
            if not raw or not addressed(raw, media_playing):
                if raw:
                    log.info("overheard speech, not for JARVIS (%d words, media %s)", len(raw.split()), media_playing)
                return
            if self._busy.locked() or self.live_busy():
                return
            log.info("woken by speech (media %s)", media_playing)
            self._quiet_until = time.monotonic() + 3.0
            if self.mode == "live":
                self.emit({"event": "wake", "text": raw})  # the backend opens a live session that answers it
                return
            request = clean_transcript(raw)
            if len(request.split()) >= 2:
                self.stop_speaking()
                self.emit({"event": "transcript", "text": request})
            else:
                self.on_wake(1.0)  # just the name or a greeting: listen for the request
        except Exception:  # noqa: BLE001 - never kill the listener
            log.exception("could not consider overheard speech")
        finally:
            self._hearing.release()

    def on_wake(self, confidence: float = 1.0) -> None:
        if self.mode == "live":
            self.emit({"event": "wake", "confidence": confidence})  # the backend opens a live session
            return
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
