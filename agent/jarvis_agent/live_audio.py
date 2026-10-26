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

log = logging.getLogger("jarvis.live_audio")

MIC_RATE = 16000
MIC_FRAME = 640  # 40 ms
SPEAKER_RATE = 24000
BYTES_PER_S = SPEAKER_RATE * 2
PRIME_S = 0.35  # audio held back before a reply starts playing, to ride out network jitter
PRIME_WAIT_S = 0.6  # never hold back longer than this, even if the reply is shorter


class LiveAudio:
    def __init__(self, send: Callable[[dict[str, Any]], None]) -> None:
        self.send = send
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

        try:
            with sd.RawInputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=MIC_FRAME) as mic:
                while not stop.is_set():
                    data, _ = mic.read(MIC_FRAME)
                    self.send({"type": "mic", "pcm": base64.b64encode(self.gate(bytes(data))).decode()})
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

        try:
            with sd.RawOutputStream(samplerate=SPEAKER_RATE, channels=1, dtype="int16", latency="high") as out:
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
                        out.write(piece)
                        now = time.monotonic()
                        self._playing_until = max(self._playing_until, now) + len(piece) / BYTES_PER_S
        except Exception:  # noqa: BLE001
            log.exception("live speaker failed")
