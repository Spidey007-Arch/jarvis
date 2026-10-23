"""User settings, stored encrypted with Windows DPAPI (settings.dat).

Each AI provider keeps its own key, base URL and model choices, so switching
providers and back loses nothing.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from jarvis_common import dpapi

from .providers import PRESETS

log = logging.getLogger("jarvis.settings")

VERSION = 2
DEFAULTS: dict[str, Any] = {
    "version": VERSION,
    "user_name": "",
    "address": "sir",
    "proactive_alerts": True,
    "provider": "gemini",
    "provider_config": {},
    "secrets": {},
}
PROFILE_FIELDS = ("user_name", "address", "proactive_alerts")
PROVIDER_FIELDS = ("chat_model", "embed_model", "base_url")


def _migrate(stored: dict[str, Any]) -> dict[str, Any]:
    """v1 stored a single Gemini key and models at the top level."""
    if stored.get("version", 1) >= VERSION:
        return stored
    secrets = stored.get("secrets", {})
    if "gemini_api_key" in secrets:
        secrets["api_key:gemini"] = secrets.pop("gemini_api_key")
    stored["provider_config"] = {
        "gemini": {"chat_model": stored.pop("chat_model", ""), "embed_model": stored.pop("embed_model", ""), "base_url": ""}
    }
    stored["provider"] = "gemini"
    stored["version"] = VERSION
    return stored


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
            stored = _migrate(json.loads(dpapi.unprotect(self.path.read_bytes()).decode("utf-8")))
            data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except Exception as e:  # noqa: BLE001 - keep running, but say exactly what happened
            backup = self.path.with_suffix(".unreadable")
            os.replace(self.path, backup)
            self.load_error = f"Saved settings could not be decrypted ({e}); they were moved to {backup.name}."
            log.error(self.load_error)
        if data.get("provider") not in PRESETS:
            data["provider"] = "gemini"
        return data

    def _save(self) -> None:
        raw = dpapi.protect(json.dumps(self._data).encode("utf-8"))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, self.path)

    # ---------- profile ----------

    def get(self, key: str) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, DEFAULTS.get(key)))

    def update_profile(self, values: dict[str, Any]) -> None:
        with self._lock:
            for k, v in values.items():
                if k in PROFILE_FIELDS:
                    self._data[k] = v
            self._save()

    # ---------- providers ----------

    @property
    def provider(self) -> str:
        with self._lock:
            return self._data["provider"]

    def set_provider(self, provider_id: str) -> None:
        if provider_id not in PRESETS:
            raise ValueError(f"Unknown provider '{provider_id}'.")
        with self._lock:
            self._data["provider"] = provider_id
            self._save()

    def provider_cfg(self, provider_id: str | None = None) -> dict[str, str]:
        with self._lock:
            pid = provider_id or self._data["provider"]
            cfg = {"chat_model": "", "embed_model": "", "base_url": ""}
            cfg.update(self._data["provider_config"].get(pid, {}))
            if not cfg["base_url"]:
                cfg["base_url"] = PRESETS[pid]["base_url"]
            return cfg

    def set_provider_cfg(self, provider_id: str, **fields: str) -> None:
        with self._lock:
            cfg = self._data["provider_config"].setdefault(provider_id, {})
            for k, v in fields.items():
                if k in PROVIDER_FIELDS:
                    cfg[k] = v
            self._save()

    def api_key(self, provider_id: str | None = None) -> str:
        with self._lock:
            return self._data["secrets"].get(f"api_key:{provider_id or self._data['provider']}", "")

    def set_api_key(self, provider_id: str, value: str) -> None:
        with self._lock:
            name = f"api_key:{provider_id}"
            if value:
                self._data["secrets"][name] = value
            else:
                self._data["secrets"].pop(name, None)
            self._save()

    def public(self) -> dict[str, Any]:
        pid = self.provider
        cfg = self.provider_cfg(pid)
        key = self.api_key(pid)
        with self._lock:
            keyed = sorted(k.split(":", 1)[1] for k in self._data["secrets"] if k.startswith("api_key:"))
            profile = {k: self._data.get(k) for k in PROFILE_FIELDS}
        return {
            **profile,
            "provider": pid,
            "base_url": cfg["base_url"],
            "chat_model": cfg["chat_model"],
            "embed_model": cfg["embed_model"],
            "has_api_key": bool(key),
            "api_key_hint": f"…{key[-4:]}" if len(key) >= 8 else "",
            "providers_with_keys": keyed,
            "load_error": self.load_error,
        }
