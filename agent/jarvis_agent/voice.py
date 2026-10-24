"""Voice: wake word, listening, transcription and speech, all on this PC.

- Wake word: Windows' offline recognizer, constrained to "JARVIS" / "hey JARVIS", with a dictation
  grammar competing so ordinary speech is not forced into a match.
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
WAKE_CONFIDENCE = 0.72
MAX_UTTERANCE_S = 15.0
NO_SPEECH_S = 5.0  # give up if nothing is said this long after the wake word
END_SILENCE_S = 1.0

FILES = {
    "whisper_zip": "whisper-bin-x64.zip",
    "whisper_model": "ggml-base.en-q5_1.bin",
    "piper_zip": "piper_windows_amd64.zip",
    "voice_model": "en_GB-alan-medium.onnx",
    "voice_config": "en_GB-alan-medium.onnx.json",
}
# A download still in progress already exists as a file; only a complete one counts.
MIN_BYTES = {"whisper_zip": 8_000_000, "whisper_model": 59_000_000, "piper_zip": 22_000_000, "voice_model": 63_000_000, "voice_config": 4_000}

_WAKE_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$rec = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers() |
  Sort-Object { if ($_.Culture.Name -eq 'en-GB') { 0 } elseif ($_.Culture.Name -like 'en-*') { 1 } else { 2 } } | Select-Object -First 1
if (-not $rec) { [Console]::Out.WriteLine('ERROR no speech recognizer is installed'); exit 2 }
$r = New-Object System.Speech.Recognition.SpeechRecognitionEngine($rec)
$c = New-Object System.Speech.Recognition.Choices
$c.Add([string[]]@('jarvis', 'hey jarvis', 'okay jarvis', 'ok jarvis'))
$gb = New-Object System.Speech.Recognition.GrammarBuilder($c); $gb.Culture = $rec.Culture
$wake = New-Object System.Speech.Recognition.Grammar($gb); $wake.Name = 'wake'
$r.LoadGrammar($wake)
try { $d = New-Object System.Speech.Recognition.DictationGrammar; $d.Name = 'other'; $r.LoadGrammar($d) } catch { }
if ($env:JARVIS_WAKE_WAV) { $r.SetInputToWaveFile($env:JARVIS_WAKE_WAV) } else { $r.SetInputToDefaultAudioDevice() }
[Console]::Out.WriteLine('READY ' + $rec.Culture.Name); [Console]::Out.Flush()
while ($true) {
  # The agent that started this listener is gone (crash, restart): release the microphone and exit.
  if ($env:JARVIS_AGENT_PID -and -not (Get-Process -Id $env:JARVIS_AGENT_PID -ErrorAction SilentlyContinue)) { exit 0 }
  try { $res = $r.Recognize([TimeSpan]::FromSeconds(4)) }
  catch { if ($env:JARVIS_WAKE_WAV) { break }; Start-Sleep -Seconds 1; try { $r.SetInputToDefaultAudioDevice() } catch { }; continue }
  if ($res -and $res.Grammar.Name -eq 'wake') { [Console]::Out.WriteLine('WAKE ' + $res.Confidence.ToString('0.00')); [Console]::Out.Flush() }
  elseif ($env:JARVIS_WAKE_WAV -and -not $res) { break }
}
"""


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
        self._wake: subprocess.Popen | None = None
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
        if enabled and (self._wake is None or self._wake.poll() is not None):
            self._wake = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _WAKE_PS],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=win.CREATE_NO_WINDOW,
                                          env={**os.environ, "JARVIS_AGENT_PID": str(os.getpid())})
            threading.Thread(target=self._read_wake, args=(self._wake,), daemon=True).start()
        if not enabled and self._wake is not None:
            self._wake.kill()
            self._wake = None
        return {"enabled": self.enabled, **setup}

    def _read_wake(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if line.startswith("READY"):
                log.info("wake word listening (%s)", line.split(" ", 1)[-1])
            elif line.startswith("WAKE"):
                try:
                    conf = float(line.split()[1])
                except (IndexError, ValueError):
                    conf = 0.0
                if conf >= WAKE_CONFIDENCE and self.enabled:
                    self.on_wake(conf)
            elif line.startswith("ERROR"):
                self.emit({"event": "error", "message": line[6:]})
        if self.enabled and proc is self._wake:
            log.warning("wake listener exited; restarting in 3s")
            time.sleep(3)
            if self.enabled:
                self._wake = None
                self.set_enabled(True)

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
