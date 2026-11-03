"""Automations: jobs JARVIS runs on its own on a schedule ("every weekday at 9, go through my unread mail and tell me
what matters"). Each run is an ordinary request through the agent, saved in that automation's own conversation, and
its result can be spoken aloud.

Schedules: once at a date-time, daily at a time (optionally only on some weekdays), or every N minutes. A run that
came due while JARVIS was off is skipped rather than run late, unless it is less than LATE_OK_S overdue. Approvals
work as in chat: a step that needs a yes waits for one in the app and is declined after 2 minutes."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from .db import DB

log = logging.getLogger("jarvis.automations")

SCHEMA = """
CREATE TABLE IF NOT EXISTS automations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    at          TEXT NOT NULL DEFAULT '',
    days        TEXT NOT NULL DEFAULT '[]',
    every_min   INTEGER NOT NULL DEFAULT 0,
    deliver     TEXT NOT NULL DEFAULT '[]',
    enabled     INTEGER NOT NULL DEFAULT 1,
    conv_id     TEXT,
    next_run    REAL,
    last_run    REAL,
    last_status TEXT NOT NULL DEFAULT '',
    last_result TEXT NOT NULL DEFAULT '',
    runs        INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
)
"""
KINDS = ("once", "daily", "interval")
DELIVER = ("speak",)  # every run is also kept in its conversation
MIN_EVERY = 15  # minutes: more often than this would burn through the AI quota
LATE_OK_S = 30 * 60
CHECK_S = 20
MAX = 50
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class AutomationError(Exception):
    pass


def _hm(at: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in at.strip().split(":")[:2])
    except ValueError as e:
        raise AutomationError(f"'{at}' is not a time; use HH:MM, e.g. 09:00.") from e
    if not (0 <= h < 24 and 0 <= m < 60):
        raise AutomationError(f"'{at}' is not a time of day.")
    return h, m


def next_run(kind: str, at: str, days: list[int], every_min: int, after: float) -> float | None:
    """When a schedule next fires, strictly after `after` (local time). None: a one-off that has passed."""
    now = datetime.fromtimestamp(after)
    if kind == "once":
        try:
            when = datetime.fromisoformat(at.strip())
        except ValueError as e:
            raise AutomationError(f"'{at}' is not a date-time; use YYYY-MM-DDTHH:MM.") from e
        return when.timestamp() if when.timestamp() > after else None
    if kind == "daily":
        h, m = _hm(at)
        allowed = set(days) or set(range(7))
        day = now.replace(hour=h, minute=m, second=0, microsecond=0)
        for _ in range(8):
            if day > now and day.weekday() in allowed:
                return day.timestamp()
            day += timedelta(days=1)
        raise AutomationError("No day of the week is chosen.")
    if kind == "interval":
        return after + max(MIN_EVERY, every_min) * 60
    raise AutomationError(f"Unknown schedule '{kind}'.")


def describe(a: dict[str, Any]) -> str:
    """The schedule in words, for the list and for JARVIS."""
    if a["kind"] == "once":
        return f"once, {a['at'].replace('T', ' ')}"
    if a["kind"] == "interval":
        mins = a["every_min"]
        return f"every {mins // 60} hours" if mins % 60 == 0 and mins >= 120 else ("every hour" if mins == 60 else f"every {mins} minutes")
    days = a["days"]
    when = "every day" if not days or len(days) == 7 else \
        "weekdays" if sorted(days) == [0, 1, 2, 3, 4] else "weekends" if sorted(days) == [5, 6] else ", ".join(DAY_NAMES[d] for d in sorted(days))
    return f"{when} at {a['at']}"


# A run: (automation) -> {"status": "done" | "failed", "result": text, "conversation_id": id}
Runner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class Automations:
    def __init__(self, db: DB) -> None:
        self.db = db
        db.run(SCHEMA)
        self.runner: Runner | None = None  # set by the router: runs one as a request through the agent
        self.running: set[str] = set()

    def _row(self, r: dict[str, Any]) -> dict[str, Any]:
        a = {**r, "days": json.loads(r["days"] or "[]"), "enabled": bool(r["enabled"]),
             "deliver": [d for d in json.loads(r["deliver"] or "[]") if d in DELIVER]}  # drops ways that no longer exist
        a["schedule"] = describe(a)
        a["running"] = a["id"] in self.running
        return a

    def list(self) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.db.all("SELECT * FROM automations ORDER BY created_at")]

    def get(self, aid: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM automations WHERE id=?", [aid])
        return self._row(r) if r else None

    def save(self, name: str, prompt: str, kind: str, at: str = "", days: list[int] | None = None, every_min: int = 0,
             deliver: list[str] | None = None, enabled: bool = True, aid: str | None = None) -> dict[str, Any]:
        name, prompt, kind = name.strip()[:80], prompt.strip()[:4000], kind.strip().lower()
        if not name or not prompt:
            raise AutomationError("An automation needs a name and what JARVIS should do.")
        if kind not in KINDS:
            raise AutomationError(f"The schedule must be one of: {', '.join(KINDS)}.")
        days = sorted({int(d) for d in (days or []) if 0 <= int(d) <= 6})
        every_min = int(every_min or 0)
        if kind == "interval" and every_min < MIN_EVERY:
            raise AutomationError(f"Every {MIN_EVERY} minutes is the most often an automation can run.")
        deliver = [d for d in (deliver or []) if d in DELIVER]
        upcoming = next_run(kind, at, days, every_min, time.time())
        if kind == "once" and upcoming is None:
            raise AutomationError("That time has already passed.")
        if aid:
            if not self.db.run("UPDATE automations SET name=?, prompt=?, kind=?, at=?, days=?, every_min=?, deliver=?, enabled=?, next_run=? "
                               "WHERE id=?", [name, prompt, kind, at.strip(), json.dumps(days), every_min, json.dumps(deliver),
                                              int(enabled), upcoming, aid]):
                raise KeyError(aid)
        else:
            if (self.db.one("SELECT COUNT(*) AS n FROM automations") or {}).get("n", 0) >= MAX:
                raise AutomationError(f"There are already {MAX} automations; delete one first.")
            aid = uuid.uuid4().hex[:10]
            self.db.run("INSERT INTO automations (id, name, prompt, kind, at, days, every_min, deliver, enabled, next_run, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", [aid, name, prompt, kind, at.strip(), json.dumps(days), every_min,
                                                          json.dumps(deliver), int(enabled), upcoming, time.time()])
        return self.get(aid)  # type: ignore[return-value]

    def set_enabled(self, aid: str, enabled: bool) -> dict[str, Any]:
        a = self.get(aid)
        if not a:
            raise KeyError(aid)
        upcoming = next_run(a["kind"], a["at"], a["days"], a["every_min"], time.time()) if enabled else a["next_run"]
        if enabled and upcoming is None:
            raise AutomationError("That one-off time has passed; give it a new time.")
        self.db.run("UPDATE automations SET enabled=?, next_run=? WHERE id=?", [int(enabled), upcoming, aid])
        return self.get(aid)  # type: ignore[return-value]

    def delete(self, aid: str) -> bool:
        return self.db.run("DELETE FROM automations WHERE id=?", [aid]) > 0

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        """Enabled automations whose time has come. Ones far overdue (JARVIS was off) are moved on, not run late."""
        now = now or time.time()
        out = []
        for r in self.db.all("SELECT * FROM automations WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?", [now]):
            a = self._row(r)
            if a["id"] in self.running:
                continue
            if now - a["next_run"] > LATE_OK_S:
                upcoming = next_run(a["kind"], a["at"], a["days"], a["every_min"], now)
                self.db.run("UPDATE automations SET next_run=?, enabled=?, last_status='missed' WHERE id=?",
                            [upcoming, int(upcoming is not None), a["id"]])
                log.info("automation missed while off", extra={"fields": {"name": a["name"]}})
                continue
            out.append(a)
        return out

    async def run_one(self, a: dict[str, Any], scheduled: bool = True) -> dict[str, Any]:
        if not self.runner:
            raise AutomationError("Automations aren't ready yet.")
        if a["id"] in self.running:
            raise AutomationError(f"“{a['name']}” is already running.")
        self.running.add(a["id"])
        started = time.time()
        if scheduled:  # move the schedule on first, so a slow or failing run is never started twice
            upcoming = next_run(a["kind"], a["at"], a["days"], a["every_min"], max(started, a["next_run"] or started))
            self.db.run("UPDATE automations SET next_run=?, enabled=? WHERE id=?", [upcoming, int(upcoming is not None), a["id"]])
        try:
            out = await self.runner(a)
        except Exception as e:  # noqa: BLE001 - one broken run must not stop the others
            log.exception("automation failed")
            out = {"status": "failed", "result": str(e)}
        finally:
            self.running.discard(a["id"])
        self.db.run("UPDATE automations SET last_run=?, last_status=?, last_result=?, runs=runs+1, conv_id=COALESCE(?, conv_id) WHERE id=?",
                    [started, out.get("status", "done"), str(out.get("result", ""))[:2000], out.get("conversation_id"), a["id"]])
        log.info("automation ran", extra={"fields": {"name": a["name"], "status": out.get("status"), "s": round(time.time() - started, 1)}})
        return out

    async def run(self) -> None:
        """The scheduler: one at a time, so two jobs never fight over the screen."""
        while True:
            try:
                for a in self.due():
                    await self.run_one(a)
            except Exception:  # noqa: BLE001
                log.exception("automation scheduler pass failed")
            await asyncio.sleep(CHECK_S)
