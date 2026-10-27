"""Task mode: a longer job JARVIS works through step by step, which the user can watch and steer.

A request becomes a visible task when JARVIS posts a plan (the set_plan tool) or keeps acting for a few
steps. Before every step the task passes a checkpoint, so the user can pause it, take over the mouse and
keyboard and hand back, or stop it, and the next step respects that at once. After taking over, JARVIS is
told to look again before continuing, because the screen may have changed.

Tasks are saved as they go. One that was cut short by JARVIS closing is offered once after a restart; it
is never resumed on its own, and continuing starts by checking what the screen shows now."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .db import DB

VISIBLE_AFTER = 3  # tool steps before a request without a plan is shown as a task
PLAN_ROUNDS = 25  # model rounds allowed once JARVIS has planned a task (a normal request gets 8)
OFFER_FOR_S = 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    goal       TEXT NOT NULL,
    conv_id    TEXT,
    plan       TEXT NOT NULL DEFAULT '[]',
    current    INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL,      -- running | paused | taken_over | done | failed | stopped | interrupted | dismissed
    done_steps TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class TaskStopped(Exception):
    pass


@dataclass
class Task:
    id: str
    goal: str
    conv_id: str
    actions: list[dict[str, Any]]
    status: str = "running"
    plan: list[str] = field(default_factory=list)
    current: int = 0
    created_at: float = field(default_factory=time.time)
    max_rounds: int = 8
    handed_back: bool = False
    thought: str = ""  # the model's latest reasoning summary, for the panel
    agent: asyncio.Task | None = None
    resume: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def visible(self) -> bool:
        return bool(self.plan) or len(self.actions) >= VISIBLE_AFTER

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "goal": self.goal, "status": self.status, "plan": self.plan, "current": self.current,
                "actions": [{"label": a["label"], "status": a["status"]} for a in self.actions[-6:]],
                "steps_taken": len(self.actions), "started": self.created_at, "thought": self.thought}


class Tasks:
    def __init__(self, db: DB) -> None:
        self.db = db
        db.run(SCHEMA)
        self.current: Task | None = None
        # Whatever was still going when JARVIS last closed was cut short.
        db.run("UPDATE tasks SET status='interrupted' WHERE status IN ('running','paused','taken_over')")

    # ----- lifecycle -----

    def begin(self, goal: str, conv_id: str, actions: list[dict[str, Any]]) -> Task:
        t = Task(id=uuid.uuid4().hex[:10], goal=goal[:300], conv_id=conv_id, actions=actions)
        t.resume.set()
        self.current = t
        return t

    def finish(self, t: Task, status: str) -> None:
        if t.status not in ("stopped",):
            t.status = status
        if t.visible:
            self._save(t)
        if self.current is t:
            self.current = None

    def _save(self, t: Task) -> None:
        done = [a["label"] for a in t.actions if a["status"] == "done"][-40:]
        self.db.run("INSERT INTO tasks (id, goal, conv_id, plan, current, status, done_steps, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET plan=excluded.plan, current=excluded.current, "
                    "status=excluded.status, done_steps=excluded.done_steps, updated_at=excluded.updated_at",
                    [t.id, t.goal, t.conv_id, json.dumps(t.plan), t.current, t.status, json.dumps(done), t.created_at, time.time()])

    # ----- what JARVIS calls -----

    def set_plan(self, steps: list[str], current: int) -> dict[str, Any]:
        t = self.current
        steps = [str(s).strip()[:120] for s in steps if str(s).strip()][:15]
        if t is None:
            return {"ok": True, "note": "No task panel in a voice session; just carry on."}
        if steps:
            t.plan = steps
        t.current = max(0, min(int(current or 0), max(0, len(t.plan) - 1)))
        t.max_rounds = PLAN_ROUNDS
        self._save(t)
        return {"plan": t.plan, "working_on": t.plan[t.current] if t.plan else None}

    async def checkpoint(self, t: Task) -> str | None:
        """Called before every step. Waits while paused or taken over; raises TaskStopped if stopped. Returns
        'handed_back' once after the user hands control back."""
        if t.status == "stopped":
            raise TaskStopped
        if not t.resume.is_set():
            await t.resume.wait()
            if t.status == "stopped":
                raise TaskStopped
        if t.handed_back:
            t.handed_back = False
            return "handed_back"
        if t.visible:
            self._save(t)
        return None

    # ----- what the user does -----

    def control(self, task_id: str, what: str) -> dict[str, Any]:
        t = self.current
        if t is None or t.id != task_id:
            raise KeyError(task_id)
        if what == "pause":
            t.status = "paused"
            t.resume.clear()
        elif what == "take_over":
            t.status = "taken_over"
            t.resume.clear()
        elif what in ("resume", "hand_back"):
            t.handed_back = t.status == "taken_over" or what == "hand_back"
            t.status = "running"
            t.resume.set()
        elif what == "stop":
            self.stop(t)
        else:
            raise ValueError(what)
        self._save(t)
        return t.view()

    def stop(self, t: Task) -> None:
        t.status = "stopped"
        t.resume.set()  # let a paused step wake up and see the stop
        if t.agent and not t.agent.done():
            t.agent.cancel()

    def stop_all(self) -> bool:
        t = self.current
        if t is None:
            return False
        self.stop(t)
        self._save(t)
        return True

    # ----- after a restart -----

    def offer(self) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM tasks WHERE status='interrupted' AND updated_at > ? ORDER BY updated_at DESC LIMIT 1",
                          [time.time() - OFFER_FOR_S])
        if not row:
            return None
        return {"id": row["id"], "goal": row["goal"], "plan": json.loads(row["plan"]), "current": row["current"],
                "done_steps": json.loads(row["done_steps"])}

    def dismiss(self, task_id: str) -> None:
        self.db.run("UPDATE tasks SET status='dismissed' WHERE id=?", [task_id])

    def continue_prompt(self, task_id: str) -> str:
        row = self.db.one("SELECT * FROM tasks WHERE id=?", [task_id])
        if not row:
            raise KeyError(task_id)
        self.db.run("UPDATE tasks SET status='dismissed' WHERE id=?", [task_id])
        plan = json.loads(row["plan"])
        done = json.loads(row["done_steps"])
        parts = [f"Continue the task I started earlier, which was cut short: {row['goal']}"]
        if plan:
            parts.append("The plan was: " + "; ".join(f"{i + 1}. {s}" for i, s in enumerate(plan)) +
                         f". You were on step {row['current'] + 1}.")
        if done:
            parts.append("Already done: " + "; ".join(done[-12:]) + ".")
        parts.append("Things may have changed since: check the current state first (look at the screen or the files), "
                     "then carry on from where it stopped. Don't repeat anything that was already done.")
        return " ".join(parts)
