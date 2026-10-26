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
"""

_STOP = {"the", "a", "an", "to", "and", "or", "of", "in", "on", "for", "my", "me", "please", "can", "you", "it", "this",
         "that", "with", "jarvis", "sir", "i", "is", "be", "do", "from", "at", "into", "all", "some", "now", "then"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1}


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / max(1, min(len(a), len(b))) if a and b else 0.0


class Skills:
    def __init__(self, db: DB) -> None:
        self.db = db
        db.run(SCHEMA)

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


def guidance(skill: dict[str, Any]) -> str:
    """The workflow as a hint for the system prompt."""
    lines = []
    for i, s in enumerate(skill["steps"], 1):
        args = json.dumps(s.get("args", {}), ensure_ascii=False)
        lines.append(f"{i}. {s['did']} ({s['tool']} {args[:160]})")
    return (f"A workflow that worked before for a similar request (\"{skill['goal']}\", worked {skill['successes']} time(s), "
            f"failed {skill['failures']}):\n" + "\n".join(lines) +
            "\nUse it as a guide, not a script: check the current state, adapt names and paths, and skip steps that don't apply.")
