"""'Hey JARVIS' detection with openWakeWord's models, run directly on onnxruntime.

Every 80 ms of 16 kHz audio: a mel spectrogram of the last 0.78 s (76 frames) feeds the shared speech
embedding model, giving one 96-value embedding; the wake model scores the last 16 embeddings (about
1.3 s). That is the openWakeWord pipeline with one new embedding per chunk, so it stays light on CPU.
Models: melspectrogram.onnx, embedding_model.onnx and hey_jarvis_v0.1.onnx from openWakeWord v0.5.1."""
from __future__ import annotations

from collections import deque
from pathlib import Path

CHUNK = 1280  # 80 ms at 16 kHz: one embedding step (8 mel frames of 10 ms)
_MEL_WINDOW = 12800  # 0.8 s: enough for 76+ mel frames (10 ms hop); the last 76 are used
_EMBEDDINGS = 16
THRESHOLD = 0.5

MODELS = ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")


class WakeWord:
    def __init__(self, folder: Path, threshold: float = THRESHOLD) -> None:
        import numpy as np
        import onnxruntime as ort

        self.np = np
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1  # always-on: keep it to one core
        opts.inter_op_num_threads = 1

        def load(name: str):
            return ort.InferenceSession(str(folder / name), sess_options=opts, providers=["CPUExecutionProvider"])

        self.mel, self.emb, self.wake = (load(n) for n in MODELS)
        self.mel_in = self.mel.get_inputs()[0].name
        self.emb_in = self.emb.get_inputs()[0].name
        self.wake_in = self.wake.get_inputs()[0].name
        self.threshold = threshold
        self.reset()

    def reset(self) -> None:
        self.audio = self.np.zeros(_MEL_WINDOW, dtype=self.np.float32)
        self.embeddings: deque = deque(maxlen=_EMBEDDINGS)
        self.cooldown = 0

    def process(self, chunk: bytes) -> float:
        """Feed 80 ms of 16-bit mono PCM; returns the wake score (0..1) for the latest 1.3 s."""
        np = self.np
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        self.audio = np.concatenate([self.audio, samples])[-_MEL_WINDOW:]
        spec = np.squeeze(self.mel.run(None, {self.mel_in: self.audio[None, :]})[0]) / 10.0 + 2.0
        spec = spec[-76:]
        if spec.shape[0] < 76:
            return 0.0
        emb = self.emb.run(None, {self.emb_in: spec[None, :, :, None].astype(np.float32)})[0]
        self.embeddings.append(np.squeeze(emb))
        if len(self.embeddings) < _EMBEDDINGS:
            return 0.0
        score = float(np.squeeze(self.wake.run(None, {self.wake_in: np.stack(self.embeddings)[None, :, :].astype(np.float32)})[0]))
        if self.cooldown:
            self.cooldown -= 1
            return 0.0
        return score

    def heard(self, chunk: bytes) -> float | None:
        """The score if this chunk completes 'Hey JARVIS', else None. Ignores ~1.5 s after a hit."""
        score = self.process(chunk)
        if score >= self.threshold:
            self.cooldown = 18
            return score
        return None
