"""Background jobs: housekeeping that can wait, done when the user isn't using the PC.

Memory upkeep and summaries, and pruning old usage counts, don't need to happen the moment they become due. Each
job runs when it is due and the user is away or idle (and the laptop is plugged in or has charge to spare), one
job at a time, so it never competes with the user for the CPU, the network or the AI quota. A job that has waited
a whole extra period runs anyway, so nothing is put off for ever."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("jarvis.jobs")

TICK_S = 60.0
IDLE_S = 180  # no mouse or keyboard input for this long counts as idle


@dataclass
class Job:
    name: str
    every_s: float
    run: Callable[[], Awaitable[Any]]
    last_run: float = 0.0
    last_result: str = "not run yet"
    runs: int = 0
    first_due: float = field(default_factory=time.time)


class Jobs:
    def __init__(self, db: Any, stats: Callable[[], dict[str, Any]], presence: Callable[[], str]) -> None:
        self.db = db
        self.stats = stats  # the agent's latest heartbeat
        self.presence = presence  # "active" | "passive" | "away"
        self.jobs: list[Job] = []

    def add(self, name: str, every_s: float, run: Callable[[], Awaitable[Any]]) -> None:
        row = self.db.one("SELECT value FROM meta WHERE key=?", [f"job:{name}"])
        last = float(row["value"]) if row else 0.0
        self.jobs.append(Job(name, every_s, run, last_run=last))

    def _quiet(self) -> bool:
        s = self.stats() or {}
        idle = s.get("idle_s")
        user_idle = self.presence() in ("away", "passive") or (idle is not None and idle >= IDLE_S)
        battery_ok = s.get("battery_plugged") is not False or (s.get("battery_percent") or 100) >= 40
        return user_idle and battery_ok

    def due(self, now: float | None = None) -> Job | None:
        now = time.time() if now is None else now
        waiting = [j for j in self.jobs if now - j.last_run >= j.every_s]
        if not waiting:
            return None
        overdue = [j for j in waiting if now - j.last_run >= 2 * j.every_s and j.last_run]  # waited a whole extra period
        if overdue or self._quiet():
            return min(overdue or waiting, key=lambda j: j.last_run)
        return None

    async def tick(self, now: float | None = None) -> str | None:
        job = self.due(now)
        if job is None:
            return None
        started = time.time()
        try:
            result = await job.run()
            job.last_result = f"ok: {result}" if result not in (None, "") else "ok"
        except Exception as e:  # noqa: BLE001 - a failed job is retried next period, never fatal
            job.last_result = f"failed: {e}"[:200]
            log.exception("background job %s failed", job.name)
        job.last_run, job.runs = started, job.runs + 1
        self.db.run("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    [f"job:{job.name}", str(started)])
        log.info("background job", extra={"fields": {"job": job.name, "result": job.last_result[:80], "s": round(time.time() - started, 1)}})
        return job.name

    def view(self) -> list[dict[str, Any]]:
        return [{"name": j.name, "every_hours": round(j.every_s / 3600, 1), "last_run": j.last_run or None,
                 "next_due": (j.last_run or time.time()) + j.every_s, "last_result": j.last_result} for j in self.jobs]

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            await self.tick()
