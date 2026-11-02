"""Learned workflows: multi-step jobs that worked, remembered so the next similar request goes faster.

When a request takes several successful steps, the steps (tool, a short description, the arguments) are
saved against the request. A later request that reads alike gets the best match handed to JARVIS as "this
worked before" guidance, never run blindly, since files, windows and pages change. Every reuse is scored
by how it went; a workflow that keeps failing is no longer offered."""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from .db import DB

MIN_STEPS = 3  # fewer successful steps is not a workflow worth remembering
MATCH_MIN = 0.45  # word overlap needed to offer a workflow for a request
KEEP = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id         TEXT PRIMARY KEY,
    goal       TEXT NOT NULL,
    steps      TEXT NOT NULL,
    successes  INTEGER NOT NULL DEFAULT 1,
    failures   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_used  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS my_skills (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    when_to    TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    uses       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""
MINE_MATCH = 0.5  # word overlap with a skill's name and "use when" for it to be handed to JARVIS
MINE_MAX = 2  # at most this many of the user's skills per request

_STOP = {"the", "a", "an", "to", "and", "or", "of", "in", "on", "for", "my", "me", "please", "can", "you", "it", "this",
         "that", "with", "jarvis", "sir", "i", "is", "be", "do", "from", "at", "into", "all", "some", "now", "then"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1}


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / max(1, min(len(a), len(b))) if a and b else 0.0


class Skills:
    def __init__(self, db: DB) -> None:
        self.db = db
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                db.run(stmt)

    def match(self, request: str) -> dict[str, Any] | None:
        """The most reliable workflow for a request that reads alike, if any."""
        want = _words(request)
        best, best_score = None, 0.0
        for r in self.db.all("SELECT * FROM skills"):
            sim = _overlap(want, _words(r["goal"]))
            if sim < MATCH_MIN:
                continue
            reliability = (r["successes"] + 1) / (r["successes"] + r["failures"] + 2)
            if reliability < 0.4:
                continue
            score = sim * 0.7 + reliability * 0.3
            if score > best_score:
                best, best_score = r, score
        if not best:
            return None
        return {"id": best["id"], "goal": best["goal"], "steps": json.loads(best["steps"]),
                "successes": best["successes"], "failures": best["failures"]}

    def learn(self, request: str, actions: list[dict[str, Any]], used: str | None) -> str | None:
        """After a request: score the workflow it was offered, and save a new one if this went well."""
        done = [a for a in actions if a.get("status") == "done" and a.get("tool") not in ("set_plan",)]
        failed = [a for a in actions if a.get("status") in ("failed", "declined")]
        now = time.time()
        if used:
            col = "successes" if done and not failed else "failures"
            self.db.run(f"UPDATE skills SET {col}={col}+1, last_used=? WHERE id=?", [now, used])
            if not failed:
                return used
        if len(done) < MIN_STEPS or failed:
            return None
        steps = [{"tool": a["tool"], "did": a["label"], "args": a.get("args", {})} for a in done][:20]
        same = next((r for r in self.db.all("SELECT id, goal FROM skills") if _overlap(_words(request), _words(r["goal"])) >= 0.9), None)
        if same:
            self.db.run("UPDATE skills SET steps=?, successes=successes+1, last_used=? WHERE id=?", [json.dumps(steps), now, same["id"]])
            return same["id"]
        sid = uuid.uuid4().hex[:10]
        self.db.run("INSERT INTO skills (id, goal, steps, created_at, last_used) VALUES (?,?,?,?,?)",
                    [sid, request[:300], json.dumps(steps), now, now])
        self.db.run("DELETE FROM skills WHERE id NOT IN (SELECT id FROM skills ORDER BY last_used DESC LIMIT ?)", [KEEP])
        return sid

    def list(self) -> list[dict[str, Any]]:
        return [{"id": r["id"], "goal": r["goal"], "steps": [s["did"] for s in json.loads(r["steps"])],
                 "successes": r["successes"], "failures": r["failures"], "last_used": r["last_used"]}
                for r in self.db.all("SELECT * FROM skills ORDER BY last_used DESC")]

    def forget(self, sid: str) -> bool:
        return self.db.run("DELETE FROM skills WHERE id=?", [sid]) > 0

    # ---------- the user's own skills (Customize > Skills): written by them, followed when a request fits ----------

    def mine(self) -> list[dict[str, Any]]:
        return [{**r, "enabled": bool(r["enabled"])} for r in self.db.all("SELECT * FROM my_skills ORDER BY name COLLATE NOCASE")]

    def save_mine(self, name: str, when_to: str, body: str, enabled: bool = True, sid: str | None = None) -> dict[str, Any]:
        name, when_to, body = name.strip()[:80], when_to.strip()[:300], body.strip()[:6000]
        if not name or not body:
            raise ValueError("A skill needs a name and its steps.")
        clash = self.db.one("SELECT id FROM my_skills WHERE lower(name)=lower(?) AND id<>?", [name, sid or ""])
        if clash:
            raise ValueError(f"There is already a skill called '{name}'.")
        now = time.time()
        if sid:
            if not self.db.run("UPDATE my_skills SET name=?, when_to=?, body=?, enabled=?, updated_at=? WHERE id=?",
                               [name, when_to, body, int(enabled), now, sid]):
                raise KeyError(sid)
        else:
            sid = uuid.uuid4().hex[:10]
            self.db.run("INSERT INTO my_skills (id, name, when_to, body, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                        [sid, name, when_to, body, int(enabled), now, now])
        return next(s for s in self.mine() if s["id"] == sid)

    def remove_mine(self, sid: str) -> bool:
        return self.db.run("DELETE FROM my_skills WHERE id=?", [sid]) > 0

    def match_mine(self, request: str) -> list[dict[str, Any]]:
        """The user's enabled skills that fit a request: named in it, or alike enough to its "use when"."""
        want, low = _words(request), request.lower()
        scored = []
        for s in self.db.all("SELECT * FROM my_skills WHERE enabled=1"):
            named = s["name"].lower() in low
            score = 1.0 if named else _overlap(want, _words(s["name"] + " " + s["when_to"]))
            if score >= MINE_MATCH:
                scored.append((score, s))
        picked = [s for _score, s in sorted(scored, key=lambda x: -x[0])[:MINE_MAX]]
        for s in picked:
            self.db.run("UPDATE my_skills SET uses=uses+1 WHERE id=?", [s["id"]])
        return picked


def guidance(skill: dict[str, Any]) -> str:
    """The workflow as a hint for the system prompt."""
    lines = []
    for i, s in enumerate(skill["steps"], 1):
        args = json.dumps(s.get("args", {}), ensure_ascii=False)
        lines.append(f"{i}. {s['did']} ({s['tool']} {args[:160]})")
    return (f"A workflow that worked before for a similar request (\"{skill['goal']}\", worked {skill['successes']} time(s), "
            f"failed {skill['failures']}):\n" + "\n".join(lines) +
            "\nUse it as a guide, not a script: check the current state, adapt names and paths, and skip steps that don't apply.")


def my_guidance(skill: dict[str, Any]) -> str:
    """One of the user's own skills, for the system prompt."""
    when = f" (use when: {skill['when_to']})" if skill.get("when_to") else ""
    return (f"The user's skill \"{skill['name']}\"{when}. They wrote these steps for this kind of request; follow them, "
            f"adapting to what you find:\n{skill['body']}")
