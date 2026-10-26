"""Audio for live voice sessions: the microphone streamed to the backend, and the reply played as it arrives.

Mic: 16 kHz 16-bit mono in 40 ms frames. While JARVIS is talking the mic is sent as silence, so it
finishes in one go: its own voice coming back through the mic, room noise or someone talking cannot
cut it off or be taken as a new request. "Hey JARVIS" (heard by the wake word listener) stops it.
Speaker: Gemini's 24 kHz 16-bit mono, queued and written to the default output device. Each reply is buffered PRIME_S before it starts, and again if the network falls behind, so a slow
line gives a brief pause instead of choppy audio. flush() drops whatever has not been played yet, so an
interruption is instant."""
from __future__ import annotations

import base64
import logging
import queue
import threading
import time
from typing import Any, Callable

from . import win

log = logging.getLogger("jarvis.live_audio")

MIC_RATE = 16000
MIC_FRAME = 640  # 40 ms
SPEAKER_RATE = 24000
BYTES_PER_S = SPEAKER_RATE * 2
PRIME_S = 0.35  # audio held back before a reply starts playing, to ride out network jitter
PRIME_WAIT_S = 0.6  # never hold back longer than this, even if the reply is shorter


class VoiceGate:
    """Lets only the user's own speech through to the live conversation. Each burst of speech is held
    back (silence is sent meanwhile) until there is JUDGE_S of it, or it ends; then it is checked against
    the voiceprint. The user's speech is sent on at once and the rest of that burst streams live; anyone
    else's (another person, the TV) is dropped until they stop."""

    JUDGE_S = 1.2
    ONSET_S = 0.16
    END_S = 0.6
    PREROLL = 6  # frames (240 ms) kept from before the onset, so the first syllable is not lost

    def __init__(self, verifier: Any) -> None:
        import array
        import math

        self.v = verifier
        self._rms = lambda c: math.sqrt(sum(x * x for x in array.array("h", c)) / max(1, len(c) // 2))
        self.floor = 60.0
        self.state = "idle"  # idle -> judging -> pass | drop -> idle
        self.held: list[bytes] = []
        self.loud = self.quiet = 0.0

    def feed(self, frame: bytes) -> list[bytes]:
        secs = len(frame) / 2 / MIC_RATE
        silent = bytes(len(frame))
        level = self._rms(frame)
        loud = level > max(110.0, self.floor * 2.5)
        self.quiet = 0.0 if loud else self.quiet + secs
        if self.state == "idle":
            if not loud:
                self.floor += (level - self.floor) * 0.05
            self.loud = self.loud + secs if loud else 0.0
            self.held = (self.held + [frame])[-self.PREROLL:]
            if self.loud >= self.ONSET_S:
                self.state = "judging"
            return [silent]
        if self.state == "judging":
            self.held.append(frame)
            ended = self.quiet >= self.END_S
            if len(self.held) * secs < self.JUDGE_S and not ended:
                return [silent]
            try:
                mine = self.v.is_user(b"".join(self.held))
            except Exception:  # noqa: BLE001 - too short or a model hiccup: not proven to be the user
                mine = False
            held, self.held = self.held, []
            if ended:
                self.state = "idle"
                return held if mine else [silent]
            self.state = "pass" if mine else "drop"
            return held if mine else [silent]
        # pass or drop, until the speaker pauses
        out = [frame] if self.state == "pass" else [silent]
        if self.quiet >= self.END_S:
            self.state, self.loud, self.held = "idle", 0.0, []
        return out


class LiveAudio:
    def __init__(self, send: Callable[[dict[str, Any]], None], levels: Any = None) -> None:
        self.send = send
        self.levels = levels  # speech.Levels: drives the arc reactor with both voices
        self._mic_stop: threading.Event | None = None
        self._play: queue.Queue[bytes] = queue.Queue()
        self._speaker: threading.Thread | None = None
        self._generation = 0
        self._playing_until = 0.0  # when the audio written so far finishes coming out of the speakers

    @property
    def mic_on(self) -> bool:
        return self._mic_stop is not None and not self._mic_stop.is_set()

    @property
    def talking(self) -> bool:
        return time.monotonic() < self._playing_until + 0.25

    # ----- microphone -----

    def set_mic(self, on: bool) -> None:
        if on and not self.mic_on:
            self._mic_stop = threading.Event()
            threading.Thread(target=self._stream_mic, args=(self._mic_stop,), daemon=True).start()
        elif not on and self._mic_stop is not None:
            self._mic_stop.set()
            self._mic_stop = None
            self.flush()

    def gate(self, chunk: bytes) -> bytes:
        """What to send for this mic frame: itself, or silence while JARVIS is talking."""
        return bytes(len(chunk)) if self.talking else chunk

    def _stream_mic(self, stop: threading.Event) -> None:
        import sounddevice as sd

        from . import speaker

        v = speaker.verifier()
        voices = VoiceGate(v) if v and v.enrolled else None  # with a voiceprint, only the user's speech goes through
        try:
            with sd.RawInputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=MIC_FRAME) as mic:
                while not stop.is_set():
                    data, _ = mic.read(MIC_FRAME)
                    frames = [self.gate(bytes(data))]
                    if self.levels is not None and not self.talking:
                        self.levels.feed("user", bytes(data), MIC_RATE)
                    if voices is not None:
                        frames = voices.feed(frames[0])
                    for f in frames:
                        self.send({"type": "mic", "pcm": base64.b64encode(f).decode()})
        except Exception as e:  # noqa: BLE001
            log.exception("live microphone failed")
            self.send({"type": "voice", "event": "error", "message": f"Microphone failed: {e}"})

    # ----- speaker -----

    def play(self, b64: str) -> None:
        self._play.put(base64.b64decode(b64))
        if self._speaker is None or not self._speaker.is_alive():
            self._speaker = threading.Thread(target=self._play_loop, daemon=True)
            self._speaker.start()

    def flush(self) -> None:
        """The user interrupted: forget everything not yet played."""
        self._generation += 1
        self._playing_until = 0.0
        while True:
            try:
                self._play.get_nowait()
            except queue.Empty:
                break

    def _prime(self, first: bytes) -> bytes:
        """Collect PRIME_S of audio (or whatever arrives within PRIME_WAIT_S) before playing starts."""
        buf = [first]
        size = len(first)
        deadline = time.monotonic() + PRIME_WAIT_S
        while size < PRIME_S * BYTES_PER_S:
            try:
                more = self._play.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            buf.append(more)
            size += len(more)
        return b"".join(buf)

    def _play_loop(self) -> None:
        import sounddevice as sd

        from .speech import open_output

        win.boost_thread()
        out = None
        try:
            out = open_output(SPEAKER_RATE)
            primed = False
            while True:
                try:
                    chunk = self._play.get(timeout=0.05 if primed else 5.0)
                except queue.Empty:
                    if not primed:
                        return  # quiet for a while: release the device; the next reply reopens it
                    if time.monotonic() < self._playing_until - 0.05:
                        continue  # still playing what was written; more may come
                    primed = False  # ran dry (reply over, or the network is behind): buffer again
                    continue
                gen = self._generation
                if not primed:
                    chunk = self._prime(chunk)
                    primed = True
                # write in small pieces so a flush stops playback within ~40 ms
                step = SPEAKER_RATE // 25 * 2
                for i in range(0, len(chunk), step):
                    if gen != self._generation:
                        break
                    piece = chunk[i:i + step]
                    if self.levels is not None:
                        self.levels.feed("jarvis", piece, SPEAKER_RATE)
                    try:
                        out.write(piece)
                    except sd.PortAudioError as e:  # the device vanished (Bluetooth mode switch, unplugged): reopen once
                        log.warning("sound device changed while speaking (%s); reopening", e)
                        out.close()
                        time.sleep(0.4)
                        out = open_output(SPEAKER_RATE)
                        out.write(piece)
                    now = time.monotonic()
                    self._playing_until = max(self._playing_until, now) + len(piece) / BYTES_PER_S
        except Exception:  # noqa: BLE001
            log.exception("live speaker failed")
        finally:
            if out is not None:
                try:
                    out.close()
                except Exception:  # noqa: BLE001
                    pass
