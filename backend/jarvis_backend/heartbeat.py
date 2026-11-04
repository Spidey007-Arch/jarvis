"""Heartbeat: every so often JARVIS goes through a short list of things the user cares about ("anything urgent in
my mail?", "is drive C nearly full?", "a meeting in the next 30 minutes?") and speaks up only when one needs them.

Each beat is one request through the same agent as the chat box, told to only look, never change anything, and to
answer ALL_CLEAR when nothing needs the user. A quiet beat leaves nothing behind; one that finds something is
said aloud and kept in the Heartbeat conversation. What was already said isn't said again unless it changed.
Beats pause while the user is away, during quiet hours, and while a task or a live conversation is running."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

log = logging.getLogger("jarvis.heartbeat")

MIN_EVERY = 15  # minutes: a beat is a full AI request
DEFAULT_EVERY = 30
MAX_CHECKS = 12
REMEMBER_TOLD_S = 6 * 3600
TICK_S = 30
CLEAR = "ALL_CLEAR"
EXAMPLES = ["Anything in my unread Gmail that looks urgent or is from my boss",
            "A calendar event starting in the next 30 minutes",
            "Any drive more than 90% full, or the battery under 20% and not charging"]

PROMPT = """[Heartbeat: JARVIS's own check, every {every} minutes. The user didn't ask anything just now and isn't watching.
Look at each item below with your tools. Only read and look: never send, buy, delete, close or change anything.
{items}
{told}If nothing needs the user's attention right now, reply with exactly {clear} and nothing else.
Otherwise reply with one or two short spoken sentences about only what needs them, most urgent first.]"""


def config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """The saved heartbeat settings, cleaned up and with defaults."""
    raw = raw or {}
    checks = [str(c).strip()[:200] for c in raw.get("checks") or [] if str(c).strip()][:MAX_CHECKS]
    return {"enabled": bool(raw.get("enabled", True)), "every_min": max(MIN_EVERY, int(raw.get("every_min") or DEFAULT_EVERY)),
            "checks": checks, "quiet_from": str(raw.get("quiet_from") or "23:00")[:5], "quiet_to": str(raw.get("quiet_to") or "07:00")[:5]}


def _minutes(hm: str) -> int:
    try:
        h, m = (int(x) for x in hm.split(":")[:2])
        return h * 60 + m
    except ValueError:
        return -1


def quiet(cfg: dict[str, Any], now: datetime) -> bool:
    """Inside the quiet hours (which may run past midnight)."""
    a, b, t = _minutes(cfg["quiet_from"]), _minutes(cfg["quiet_to"]), now.hour * 60 + now.minute
    if a < 0 or b < 0 or a == b:
        return False
    return a <= t < b if a < b else t >= a or t < b


def is_clear(reply: str) -> bool:
    text = reply.strip().strip(".!").upper()
    return not text or text == CLEAR or text.startswith(CLEAR) and len(text) < len(CLEAR) + 20


class Heartbeat:
    def __init__(self, settings: Any, db: Any, *, away: Callable[[], bool], busy: Callable[[], bool]) -> None:
        self.settings, self.db = settings, db
        self.away, self.busy = away, busy
        self.runner: Callable[[str, str | None], Awaitable[dict[str, Any]]] | None = None  # set by the router: run_message
        self.say: Callable[[str], Awaitable[None]] | None = None
        self.told: list[tuple[float, str]] = []
        self.last_beat = 0.0
        self.last: dict[str, Any] = {}
        self.running = False

    def cfg(self) -> dict[str, Any]:
        return config(self.settings.get("heartbeat"))

    def status(self) -> dict[str, Any]:
        c = self.cfg()
        return {**c, "running": self.running, "last_beat": self.last_beat or None, "last": self.last,
                "next_beat": (self.last_beat + c["every_min"] * 60) if self.last_beat and c["enabled"] and c["checks"] else None,
                "told": [{"at": at, "text": text} for at, text in reversed(self.told)], "examples": EXAMPLES}

    def why_not(self, now: float | None = None) -> str | None:
        """Why a beat wouldn't run now, or None when it would."""
        now = now or time.time()
        c = self.cfg()
        if not c["enabled"]:
            return "off"
        if not c["checks"]:
            return "nothing to check"
        if now - self.last_beat < c["every_min"] * 60:
            return "not yet"
        if quiet(c, datetime.fromtimestamp(now)):
            return "quiet hours"
        if self.away():
            return "you're away"
        if self.busy() or self.running:
            return "busy"
        return None

    def prompt(self, c: dict[str, Any], now: float) -> str:
        self.told = [(at, text) for at, text in self.told if now - at < REMEMBER_TOLD_S]
        told = ""
        if self.told:
            told = "Already told the user (don't say these again unless something changed or got more urgent):\n" + \
                   "\n".join(f"- {datetime.fromtimestamp(at):%H:%M}: {text}" for at, text in self.told[-6:]) + "\n"
        return PROMPT.format(every=c["every_min"], items="\n".join(f"- {x}" for x in c["checks"]), told=told, clear=CLEAR)

    def _conversation(self) -> str | None:
        row = self.db.one("SELECT value FROM meta WHERE key='heartbeat_conv'")
        return row["value"] if row else None

    async def beat(self) -> dict[str, Any]:
        if not self.runner:
            raise RuntimeError("The heartbeat isn't ready yet.")
        c, now = self.cfg(), time.time()
        if not c["checks"]:
            return {"status": "skipped", "result": "There's nothing on the list to check."}
        self.running, self.last_beat = True, now
        try:
            out = await self.runner(self.prompt(c, now), self._conversation())
        finally:
            self.running = False
        conv, reply = out.get("conversation_id"), str(out.get("result") or "")
        if conv:
            self.db.run("INSERT OR REPLACE INTO meta (key, value) VALUES ('heartbeat_conv', ?)", [conv])
            self.db.run("UPDATE conversations SET title=? WHERE id=?", ["♥ Heartbeat", conv])
        if out.get("status") == "failed":
            self.last = {"at": now, "status": "failed", "text": reply[:300]}
        elif is_clear(reply):
            if conv:  # a quiet beat leaves nothing behind
                self.db.run("DELETE FROM messages WHERE conversation_id=? AND created_at>=?", [conv, now - 1])
            self.last = {"at": now, "status": "clear", "text": "All clear."}
        else:
            self.told.append((now, reply[:300]))
            self.last = {"at": now, "status": "alert", "text": reply[:300]}
            if self.say:
                await self.say(reply[:600])
        log.info("heartbeat", extra={"fields": {"status": self.last["status"], "s": round(time.time() - now, 1)}})
        return self.last

    async def run(self) -> None:
        self.last_beat = time.time() - self.cfg()["every_min"] * 60 + 120  # first beat two minutes after start
        while True:
            await asyncio.sleep(TICK_S)
            try:
                if self.why_not() is None:
                    await self.beat()
            except Exception:  # noqa: BLE001 - keep beating
                log.exception("heartbeat failed")
