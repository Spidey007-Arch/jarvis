"""Reminders: stored in SQLite, delivered as Windows notifications by the desktop agent."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Any

from .bridge import AgentBridge, AgentError, AgentUnavailable
from .db import DB

log = logging.getLogger("jarvis.reminders")


class ReminderError(Exception):
    pass


class Reminders:
    def __init__(self, db: DB, bridge: AgentBridge) -> None:
        self.db = db
        self.bridge = bridge

    def add(self, text: str, due: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ReminderError("The reminder needs some text.")
        try:
            when = datetime.fromisoformat(due.strip())
        except (ValueError, AttributeError) as e:
            raise ReminderError(f"'{due}' is not a date-time like 2026-10-22T18:30.") from e
        ts = when.timestamp() if when.tzinfo else when.astimezone().timestamp()
        if ts < time.time() - 30:
            raise ReminderError(f"{when:%A %d %B at %H:%M} is in the past.")
        rid = uuid.uuid4().hex[:8]
        self.db.run("INSERT INTO reminders (id, text, due_at, created_at, status) VALUES (?,?,?,?, 'pending')",
                    [rid, text, ts, time.time()])
        return {"id": rid, "text": text, "due": datetime.fromtimestamp(ts).strftime("%A %d %B %Y at %H:%M")}

    def pending(self) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM reminders WHERE status='pending' ORDER BY due_at")
        return [{"id": r["id"], "text": r["text"], "due": datetime.fromtimestamp(r["due_at"]).strftime("%a %d %b %H:%M")} for r in rows]

    def cancel(self, rid: str) -> dict[str, Any]:
        if self.db.run("UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'", [rid]) == 0:
            raise ReminderError(f"No pending reminder has id '{rid}'.")
        return {"cancelled": rid}

    async def run(self) -> None:
        """Deliver due reminders. If the agent is offline, keep them pending and retry."""
        while True:
            due = self.db.all("SELECT * FROM reminders WHERE status='pending' AND due_at <= ?", [time.time()])
            for r in due:
                try:
                    await self.bridge.call("notify", {"title": "JARVIS reminder", "body": r["text"]}, timeout=20)
                    self.db.run("UPDATE reminders SET status='done', fired_at=? WHERE id=?", [time.time(), r["id"]])
                    log.info("reminder delivered", extra={"fields": {"id": r["id"]}})
                except (AgentUnavailable, AgentError) as e:
                    log.warning("reminder %s not delivered yet: %s", r["id"], e)
            await asyncio.sleep(10)
