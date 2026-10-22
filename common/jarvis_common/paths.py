"""Filesystem locations shared by the backend and agent."""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("JARVIS_DATA_DIR")
    if env:
        base = Path(env)
    else:
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JARVIS"
    base.mkdir(parents=True, exist_ok=True)
    return base


def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def runtime_file() -> Path:
    return data_dir() / "runtime.json"
