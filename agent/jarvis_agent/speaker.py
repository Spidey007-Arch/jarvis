"""Speaker verification: does this audio sound like the user? Runs offline on onnxruntime.

Model: WeSpeaker ResNet34 (VoxCeleb), wespeaker_en_voxceleb_resnet34_LM.onnx from the sherpa-onnx
releases. It takes Kaldi-style log mel filterbanks (80 bands, 25 ms frames every 10 ms, raw 16-bit
sample scale, mean-normalized) and returns a 256-value voice embedding; two clips of the same person
point the same way (cosine similarity), different people do not.

Enrollment records the user reading for a few seconds; the voiceprint is the normalized mean of the
embeddings of 3 s pieces of it, saved in <data>/voice/voiceprint.json. Nothing leaves the PC."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.speaker")

RATE = 16000
MODEL = "wespeaker_en_voxceleb_resnet34_LM.onnx"
THRESHOLD = 0.42  # cosine similarity to the voiceprint; same speaker is typically 0.55-0.8, others below 0.3
MIN_SPEECH_S = 0.8  # shorter than this says too little about who is speaking
PIECE_S = 3.0


def _voice_dir() -> Path:
    from .voice import voice_dir

    return voice_dir()


def model_path() -> Path:
    return _voice_dir() / "downloads" / MODEL


def print_path() -> Path:
    return _voice_dir() / "voiceprint.json"


_MEL: Any = None


def _mel_banks(np: Any) -> Any:
    """Kaldi's triangular mel filters: 80 bands from 20 Hz to Nyquist on a 512-point FFT."""
    global _MEL
    if _MEL is None:
        n_fft, bins, low, high = 512, 80, 20.0, RATE / 2
        mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)  # noqa: E731
        lo, hi = mel(low), mel(high)
        centers = lo + (hi - lo) / (bins + 1) * np.arange(bins + 2)
        freqs = mel(np.arange(n_fft // 2) * RATE / n_fft)
        banks = np.zeros((bins, n_fft // 2 + 1), dtype=np.float32)
        for b in range(bins):
            left, mid, right = centers[b], centers[b + 1], centers[b + 2]
            up = (freqs - left) / (mid - left)
            down = (right - freqs) / (right - mid)
            banks[b, : n_fft // 2] = np.maximum(0.0, np.minimum(up, down))
        _MEL = banks
    return _MEL


def fbank(pcm: bytes) -> Any:
    """80-band log mel filterbank features, as Kaldi computes them (no dither), mean-normalized."""
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    frame, shift = 400, 160
    if len(x) < frame:
        return np.zeros((0, 80), dtype=np.float32)
    n = 1 + (len(x) - frame) // shift
    idx = np.arange(frame)[None, :] + shift * np.arange(n)[:, None]
    frames = x[idx]
    frames = frames - frames.mean(axis=1, keepdims=True)  # remove DC
    frames = np.concatenate([frames[:, :1], frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)  # pre-emphasis
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(frame) / (frame - 1))) ** 0.85  # Povey window
    spec = np.abs(np.fft.rfft(frames * window, n=512)) ** 2
    feats = np.log(np.maximum(spec @ _mel_banks(np).T, np.finfo(np.float32).eps))
    return (feats - feats.mean(axis=0, keepdims=True)).astype(np.float32)


class Verifier:
    def __init__(self) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        self.session = ort.InferenceSession(str(model_path()), sess_options=opts, providers=["CPUExecutionProvider"])
        self.voiceprint: Any = None
        self.threshold = THRESHOLD
        self.load()

    def load(self) -> None:
        import numpy as np

        try:
            data = json.loads(print_path().read_text(encoding="utf-8"))
            self.voiceprint = np.asarray(data["embedding"], dtype=np.float32)
            self.threshold = float(data.get("threshold", THRESHOLD))
        except (OSError, ValueError, KeyError):
            self.voiceprint = None

    @property
    def enrolled(self) -> bool:
        return self.voiceprint is not None

    def embed(self, pcm: bytes) -> Any:
        import numpy as np

        feats = fbank(pcm)
        if len(feats) < 50:
            raise ValueError("too little audio to tell who is speaking")
        e = self.session.run(None, {"feats": feats[None, :, :]})[0][0]
        return e / (np.linalg.norm(e) + 1e-9)

    def score(self, pcm: bytes) -> float:
        """Cosine similarity of this speech to the user's voiceprint (1.0 if nobody is enrolled)."""
        import numpy as np

        if not self.enrolled:
            return 1.0
        if len(pcm) / 2 / RATE < MIN_SPEECH_S:
            return 0.0
        return float(np.dot(self.embed(pcm), self.voiceprint))

    def is_user(self, pcm: bytes) -> bool:
        if not self.enrolled:
            return True
        s = self.score(pcm)
        log.info("speaker check %.2f (needs %.2f)", s, self.threshold)
        return s >= self.threshold

    def enroll(self, pcm: bytes) -> dict[str, Any]:
        """Make the voiceprint from a recording of the user speaking (at least ~9 s of speech)."""
        import numpy as np

        piece = int(PIECE_S * RATE) * 2
        chunks = [pcm[i:i + piece] for i in range(0, len(pcm) - piece + 1, piece)]
        chunks = [c for c in chunks if _voiced(c) > 0.5]  # the parts where the user was actually talking
        if len(chunks) < 3:
            raise ValueError("I didn't hear enough speech. Read the whole paragraph aloud, close to the microphone.")
        embs = np.stack([self.embed(c) for c in chunks])
        center = embs.mean(axis=0)
        center /= np.linalg.norm(center) + 1e-9
        sims = embs @ center
        consistency = float(sims.mean())
        if consistency < 0.6:
            raise ValueError("The recording sounded like more than one voice or a lot of noise. Try again somewhere quieter.")
        # Accept speech nearly as close to the voiceprint as the enrollment pieces themselves were.
        threshold = round(max(0.35, min(0.5, float(sims.min()) - 0.25)), 2)
        print_path().write_text(json.dumps({"embedding": center.tolist(), "threshold": threshold, "pieces": len(chunks),
                                            "consistency": round(consistency, 3), "created": time.time()}), encoding="utf-8")
        self.voiceprint, self.threshold = center, threshold
        return {"enrolled": True, "pieces": len(chunks), "consistency": round(consistency, 2), "threshold": threshold}

    def forget(self) -> None:
        print_path().unlink(missing_ok=True)
        self.voiceprint = None


def _voiced(pcm: bytes) -> float:
    """Share of 50 ms blocks clearly above the quietest part of the clip: roughly, how much is speech."""
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    blocks = x[: len(x) // 800 * 800].reshape(-1, 800)
    if not len(blocks):
        return 0.0
    rms = np.sqrt((blocks ** 2).mean(axis=1))
    floor = np.percentile(rms, 10)
    return float((rms > max(110.0, floor * 2.5)).mean())


_VERIFIER: Verifier | None = None
_LOCK = threading.Lock()


def verifier() -> Verifier | None:
    """The shared verifier, or None if the model is not downloaded (then nobody is checked)."""
    global _VERIFIER
    with _LOCK:
        if _VERIFIER is None and model_path().is_file():
            try:
                _VERIFIER = Verifier()
            except Exception:  # noqa: BLE001 - a broken model must not stop voice
                log.exception("speaker model could not load")
        return _VERIFIER
