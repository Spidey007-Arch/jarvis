"""Audio for live voice sessions: the microphone streamed to the backend, and the reply played as it arrives.

Mic: 16 kHz 16-bit mono in 40 ms frames. Speaker: Gemini's 24 kHz 16-bit mono, queued and written to the
default output device; flush() drops whatever has not been played yet, so an interruption is instant."""
from __future__ import annotations

import base64
import logging
import queue
import threading
from typing import Any, Callable

log = logging.getLogger("jarvis.live_audio")

MIC_RATE = 16000
MIC_FRAME = 640  # 40 ms
SPEAKER_RATE = 24000


class LiveAudio:
    def __init__(self, send: Callable[[dict[str, Any]], None]) -> None:
        self.send = send
        self._mic_stop: threading.Event | None = None
        self._play: queue.Queue[bytes] = queue.Queue()
        self._speaker: threading.Thread | None = None
        self._generation = 0

    @property
    def mic_on(self) -> bool:
        return self._mic_stop is not None and not self._mic_stop.is_set()

    # ----- microphone -----

    def set_mic(self, on: bool) -> None:
        if on and not self.mic_on:
            self._mic_stop = threading.Event()
            threading.Thread(target=self._stream_mic, args=(self._mic_stop,), daemon=True).start()
        elif not on and self._mic_stop is not None:
            self._mic_stop.set()
            self._mic_stop = None
            self.flush()

    def _stream_mic(self, stop: threading.Event) -> None:
        import sounddevice as sd

        try:
            with sd.RawInputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=MIC_FRAME) as mic:
                while not stop.is_set():
                    data, _ = mic.read(MIC_FRAME)
                    self.send({"type": "mic", "pcm": base64.b64encode(bytes(data)).decode()})
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
        while True:
            try:
                self._play.get_nowait()
            except queue.Empty:
                break

    def _play_loop(self) -> None:
        import sounddevice as sd

        try:
            with sd.RawOutputStream(samplerate=SPEAKER_RATE, channels=1, dtype="int16") as out:
                while True:
                    try:
                        chunk = self._play.get(timeout=5.0)
                    except queue.Empty:
                        return  # quiet for a while: release the device; the next reply reopens it
                    gen = self._generation
                    # write in small pieces so a flush stops playback within ~40 ms
                    step = SPEAKER_RATE // 25 * 2
                    for i in range(0, len(chunk), step):
                        if gen != self._generation:
                            break
                        out.write(chunk[i:i + step])
        except Exception:  # noqa: BLE001
            log.exception("live speaker failed")
