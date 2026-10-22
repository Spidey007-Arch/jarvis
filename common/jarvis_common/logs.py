"""Structured logging into <data>/logs: one file per concern plus error.log."""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone

from jarvis_common.paths import logs_dir


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            entry.update(extra)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def _file_handler(name: str, level: int = logging.DEBUG) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        logs_dir() / f"{name}.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    return handler


def setup(component: str) -> logging.Logger:
    """Configure logging for `component` ("backend" or "agent")."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(_file_handler(component))
    root.addHandler(_file_handler("error", logging.ERROR))
    # The shell polls /health every second; per-request access lines would drown the log.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(JsonFormatter())
        root.addHandler(console)
    # automation actions get their own audit trail
    audit = logging.getLogger("jarvis.automation")
    audit.addHandler(_file_handler("automation"))
    return logging.getLogger(f"jarvis.{component}")
