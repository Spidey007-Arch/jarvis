"""Reminders, alarms and timers: stored in SQLite, spoken aloud by the desktop agent (no notifications).

They survive restarts. Anything that came due while JARVIS was not running is not rung late: it is
mentioned once, all together, as missed."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from .bridge import AgentBridge, AgentError, AgentUnavailable
from .db import DB

log = logging.getLogger("jarvis.reminders")

MISSED_S = 300  # due more than this long ago when first seen: JARVIS was off, so it is reported as missed
KINDS = ("reminder", "alarm", "timer")


class ReminderError(Exception):
    pass


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    parts = [f"{h} hour{'s' * (h != 1)}" if h else "", f"{m} minute{'s' * (m != 1)}" if m else "", f"{s} second{'s' * (s != 1)}" if s else ""]
    return " ".join(p for p in parts if p) or "0 seconds"


def spoken(r: dict[str, Any]) -> str:
    """What JARVIS says when one comes due."""
    kind = r.get("kind") or "reminder"
    if kind == "timer":
        return f"Your {r['text']} timer is done." if r["text"] else "Your timer is done."
    if kind == "alarm":
        line = f"It's {_clock(r['due_at'])}." + (f" {r['text']}." if r["text"] else "")
        return f"Alarm. {line} Again: {line}"
    return f"Just a reminder: {r['text']}"


class Reminders:
    def __init__(self, db: DB, bridge: AgentBridge) -> None:
        self.db = db
        self.bridge = bridge

    def _insert(self, text: str, ts: float, kind: str) -> str:
        rid = uuid.uuid4().hex[:8]
        self.db.run("INSERT INTO reminders (id, text, due_at, created_at, status, kind) VALUES (?,?,?,?, 'pending', ?)",
                    [rid, text, ts, time.time(), kind])
        return rid

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
        rid = self._insert(text, ts, "reminder")
        return {"id": rid, "text": text, "due": datetime.fromtimestamp(ts).strftime("%A %d %B %Y at %H:%M")}

    def add_alarm(self, at: str, label: str = "") -> dict[str, Any]:
        """at: a time today (HH:MM, rolled to tomorrow if it has passed) or a full ISO date-time."""
        at = (at or "").strip()
        now = datetime.now()
        try:
            if len(at) <= 5 and ":" in at:
                hh, mm = (int(v) for v in at.split(":"))
                when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if when <= now:
                    when += timedelta(days=1)
            else:
                when = datetime.fromisoformat(at)
        except ValueError as e:
            raise ReminderError(f"'{at}' is not a time like 07:30 or a date-time like 2026-10-22T07:30.") from e
        ts = when.timestamp() if when.tzinfo else when.astimezone().timestamp()
        if ts < time.time() - 30:
            raise ReminderError(f"{when:%A %d %B at %H:%M} is in the past.")
        rid = self._insert((label or "").strip(), ts, "alarm")
        return {"id": rid, "alarm": datetime.fromtimestamp(ts).strftime("%A %d %B at %H:%M"), "label": label}

    def add_timer(self, seconds: float, label: str = "") -> dict[str, Any]:
        if not 1 <= seconds <= 7 * 86400:
            raise ReminderError("A timer must run between one second and a week.")
        label = (label or "").strip() or _duration(seconds)
        rid = self._insert(label, time.time() + seconds, "timer")
        return {"id": rid, "timer": _duration(seconds), "label": label, "rings_at": _clock(time.time() + seconds)}

    def pending(self) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM reminders WHERE status='pending' ORDER BY due_at")
        out = []
        for r in rows:
            item = {"id": r["id"], "kind": r.get("kind") or "reminder", "text": r["text"],
                    "due": datetime.fromtimestamp(r["due_at"]).strftime("%a %d %b %H:%M")}
            if item["kind"] == "timer":
                item["left"] = _duration(max(0.0, r["due_at"] - time.time()))
            out.append(item)
        return out

    def cancel(self, rid: str) -> dict[str, Any]:
        if self.db.run("UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'", [rid]) == 0:
            raise ReminderError(f"No pending reminder, alarm or timer has id '{rid}'.")
        return {"cancelled": rid}

    def overdue(self, now: float | None = None) -> list[dict[str, Any]]:
        """Pending ones that came due while JARVIS was off (or couldn't speak), still pending until they are mentioned."""
        now = time.time() if now is None else now
        return self.db.all("SELECT * FROM reminders WHERE status='pending' AND due_at <= ?", [now - MISSED_S])

    def mark_missed(self, rows: list[dict[str, Any]], now: float | None = None) -> None:
        now = time.time() if now is None else now
        for r in rows:
            self.db.run("UPDATE reminders SET status='missed', fired_at=? WHERE id=? AND status='pending'", [now, r["id"]])

    async def run(self) -> None:
        """Deliver due reminders. If the agent is offline or cannot speak yet, keep them pending and retry."""
        while True:
            if not getattr(self.bridge, "connected", True):  # on a cold start the voice isn't there yet: nothing can be said
                await asyncio.sleep(2)
                continue
            # Missed ones are marked only once the line was actually spoken.
            missed = self.overdue()
            if missed:
                items = "; ".join(f"{'your ' + (r.get('kind') or 'reminder')} at {_clock(r['due_at'])}" + (f", {r['text']}" if r["text"] else "")
                                  for r in missed[:5])
                line = (f"While I was off, you missed {len(missed)} " + ("thing" if len(missed) == 1 else "things") + f": {items}.")
                try:
                    await self.bridge.call("speak", {"text": line, "queue": True}, timeout=20)
                    self.mark_missed(missed)
                except (AgentUnavailable, AgentError) as e:
                    log.warning("could not mention missed reminders yet: %s", e)
                    await asyncio.sleep(10)
                    continue  # keep them for the next pass; don't also try them one by one as if just due
            due = self.db.all("SELECT * FROM reminders WHERE status='pending' AND due_at <= ?", [time.time()])
            for r in due:
                try:
                    await self.bridge.call("speak", {"text": spoken(r), "queue": True}, timeout=20)
                    self.db.run("UPDATE reminders SET status='done', fired_at=? WHERE id=?", [time.time(), r["id"]])
                    log.info("reminder delivered", extra={"fields": {"id": r["id"], "kind": r.get("kind")}})
                except (AgentUnavailable, AgentError) as e:
                    log.warning("reminder %s not delivered yet: %s", r["id"], e)
            await asyncio.sleep(2 if any((r.get("kind") == "timer") for r in self.db.all(
                "SELECT kind FROM reminders WHERE status='pending' AND due_at <= ?", [time.time() + 15])) else 10)
