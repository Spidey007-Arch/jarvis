"""Protective initiative: watch the PC and speak up when something needs the user's attention.

Readings come from the desktop agent's heartbeats. Alerts are spoken aloud (no
notifications) and kept for the briefing. Each kind of alert has its own cooldown so JARVIS
mentions a problem once, not every few seconds.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from .bridge import AgentBridge, AgentError, AgentUnavailable
from .settings import SettingsStore

log = logging.getLogger("jarvis.monitor")

CHECK_EVERY_S = 30
SUSTAINED_CHECKS = 6  # 6 x 30 s = 3 minutes of continuous overload before speaking up
COOLDOWN_S = {"battery": 30 * 60, "disk": 24 * 3600, "cpu": 45 * 60, "ram": 45 * 60}


class Monitor:
    def __init__(self, bridge: AgentBridge, settings: SettingsStore) -> None:
        self.bridge = bridge
        self.settings = settings
        self.alerts: deque[dict[str, Any]] = deque(maxlen=50)
        self._last: dict[str, float] = {}
        self._streak = {"cpu": 0, "ram": 0}

    def recent(self, since: float = 0.0) -> list[dict[str, Any]]:
        return [a for a in self.alerts if a["at"] > since]

    def evaluate(self, stats: dict[str, Any], now: float | None = None) -> list[tuple[str, str]]:
        """Pure decision step (testable): returns [(kind, message)] that should fire now."""
        now = now or time.time()
        addr = (self.settings.get("address") or "sir").strip()
        out: list[tuple[str, str]] = []

        bat, plugged = stats.get("battery_percent"), stats.get("battery_plugged")
        if bat is not None and plugged is False and bat <= 15:
            out.append(("battery", f"Battery at {bat}% and not charging, {addr}. I'd suggest plugging in before it decides for us."))

        disk = stats.get("disk_percent")
        if disk is not None and disk >= 95:
            out.append(("disk", f"The system drive is {disk:.0f}% full, {addr}. Windows grows temperamental past this point; shall I help clear some space?"))

        for kind, key, label in (("cpu", "cpu_percent", "The processor"), ("ram", "ram_percent", "Memory")):
            v = stats.get(key)
            self._streak[kind] = self._streak[kind] + 1 if v is not None and v >= 95 else 0
            if self._streak[kind] >= SUSTAINED_CHECKS:
                out.append((kind, f"{label} has been above 95% for several minutes, {addr}. Ask me for a status report and I'll find the culprit."))

        fire = [(k, m) for k, m in out if k not in self._last or now - self._last[k] >= COOLDOWN_S[k]]
        for k, _ in fire:
            self._last[k] = now
        return fire

    async def run(self) -> None:
        while True:
            await asyncio.sleep(CHECK_EVERY_S)
            if not self.settings.get("proactive_alerts") or not self.bridge.connected:
                continue
            for kind, message in self.evaluate(self.bridge.stats or {}):
                self.alerts.append({"at": time.time(), "kind": kind, "message": message})
                log.info("proactive alert", extra={"fields": {"kind": kind}})
                try:
                    await self.bridge.call("speak", {"text": message, "queue": True}, timeout=20)
                except (AgentUnavailable, AgentError) as e:
                    log.warning("could not speak alert: %s", e)
