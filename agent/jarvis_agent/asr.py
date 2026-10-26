"""Speech to text with NVIDIA Parakeet (NeMo), run on this PC by sherpa-onnx.

NVIDIA's speech NIMs need Docker on Linux and a large GPU; the same Parakeet models run here directly on the
CPU through sherpa-onnx, int8-quantized. The model stays loaded, so each request costs only the decoding
itself (a fraction of a second for a spoken command), far faster and more accurate than Whisper base.
Whisper (voice.transcribe) remains the fallback when no Parakeet model is unpacked.

Models live in <data>/voice/parakeet, unpacked from the sherpa-onnx release archive in the downloads folder:
sherpa-onnx-nemo-parakeet_tdt_ctc_110m-en-36000-int8 (CTC) or sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8 (TDT)."""
from __future__ import annotations

import logging
import os
import tarfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.asr")

ARCHIVES = ("sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2", "sherpa-onnx-nemo-parakeet_tdt_ctc_110m-en-36000-int8.tar.bz2")
RATE = 16000


def _voice_dir() -> Path:
    from .voice import voice_dir

    return voice_dir()


def model_dir() -> Path | None:
    """The unpacked Parakeet model folder (the bigger one if both are there), unpacking an archive if needed."""
    root = _voice_dir() / "parakeet"
    for archive in ARCHIVES:
        name = archive.removesuffix(".tar.bz2")
        folder = root / name
        if (folder / "tokens.txt").is_file():
            return folder
        src = _voice_dir() / "downloads" / archive
        if src.is_file():
            try:
                root.mkdir(parents=True, exist_ok=True)
                with tarfile.open(src, "r:bz2") as tar:
                    tar.extractall(root, filter="data")  # refuses absolute paths and ../ escapes
                if (folder / "tokens.txt").is_file():
                    log.info("unpacked %s", name)
                    return folder
            except (tarfile.TarError, OSError) as e:
                log.warning("could not unpack %s: %s", archive, e)
    return None


class Parakeet:
    def __init__(self, folder: Path) -> None:
        import sherpa_onnx

        threads = max(2, min(4, os.cpu_count() or 2))
        enc = next(iter(sorted(folder.glob("encoder*.onnx"))), None)
        if enc is not None:  # TDT: a transducer
            self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(enc), decoder=str(next(folder.glob("decoder*.onnx"))), joiner=str(next(folder.glob("joiner*.onnx"))),
                tokens=str(folder / "tokens.txt"), num_threads=threads, model_type="nemo_transducer", decoding_method="greedy_search")
        else:  # CTC
            model = next(iter(sorted(folder.glob("model*.onnx"))))
            self.rec = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(model=str(model), tokens=str(folder / "tokens.txt"), num_threads=threads)
        self.name = folder.name
        self.lock = threading.Lock()

    def transcribe(self, pcm: bytes) -> str:
        import numpy as np

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        samples = np.concatenate([np.zeros(int(0.2 * RATE), np.float32), samples, np.zeros(int(0.3 * RATE), np.float32)])
        with self.lock:
            stream = self.rec.create_stream()
            stream.accept_waveform(RATE, samples)
            self.rec.decode_stream(stream)
            return stream.result.text.strip()


_MODEL: Parakeet | None = None
_TRIED: tuple[str, ...] | None = None  # the model files seen at the last attempt
_LOCK = threading.Lock()


def _present() -> tuple[str, ...]:
    d = _voice_dir()
    return tuple(a for a in ARCHIVES if (d / "downloads" / a).is_file() or (d / "parakeet" / a.removesuffix(".tar.bz2")).is_dir())


def recognizer() -> Parakeet | None:
    """The loaded Parakeet model, or None (then Whisper is used). A model that finishes downloading while
    JARVIS runs is picked up on the next request."""
    global _MODEL, _TRIED
    with _LOCK:
        if _MODEL is None and _TRIED != _present():
            _TRIED = _present()
            try:
                import sherpa_onnx  # noqa: F401

                folder = model_dir()
                if folder:
                    _MODEL = Parakeet(folder)
                    log.info("speech recognition: NVIDIA %s", folder.name)
            except Exception:  # noqa: BLE001 - fall back to Whisper
                log.exception("Parakeet could not load; using Whisper")
        return _MODEL


def status() -> dict[str, Any]:
    m = recognizer()
    return {"asr": f"NVIDIA {m.name}" if m else "Whisper base"}
