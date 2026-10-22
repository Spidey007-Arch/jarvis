"""User settings, stored encrypted with Windows DPAPI (settings.dat)."""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from jarvis_common import dpapi

log = logging.getLogger("jarvis.settings")

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "user_name": "",
    "address": "sir",
    "chat_model": "",
    "embed_model": "",
    "secrets": {},
}

PUBLIC_FIELDS = ("user_name", "address", "chat_model", "embed_model")


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.load_error: str | None = None
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        data = copy.deepcopy(DEFAULTS)
        if not self.path.exists():
            return data
        try:
            stored = json.loads(dpapi.unprotect(self.path.read_bytes()).decode("utf-8"))
            data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except Exception as e:  # noqa: BLE001 - keep running, but say exactly what happened
            backup = self.path.with_suffix(".unreadable")
            os.replace(self.path, backup)
            self.load_error = f"Saved settings could not be decrypted ({e}); they were moved to {backup.name}."
            log.error(self.load_error)
        return data

    def _save(self) -> None:
        raw = dpapi.protect(json.dumps(self._data).encode("utf-8"))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, self.path)

    def get(self, key: str) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, DEFAULTS.get(key)))

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            for k, v in values.items():
                if k in PUBLIC_FIELDS:
                    self._data[k] = v
            self._save()

    def secret(self, name: str) -> str:
        with self._lock:
            return self._data["secrets"].get(name, "")

    def set_secret(self, name: str, value: str) -> None:
        with self._lock:
            if value:
                self._data["secrets"][name] = value
            else:
                self._data["secrets"].pop(name, None)
            self._save()

    def public(self) -> dict[str, Any]:
        key = self.secret("gemini_api_key")
        with self._lock:
            out = {k: self._data.get(k) for k in PUBLIC_FIELDS}
        out["has_gemini_key"] = bool(key)
        out["gemini_key_hint"] = f"…{key[-4:]}" if len(key) >= 8 else ""
        out["load_error"] = self.load_error
        return out
