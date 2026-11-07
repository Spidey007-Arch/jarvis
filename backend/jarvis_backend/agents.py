"""Always-running agents: a goal JARVIS keeps working on over days or weeks ("watch the price of this laptop", "keep
an eye on this GitHub project and tell me when a new version ships"), each with its own notebook.

An agent wakes every so often. Each wake is one request through the same agent as the chat box, given its goal and
what it wrote in its notebook last time, so it carries on where it stopped even after a restart. It finishes with
NOTES: and whatever it wants to remember, and with ALL_CLEAR if there is nothing to tell the user. A quiet wake leaves
no messages behind. Approvals work as in automations: a step that needs a yes waits in the app, then is declined.

Nothing runs until the user creates an agent, because every wake is a full AI request."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable

from .db import DB
from .heartbeat import CLEAR, is_clear

log = logging.getLogger("jarvis.agents")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    goal       TEXT NOT NULL,
    every_min  INTEGER NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    notebook   TEXT NOT NULL DEFAULT '',
    conv_id    TEXT,
    last_run   REAL,
    last_text  TEXT NOT NULL DEFAULT '',
    last_status TEXT NOT NULL DEFAULT '',
    wakes      INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
)
"""
MIN_EVERY = 15  # minutes: each wake is a full AI request
DEFAULT_EVERY = 60
MAX = 20
NOTEBOOK_CHARS = 4000
TICK_S = 30
FAILS_BEFORE_PAUSE = 5

PROMPT = """[Agent “{name}”, an always-running job. The user isn't watching. This is wake number {wake}; you last woke {ago}.]
Your goal: {goal}

Your notebook from last time:
{notebook}

Carry on from the notebook: do the next useful step toward the goal, and don't repeat work it says is done.
End your reply with a line starting NOTES: followed by everything you'll need next time (what you've done, what you
found, what to check next). Before that line, say anything the user needs to know in a few plain sentences, or write
only {clear} if there's nothing to tell them."""


class AgentJobError(Exception):
    pass


def split_notes(reply: str) -> tuple[str, str | None]:
    """(what to tell the user, the new notebook) from a reply; no NOTES: line keeps the old notebook."""
    m = re.search(r"^\s*\**NOTES:\**", reply, re.M)
    if not m:
        return reply.strip(), None
    return reply[:m.start()].strip(), reply[m.end():].strip()[-NOTEBOOK_CHARS:]


class Agents:
    def __init__(self, db: DB, busy: Callable[[], bool] = lambda: False) -> None:
        self.db = db
        self.busy = busy
        self.runner: Callable[[str, str | None], Awaitable[dict[str, Any]]] | None = None  # set by the router
        self.say: Callable[[str], Awaitable[None]] | None = None  # set by the router
        self._fails: dict[str, int] = {}
        self._running: set[str] = set()
        db.run(SCHEMA)

    def _view(self, r: dict[str, Any]) -> dict[str, Any]:
        return {**r, "enabled": bool(r["enabled"]), "running": r["id"] in self._running}

    def list(self) -> list[dict[str, Any]]:
        return [self._view(r) for r in self.db.all("SELECT * FROM agents ORDER BY created_at")]

    def get(self, aid: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM agents WHERE id=?", [aid])
        return self._view(r) if r else None

    def save(self, name: str, goal: str, every_min: int = DEFAULT_EVERY, aid: str | None = None) -> dict[str, Any]:
        name, goal = name.strip()[:60], goal.strip()[:2000]
        if not name or not goal:
            raise AgentJobError("An agent needs a name and a goal.")
        if every_min < MIN_EVERY:
            raise AgentJobError(f"An agent can wake at most every {MIN_EVERY} minutes.")
        if aid:
            if not self.get(aid):
                raise AgentJobError("There's no agent with that id.")
            self.db.run("UPDATE agents SET name=?, goal=?, every_min=? WHERE id=?", [name, goal, every_min, aid])
            return self.get(aid) or {}
        if len(self.db.all("SELECT id FROM agents")) >= MAX:
            raise AgentJobError(f"{MAX} agents is the limit; delete one first.")
        aid = uuid.uuid4().hex[:12]
        self.db.run("INSERT INTO agents (id, name, goal, every_min, created_at) VALUES (?,?,?,?,?)", [aid, name, goal, every_min, time.time()])
        return self.get(aid) or {}

    def set_enabled(self, aid: str, on: bool) -> bool:
        self._fails.pop(aid, None)
        return self.db.run("UPDATE agents SET enabled=? WHERE id=?", [int(on), aid]) > 0

    def delete(self, aid: str) -> bool:
        if not self.get(aid):
            return False
        self.db.run("DELETE FROM agents WHERE id=?", [aid])
        return True

    def forget_notes(self, aid: str) -> None:
        self.db.run("UPDATE agents SET notebook='', wakes=0 WHERE id=?", [aid])

    def prompt(self, a: dict[str, Any], now: float) -> str:
        ago = f"{round((now - a['last_run']) / 60)} minutes ago" if a.get("last_run") else "never"
        return PROMPT.format(name=a["name"], wake=a["wakes"] + 1, ago=ago, goal=a["goal"], notebook=a["notebook"] or "(empty: this is your first wake)", clear=CLEAR)

    def due(self, now: float) -> list[dict[str, Any]]:
        return [a for a in self.list() if a["enabled"] and not a["running"] and now - (a["last_run"] or a["created_at"]) >= a["every_min"] * 60]

    async def wake(self, aid: str) -> dict[str, Any]:
        a = self.get(aid)
        if not a or not self.runner:
            raise AgentJobError("There's no agent with that id.")
        now = time.time()
        self._running.add(aid)
        try:
            out = await self.runner(self.prompt(a, now), a.get("conv_id"))
        finally:
            self._running.discard(aid)
        conv, reply = out.get("conversation_id"), str(out.get("result") or "")
        if conv:
            self.db.run("UPDATE conversations SET title=? WHERE id=?", [f"◉ {a['name']}"[:60], conv])
        if out.get("status") == "failed":
            n = self._fails[aid] = self._fails.get(aid, 0) + 1
            if n >= FAILS_BEFORE_PAUSE:  # don't keep spending requests on something that can't work
                self.set_enabled(aid, False)
            self.db.run("UPDATE agents SET last_run=?, last_status='failed', last_text=?, conv_id=? WHERE id=?", [now, reply[:300], conv or a.get("conv_id"), aid])
            return {"status": "failed", "text": reply[:300]}
        self._fails.pop(aid, None)
        tell, notes = split_notes(reply)
        quiet = not tell or is_clear(tell)
        if quiet and conv:
            self.db.run("DELETE FROM messages WHERE conversation_id=? AND created_at>=?", [conv, now - 1])
        status = "quiet" if quiet else "told"
        self.db.run("UPDATE agents SET last_run=?, last_status=?, last_text=?, conv_id=?, wakes=wakes+1, notebook=? WHERE id=?",
                    [now, status, "" if quiet else tell[:300], conv or a.get("conv_id"), a["notebook"] if notes is None else notes, aid])
        if not quiet and self.say:
            await self.say(f"{a['name']}: {tell[:600]}")
        log.info("agent woke", extra={"fields": {"agent": a["name"], "status": status, "s": round(time.time() - now, 1)}})
        return {"status": status, "text": tell[:300]}

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                if self.busy() or not self.runner:
                    continue
                for a in self.due(time.time())[:1]:  # one at a time: they share the user's AI quota
                    await self.wake(a["id"])
            except Exception:  # noqa: BLE001 - keep running
                log.exception("agent wake failed")
