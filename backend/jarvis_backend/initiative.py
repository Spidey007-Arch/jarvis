"""Initiative: JARVIS notices when the user steps away and comes back, and follows up on a conversation that
was left hanging, the way a person in the room would. Never more than that.

Presence comes from the agent's heartbeat: seconds since the last mouse or keyboard input, whether the PC is
locked, and whether sound is playing (watching a video is not being away). No camera, no screen content.

- Coming back after a real absence gets one short welcome, with anything that happened meanwhile.
- A follow-up needs a reason: the cheap utility model reads the end of a conversation that went quiet and
  either gives one short line worth saying (an unanswered question, something the user meant to do, a
  thread left open) or nothing. At most one per conversation pause and FOLLOWUPS_PER_HOUR an hour; never
  while the user is away, in a live conversation or in the middle of a task.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

log = logging.getLogger("jarvis.initiative")

AWAY_IDLE_S = 300  # no input for this long (and no sound playing): away
WELCOME_AFTER_S = 600  # an absence at least this long gets a welcome back
ACTIVE_IDLE_S = 5  # input within this long: at the PC right now
FOLLOWUP_AFTER_S = 120  # a conversation quiet this long may get a follow-up
FOLLOWUP_WITHIN_S = 15 * 60  # ...but not once it is this old
FOLLOWUPS_PER_HOUR = 3
TICK_S = 5.0

FOLLOWUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "say": {"type": "string", "description": "one short spoken line, or empty when nothing is worth saying"},
        "reason": {"type": "string", "description": "why, in a few words (unanswered question, unfinished plan, ...)"},
    },
    "required": ["say", "reason"],
}

FOLLOWUP_PROMPT = """You are JARVIS, the user's personal assistant on their PC. The conversation below went quiet {minutes} minutes ago and the user is still at the computer.
Decide whether there is a natural, genuinely useful thing to say now, as a friend in the room would: an answer to a question that was left open, a check on something the user said they were about to do, an offer to finish a thread that was cut short.
If there is nothing like that, or the conversation simply ended, reply with "say" empty. Never repeat what was already said, never ask whether they need anything else, never just make small talk.
If there is, "say" is one short spoken sentence (no lists, no markdown), addressed to the user as "{address}".

Conversation (oldest first):
{lines}"""


class Initiative:
    def __init__(self, *, settings: Any, bridge: Any, db: Any, llm: Any, say: Callable[[str], Awaitable[None]],
                 busy: Callable[[], bool], happenings: Callable[[float], list[str]]) -> None:
        self.settings, self.bridge, self.db, self.llm = settings, bridge, db, llm
        self.say = say  # speak a line (skips itself while a live conversation talks)
        self.busy = busy  # a live conversation or a task is running
        self.happenings = happenings  # short notes on what happened since a time (downloads, reminders)
        self.state = "active"
        self.away_since: float | None = None
        self._followed: set[str] = set()  # conversation:last-message ids already followed up
        self._recent: list[float] = []

    # ----- presence -----

    def observe(self, stats: dict[str, Any], now: float | None = None) -> str | None:
        """Update presence from a heartbeat. Returns 'returned' when the user comes back from a long absence."""
        now = time.time() if now is None else now
        idle = stats.get("idle_s")
        if idle is None:
            return None
        away = bool(stats.get("locked")) or (idle >= AWAY_IDLE_S and not stats.get("audio_playing"))
        if away and self.state != "away":
            self.state = "away"
            self.away_since = now - (0 if stats.get("locked") else idle)
            log.info("user away")
            return None
        if not away and self.state == "away" and idle < ACTIVE_IDLE_S:
            gone = now - (self.away_since or now)
            self.state, self.away_since = "active", None
            log.info("user back", extra={"fields": {"away_s": round(gone)}})
            return "returned" if gone >= WELCOME_AFTER_S else None
        if not away:
            self.state = "active" if idle < 60 else "passive"
        return None

    def welcome(self, away_for: float, since: float) -> str:
        address = (self.settings.get("address") or "sir").strip()
        mins = int(away_for // 60)
        span = f"{mins // 60} hour{'s' if mins // 60 != 1 else ''}" if mins >= 90 else f"{mins} minutes"
        line = f"Welcome back, {address}. You were away about {span}."
        notes = self.happenings(since)
        if notes:
            line += " While you were gone: " + "; ".join(notes[:3]) + "."
        return line

    # ----- follow-ups -----

    def _followup_candidate(self, now: float) -> tuple[str, list[dict[str, Any]]] | None:
        conv = self.db.one("SELECT id, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 1")
        if not conv or not FOLLOWUP_AFTER_S <= now - conv["updated_at"] <= FOLLOWUP_WITHIN_S:
            return None
        rows = self.db.all("SELECT id, role, content FROM messages WHERE conversation_id=? AND role IN ('user','model') "
                           "ORDER BY created_at DESC LIMIT 10", [conv["id"]])
        if len(rows) < 2 or not any(r["role"] == "user" for r in rows):
            return None
        key = f"{conv['id']}:{rows[0]['id']}"
        if key in self._followed:
            return None
        self._followed.add(key)
        return conv["id"], list(reversed(rows))

    async def maybe_follow_up(self, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        if not self.settings.get("followups") or self.state != "active" or self.busy():
            return None
        self._recent = [t for t in self._recent if now - t < 3600]
        if len(self._recent) >= FOLLOWUPS_PER_HOUR:
            return None
        cand = self._followup_candidate(now)
        if cand is None:
            return None
        conv_id, rows = cand
        last = self.db.one("SELECT updated_at FROM conversations WHERE id=?", [conv_id])
        lines = "\n".join(f"{'User' if r['role'] == 'user' else 'JARVIS'}: {r['content'][:500]}" for r in rows)
        prompt = FOLLOWUP_PROMPT.format(minutes=int((now - last["updated_at"]) // 60), address=(self.settings.get("address") or "sir").strip(),
                                        lines=lines)
        try:
            out = await self.llm.generate_json(prompt, FOLLOWUP_SCHEMA)
        except Exception as e:  # noqa: BLE001 - a follow-up is optional; never let it break anything
            log.info("no follow-up: %s", e)
            return None
        say = str((out or {}).get("say") or "").strip()
        if not say or self.busy() or self.state != "active":
            return None
        self._recent.append(now)
        t = time.time()
        msg_id = uuid.uuid4().hex
        self._followed.add(f"{conv_id}:{msg_id}")  # never follow up on a follow-up
        self.db.run("INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?,?,?,?,?)",
                    [msg_id, conv_id, "model", say, t])
        self.db.run("UPDATE conversations SET updated_at=? WHERE id=?", [t, conv_id])
        log.info("follow-up", extra={"fields": {"reason": str((out or {}).get("reason") or "")[:80]}})
        self.bridge.voice_event({"type": "voice", "event": "briefing", "text": say})
        await self.say(say)
        return say

    # ----- loop -----

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                if not self.bridge.connected or not self.settings.get("voice_enabled"):
                    continue
                since = self.away_since
                if self.observe(self.bridge.stats or {}) == "returned" and self.settings.get("presence_greetings") and since:
                    if not self.busy():
                        await self.say(self.welcome(time.time() - since, since))
                    continue
                await self.maybe_follow_up()
            except Exception:  # noqa: BLE001 - keep noticing
                log.exception("initiative tick failed")


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")
