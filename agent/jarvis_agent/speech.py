"""Offline speech output, and the voice levels that drive the arc reactor.

Speech: Piper stays loaded (its --output_dir mode reads one sentence per line and writes one WAV each), so
only the first sentence pays for loading. Text is split into sentences; the next one is synthesized while
the current one plays, and say(..., queue=True) lets a reply be spoken sentence by sentence as it streams
in. Playback goes through sounddevice in small blocks, so stop() is immediate.

Levels: whatever JARVIS says, and what the user says while it listens, is measured every 50 ms and sent to
the UI in batches of five (four events a second), so the core can pulse with the actual voice."""
from __future__ import annotations

import array
import logging
import math
import queue
import re
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

from . import win

log = logging.getLogger("jarvis.speech")

STEP_S = 0.05  # one level value per 50 ms
BATCH = 5  # values per event: four events a second


class Levels:
    """Turns audio into 0..1 loudness values and sends them in small batches."""

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self.emit = emit
        self._lock = threading.Lock()
        self._pending: dict[str, list[float]] = {}
        self._rest: dict[str, bytes] = {}
        self._floor: dict[str, float] = {}  # background noise per source, so silence reads as 0
        self._silent: dict[str, bool] = {}

    def feed(self, who: str, pcm: bytes, rate: int) -> None:
        """who: 'jarvis' (its voice) or 'user' (the microphone while listening)."""
        step = int(rate * STEP_S) * 2
        ref = 9000.0 if who == "jarvis" else 2200.0  # laptop mics are quiet: scale the user's voice up
        with self._lock:
            data = self._rest.get(who, b"") + pcm
            out = self._pending.setdefault(who, [])
            i = 0
            while i + step <= len(data):
                a = array.array("h", data[i:i + step])
                rms = math.sqrt(sum(x * x for x in a) / len(a))
                floor = self._floor.get(who, rms)
                # follow the noise floor down quickly and up slowly: speech rises far above it
                floor = floor + (rms - floor) * (0.3 if rms < floor else 0.003)
                self._floor[who] = floor
                above = max(0.0, rms - floor * 1.8)
                out.append(round(min(1.0, (above / ref) ** 0.6), 2))
                i += step
            self._rest[who] = data[i:]
            while len(out) >= BATCH:
                batch, out[:] = out[:BATCH], out[BATCH:]
                quiet = max(batch) < 0.03
                if quiet and self._silent.get(who):
                    continue  # nothing to show, and the UI already knows it went quiet
                self._silent[who] = quiet
                self.emit({"event": "levels", "who": who, "v": batch})


def sentences(text: str) -> list[str]:
    """Split into sentences, keeping very short ones with the next so the voice doesn't stutter."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+|\n+", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) < 25:
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return out


class Piper:
    """One long-running piper.exe: a line of text in, the path of its WAV out."""

    def __init__(self, exe: str, model: str) -> None:
        self.exe, self.model = exe, model
        self.dir = Path(tempfile.mkdtemp(prefix="jarvis-tts-"))
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def _start(self) -> subprocess.Popen:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [self.exe, "--model", self.model, "--output_dir", str(self.dir), "--sentence_silence", "0.1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                cwd=str(Path(self.exe).parent), creationflags=win.CREATE_NO_WINDOW | win.ABOVE_NORMAL_PRIORITY_CLASS)
        return self.proc

    def synth(self, text: str) -> Path:
        line = re.sub(r"\s+", " ", text).strip()
        with self.lock:
            for attempt in range(2):
                p = self._start()
                try:
                    p.stdin.write(line + "\n")
                    p.stdin.flush()
                    out = p.stdout.readline().strip()
                except (OSError, ValueError):
                    out = ""
                if out and Path(out).is_file():
                    return Path(out)
                self.close()  # crashed or stuck: start a fresh one and try once more
        raise RuntimeError("Piper did not produce speech.")

    def warm(self) -> None:
        try:
            self.synth("Ready.").unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - the first real sentence will report it
            pass

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None


class Speaker:
    """Sentence-by-sentence speech: synthesize ahead, play in order, stop at once."""

    def __init__(self, emit: Callable[[dict[str, Any]], None], levels: Levels) -> None:
        self.emit, self.levels = emit, levels
        self.piper: Piper | None = None
        self._text: queue.Queue[tuple[int, str]] = queue.Queue()
        self._audio: queue.Queue[tuple[int, Path | None]] = queue.Queue()
        self._gen = 0
        self._busy = False
        self._threads: list[threading.Thread] = []

    @property
    def speaking(self) -> bool:
        return self._busy

    def setup(self, exe: str, model: str) -> None:
        if self.piper is None or (self.piper.exe, self.piper.model) != (exe, model):
            if self.piper:
                self.piper.close()
            self.piper = Piper(exe, model)
            threading.Thread(target=self.piper.warm, daemon=True).start()
        if not self._threads:
            self._threads = [threading.Thread(target=self._synth_loop, daemon=True), threading.Thread(target=self._play_loop, daemon=True)]
            for t in self._threads:
                t.start()

    def say(self, text: str, queue_after: bool = False) -> int:
        """Speak text; with queue_after it follows what is already being said (a reply streaming in)."""
        if not queue_after:
            self.stop(quiet=True)
        parts = sentences(text)
        for s in parts:
            self._text.put((self._gen, s))
        return len(parts)

    def stop(self, quiet: bool = False) -> None:
        was = self._busy
        self._gen += 1
        for q in (self._text, self._audio):
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if q is self._audio and item[1]:
                    item[1].unlink(missing_ok=True)
        if was and not quiet:
            self.emit({"event": "idle", "reason": "interrupted"})

    def _synth_loop(self) -> None:
        while True:
            gen, text = self._text.get()
            if gen != self._gen or self.piper is None:
                continue
            try:
                self._audio.put((gen, self.piper.synth(text)))
            except Exception as e:  # noqa: BLE001
                log.exception("speech synthesis failed")
                self.emit({"event": "error", "message": f"Could not speak: {e}"})
                self._audio.put((gen, None))

    def _play_loop(self) -> None:
        import sounddevice as sd

        while True:
            gen, path = self._audio.get()
            if path is None or gen != self._gen:
                if path:
                    path.unlink(missing_ok=True)
                continue
            try:
                with wave.open(str(path), "rb") as w:
                    rate, pcm = w.getframerate(), w.readframes(w.getnframes())
                if not self._busy:
                    self._busy = True
                    self.emit({"event": "speaking"})
                block = int(rate * STEP_S) * 2
                with sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16") as out:
                    for i in range(0, len(pcm), block):
                        if gen != self._gen:
                            break
                        piece = pcm[i:i + block]
                        self.levels.feed("jarvis", piece, rate)
                        out.write(piece)
            except Exception:  # noqa: BLE001
                log.exception("speech playback failed")
            finally:
                path.unlink(missing_ok=True)
            if self._text.empty() and self._audio.empty():
                time.sleep(0.35)  # a streamed reply may still be sending its next sentence
                if self._text.empty() and self._audio.empty() and self._busy:
                    self._busy = False
                    if gen == self._gen:
                        self.emit({"event": "idle", "reason": "spoken"})
