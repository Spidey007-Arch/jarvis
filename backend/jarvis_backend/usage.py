"""Per-model request counts for the current quota day, and the limits Google's 429s reveal.

Neither Google's API nor most gateways report how much free-tier quota is left, so JARVIS keeps its own
count. Google's daily quotas reset at midnight Pacific time; a 429 names the metric and its limit
("... limit: 20, model: gemini-3.6-flash"), which is how the real limits are learned. The same key
backs both routes, so "gemini/x" through a gateway and "x" direct share one count."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .db import DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_usage (day TEXT NOT NULL, model TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, model));
CREATE TABLE IF NOT EXISTS token_usage (day TEXT NOT NULL, model TEXT NOT NULL, input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0, cached INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, model));
CREATE TABLE IF NOT EXISTS quota_limits (model TEXT PRIMARY KEY, per_day INTEGER, per_minute INTEGER, blocked_until REAL, reason TEXT);
"""

_LIMIT = re.compile(r"limit:\s*(\d+)", re.I)


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def pacific_offset(utc: datetime) -> timedelta:
    """US Pacific offset without tz data: PDT from the 2nd Sunday of March 02:00 to the 1st Sunday of November 02:00."""
    start = datetime.combine(_nth_sunday(utc.year, 3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=10)
    end = datetime.combine(_nth_sunday(utc.year, 11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=9)
    return timedelta(hours=-7) if start <= utc < end else timedelta(hours=-8)


def quota_day(now: float | None = None) -> str:
    utc = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc)
    return (utc + pacific_offset(utc)).date().isoformat()


def next_reset(now: float | None = None) -> float:
    """Epoch seconds of the next midnight Pacific."""
    utc = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc)
    local_midnight = datetime.combine((utc + pacific_offset(utc)).date() + timedelta(days=1), datetime.min.time(), timezone.utc)
    guess = local_midnight - pacific_offset(utc)
    return (local_midnight - pacific_offset(guess)).timestamp()


def base_model(name: str) -> str:
    return name.rsplit("/", 1)[-1]


class QuotaTracker:
    def __init__(self, db: DB) -> None:
        self.db = db
        for stmt in filter(None, (s.strip() for s in SCHEMA.split(";"))):
            db.run(stmt)

    def record(self, model: str, now: float | None = None) -> None:
        self.db.run(
            "INSERT INTO quota_usage (day, model, requests) VALUES (?, ?, 1) "
            "ON CONFLICT(day, model) DO UPDATE SET requests = requests + 1",
            [quota_day(now), base_model(model)],
        )
        # A success means the model is usable again.
        self.db.run("UPDATE quota_limits SET blocked_until = NULL, reason = NULL WHERE model = ?", [base_model(model)])

    def record_tokens(self, model: str, usage: dict[str, int], now: float | None = None) -> None:
        """Tokens one request used, as the provider reported them (cached: read from its cache)."""
        self.db.run(
            "INSERT INTO token_usage (day, model, input, output, cached) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(day, model) DO UPDATE SET input = input + excluded.input, output = output + excluded.output, "
            "cached = cached + excluded.cached",
            [quota_day(now), base_model(model), usage.get("input", 0), usage.get("output", 0), usage.get("cached", 0)],
        )

    def rejected(self, model: str, message: str, retry_after: float | None, now: float | None = None) -> None:
        """Learn from a 429: which limit was hit and until when the model is out."""
        now = now if now is not None else time.time()
        text = message.lower()
        m = _LIMIT.search(message)
        limit = int(m.group(1)) if m else None
        per_minute = "minute" in text
        if per_minute:
            until, reason = now + max(retry_after or 0, 60.0), "per-minute limit"
        elif "quota" in text:
            until, reason = next_reset(now), "daily quota used up"
        else:
            until, reason = now + max(retry_after or 0, 60.0), "rate limited"
        name = base_model(model)
        self.db.run("INSERT OR IGNORE INTO quota_limits (model) VALUES (?)", [name])
        column = "per_minute" if per_minute else "per_day"
        if limit is not None:
            self.db.run(f"UPDATE quota_limits SET {column} = ? WHERE model = ?", [limit, name])
        self.db.run("UPDATE quota_limits SET blocked_until = ?, reason = ? WHERE model = ?", [until, reason, name])

    def blocked_until(self, model: str, now: float | None = None) -> float | None:
        """When a model whose quota ran out is usable again (epoch seconds), or None if it is usable now."""
        row = self.db.one("SELECT blocked_until FROM quota_limits WHERE model = ?", [base_model(model)])
        until = row["blocked_until"] if row else None
        return until if until is not None and until > (now if now is not None else time.time()) else None

    def snapshot(self, models: list[str], now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        day = quota_day(now)
        used = {r["model"]: r["requests"] for r in self.db.all("SELECT model, requests FROM quota_usage WHERE day = ?", [day])}
        tokens = {r["model"]: r for r in self.db.all("SELECT * FROM token_usage WHERE day = ?", [day])}
        limits = {r["model"]: r for r in self.db.all("SELECT * FROM quota_limits")}
        names = list(dict.fromkeys([base_model(m) for m in models if m] + sorted(used)))
        rows = []
        for name in names:
            lim = limits.get(name) or {}
            blocked = lim.get("blocked_until")
            out = blocked is not None and blocked > now
            rows.append({
                "model": name,
                "used": used.get(name, 0),
                "limit": lim.get("per_day"),
                "state": "out" if out else "ready",
                "reason": lim.get("reason") if out else None,
                "until": blocked if out else None,
                "tokens_in": (tokens.get(name) or {}).get("input", 0),
                "tokens_out": (tokens.get(name) or {}).get("output", 0),
                "tokens_cached": (tokens.get(name) or {}).get("cached", 0),
            })
        total_in = sum(t["input"] for t in tokens.values())
        return {"day": day, "resets_at": next_reset(now), "models": rows,
                "tokens": {"input": total_in, "output": sum(t["output"] for t in tokens.values()),
                           "cached": sum(t["cached"] for t in tokens.values())}}

    def prune(self, keep_days: int = 14) -> None:
        cutoff = (datetime.fromisoformat(quota_day()) - timedelta(days=keep_days)).date().isoformat()
        self.db.run("DELETE FROM quota_usage WHERE day < ?", [cutoff])
        self.db.run("DELETE FROM token_usage WHERE day < ?", [cutoff])
