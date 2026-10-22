"""Long-term semantic memory.

Pipeline per chat turn (see chat.py): embed the message -> search -> rank ->
inject the relevant subset -> generate -> extract facts -> apply create/update/
delete operations here.

Superseded memories are kept (status 'superseded', pointing at their
replacement) so changes are auditable, then purged after RETENTION_DAYS.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from array import array
from typing import Any, Protocol

from .db import DB
from .extraction import SUMMARY_SCHEMA, summary_prompt

log = logging.getLogger("jarvis.memory")

TYPES = ("identity", "preference", "goal", "project", "relationship", "event", "behavior")
CORE_TYPES = ("identity", "behavior")  # always in context: they shape how JARVIS addresses the user
SIM_MIN = 0.55          # minimum cosine similarity for a memory to be considered relevant
DEDUP_SIM = 0.92        # a "new" memory this close to an existing one of the same type is an update
SUMMARIZE_AT = 10       # consolidate once one subject has this many active memories
RETENTION_DAYS = 90     # how long superseded / retracted / summarized history is kept
EVENT_STALE_DAYS = 365  # low-importance events nobody has needed for a year expire
RECENCY_HALF_LIFE_DAYS = 45
DAY = 86400.0


class Embedder(Protocol):
    embed_model: str

    async def embed(self, texts: list[str], task: str) -> list[list[float]]: ...
    async def generate_json(self, prompt: str, schema: dict[str, Any]) -> Any: ...


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _pack(v: list[float]) -> bytes:
    return array("f", _normalize(v)).tobytes()


def _unpack(b: bytes) -> array:
    a = array("f")
    a.frombytes(b)
    return a


def _dot(a: Any, b: Any) -> float:
    return sum(x * y for x, y in zip(a, b))


def _text(subject: str, content: str) -> str:
    return f"{subject}: {content}"


def _public(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k != "embedding"}
    out["has_embedding"] = row.get("embedding") is not None
    return out


def _clamp(x: Any, default: float = 0.5) -> float:
    try:
        return min(max(float(x), 0.0), 1.0)
    except (TypeError, ValueError):
        return default


class MemoryStore:
    def __init__(self, db: DB, llm: Embedder) -> None:
        self.db = db
        self.llm = llm
        self.write_lock = asyncio.Lock()

    # ---------- reads ----------

    def list(self, type_: str | None = None, q: str | None = None, status: str = "active") -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM memories WHERE status = ?", [status]
        if type_:
            sql += " AND type = ?"
            args.append(type_)
        if q:
            sql += " AND (content LIKE ? OR subject LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY updated_at DESC"
        return [_public(r) for r in self.db.all(sql, args)]

    def get(self, mem_id: str) -> dict[str, Any] | None:
        r = self.db.one("SELECT * FROM memories WHERE id = ?", [mem_id])
        return _public(r) if r else None

    def stats(self) -> dict[str, Any]:
        rows = self.db.all("SELECT type, COUNT(*) AS n FROM memories WHERE status='active' GROUP BY type")
        last = self.db.one("SELECT MAX(updated_at) AS t FROM memories WHERE status='active'")
        by_type = {t: 0 for t in TYPES}
        by_type.update({r["type"]: r["n"] for r in rows})
        return {"total": sum(by_type.values()), "by_type": by_type, "last_updated": last["t"] if last else None}

    def core(self) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM memories WHERE status='active' AND type IN (?, ?) ORDER BY importance DESC, updated_at DESC LIMIT 15",
            CORE_TYPES,
        )
        return [_public(r) for r in rows]

    # ---------- embeddings & search ----------

    async def embed_missing(self) -> int:
        """Embed active memories that have no vector yet (imports, manual edits, model change)."""
        model = self.llm.embed_model
        rows = self.db.all(
            "SELECT id, subject, content FROM memories WHERE status='active' AND (embedding IS NULL OR embedding_model IS NOT ?)",
            [model],
        )
        if not rows:
            return 0
        vecs = await self.llm.embed([_text(r["subject"], r["content"]) for r in rows], "RETRIEVAL_DOCUMENT")
        self.db.transaction(
            [("UPDATE memories SET embedding=?, embedding_model=? WHERE id=?", [_pack(v), model, r["id"]]) for r, v in zip(rows, vecs)]
        )
        return len(rows)

    async def search(self, query: str, k: int = 8, now: float | None = None) -> list[dict[str, Any]]:
        """Relevant non-core memories, ranked by similarity, importance and recency."""
        now = now or time.time()
        await self.embed_missing()
        qv = _normalize((await self.llm.embed([query], "RETRIEVAL_QUERY"))[0])
        rows = self.db.all(
            "SELECT * FROM memories WHERE status='active' AND embedding IS NOT NULL AND type NOT IN (?, ?)", CORE_TYPES
        )
        ranked = []
        for r in rows:
            sim = _dot(qv, _unpack(r["embedding"]))
            if sim < SIM_MIN:
                continue
            age_days = (now - max(r["updated_at"], r["last_accessed_at"] or 0)) / DAY
            recency = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
            score = 0.7 * sim + 0.2 * r["importance"] + 0.1 * recency
            ranked.append({**_public(r), "similarity": round(sim, 3), "score": round(score, 3)})
        ranked.sort(key=lambda m: m["score"], reverse=True)
        return ranked[:k]

    def touch(self, ids: list[str]) -> None:
        now = time.time()
        for i in ids:
            self.db.run("UPDATE memories SET last_accessed_at=?, access_count=access_count+1 WHERE id=?", [now, i])

    async def _nearest_same_type(self, type_: str, vec: list[float]) -> tuple[dict[str, Any] | None, float]:
        best, best_sim = None, -1.0
        nv = _normalize(vec)
        for r in self.db.all("SELECT * FROM memories WHERE status='active' AND type=? AND embedding IS NOT NULL", [type_]):
            s = _dot(nv, _unpack(r["embedding"]))
            if s > best_sim:
                best, best_sim = r, s
        return best, best_sim

    # ---------- writes ----------

    def _insert(self, type_: str, subject: str, content: str, importance: float, source: str, vec: list[float] | None) -> str:
        mem_id = uuid.uuid4().hex
        now = time.time()
        self.db.run(
            "INSERT INTO memories (id, type, subject, content, importance, source, created_at, updated_at, embedding, embedding_model)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [mem_id, type_, subject, content, importance, source, now, now,
             _pack(vec) if vec else None, self.llm.embed_model if vec else None],
        )
        return mem_id

    def _retire(self, mem_id: str, status: str, replaced_by: str | None) -> None:
        self.db.run(
            "UPDATE memories SET status=?, superseded_by=?, updated_at=? WHERE id=? AND status='active'",
            [status, replaced_by, time.time(), mem_id],
        )

    async def apply_ops(self, ops: list[dict[str, Any]], source: str) -> dict[str, int]:
        """Apply extractor output. Invalid operations are skipped and logged, never guessed at."""
        result = {"created": 0, "updated": 0, "deleted": 0, "skipped": 0}
        async with self.write_lock:
            for op in ops:
                kind = op.get("op")
                target = op.get("target_id") or None
                target_row = self.db.one("SELECT * FROM memories WHERE id=? AND status='active'", [target]) if target else None

                if kind == "delete":
                    if target_row:
                        self._retire(target_row["id"], "retracted", None)
                        result["deleted"] += 1
                    else:
                        result["skipped"] += 1
                    continue

                type_ = op.get("type")
                subject = (op.get("subject") or "").strip().lower()[:120]
                content = (op.get("content") or "").strip()[:600]
                if kind not in ("create", "update") or type_ not in TYPES or not subject or not content:
                    log.warning("skipping invalid memory op", extra={"fields": {"op": op}})
                    result["skipped"] += 1
                    continue
                importance = _clamp(op.get("importance"))
                vec = (await self.llm.embed([_text(subject, content)], "RETRIEVAL_DOCUMENT"))[0]

                if not target_row:
                    near, sim = await self._nearest_same_type(type_, vec)
                    if near is not None and sim >= DEDUP_SIM:
                        target_row = near
                if target_row and target_row["content"].strip() == content and target_row["subject"] == subject:
                    self.db.run(
                        "UPDATE memories SET importance=MAX(importance, ?), updated_at=? WHERE id=?",
                        [importance, time.time(), target_row["id"]],
                    )
                    result["skipped"] += 1
                    continue

                new_id = self._insert(type_, subject, content, importance, source, vec)
                if target_row:
                    self._retire(target_row["id"], "superseded", new_id)
                    result["updated"] += 1
                else:
                    result["created"] += 1
        if result["created"] or result["updated"] or result["deleted"]:
            log.info("memory updated", extra={"fields": result})
        return result

    async def maybe_summarize(self) -> int:
        """Consolidate subjects that have accumulated many separate memories."""
        groups = self.db.all(
            "SELECT type, subject, COUNT(*) AS n FROM memories WHERE status='active' GROUP BY type, subject HAVING n >= ?",
            [SUMMARIZE_AT],
        )
        merged = 0
        for g in groups:
            rows = self.db.all(
                "SELECT * FROM memories WHERE status='active' AND type=? AND subject=? ORDER BY updated_at",
                [g["type"], g["subject"]],
            )
            out = await self.llm.generate_json(summary_prompt(g["type"], g["subject"], rows), SUMMARY_SCHEMA)
            items = [m for m in (out or {}).get("memories", []) if (m.get("content") or "").strip()]
            if not items or len(items) >= len(rows):
                continue
            async with self.write_lock:
                vecs = await self.llm.embed([_text(g["subject"], m["content"]) for m in items], "RETRIEVAL_DOCUMENT")
                new_ids = [
                    self._insert(g["type"], g["subject"], m["content"].strip()[:600], _clamp(m.get("importance")), "summary", v)
                    for m, v in zip(items, vecs)
                ]
                for r in rows:
                    self._retire(r["id"], "summarized", new_ids[0])
            merged += len(rows)
            log.info("summarized memories", extra={"fields": {"subject": g["subject"], "from": len(rows), "to": len(items)}})
        return merged

    def maintenance(self, now: float | None = None) -> dict[str, int]:
        now = now or time.time()
        expired = self.db.run(
            "UPDATE memories SET status='expired', updated_at=? WHERE status='active' AND type='event' AND importance < 0.3"
            " AND COALESCE(last_accessed_at, updated_at) < ?",
            [now, now - EVENT_STALE_DAYS * DAY],
        )
        purged = self.db.run(
            "DELETE FROM memories WHERE status IN ('superseded','summarized','retracted','expired') AND updated_at < ?",
            [now - RETENTION_DAYS * DAY],
        )
        return {"expired": expired, "purged": purged}

    # ---------- manual management (Settings > Memory) ----------

    def update_manual(self, mem_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM memories WHERE id=?", [mem_id])
        if not row:
            return None
        type_ = fields.get("type", row["type"])
        if type_ not in TYPES:
            raise ValueError(f"Unknown memory type '{type_}'.")
        subject = (fields.get("subject", row["subject"]) or "").strip().lower()[:120]
        content = (fields.get("content", row["content"]) or "").strip()[:600]
        if not subject or not content:
            raise ValueError("Subject and content cannot be empty.")
        importance = _clamp(fields.get("importance", row["importance"]))
        text_changed = subject != row["subject"] or content != row["content"]
        self.db.run(
            "UPDATE memories SET type=?, subject=?, content=?, importance=?, updated_at=?, source='manual'"
            + (", embedding=NULL, embedding_model=NULL" if text_changed else "")
            + " WHERE id=?",
            [type_, subject, content, importance, time.time(), mem_id],
        )
        return self.get(mem_id)

    def delete(self, mem_id: str) -> bool:
        """User-requested deletion is permanent, including the history of that memory."""
        return self.db.run("DELETE FROM memories WHERE id=? OR superseded_by=?", [mem_id, mem_id]) > 0

    def export(self) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT type, subject, content, importance, created_at, updated_at FROM memories WHERE status='active'")
        return rows

    def import_items(self, items: list[dict[str, Any]]) -> dict[str, int]:
        added, skipped = 0, 0
        for it in items:
            type_ = it.get("type")
            subject = (it.get("subject") or "").strip().lower()[:120]
            content = (it.get("content") or "").strip()[:600]
            if type_ not in TYPES or not subject or not content:
                skipped += 1
                continue
            exists = self.db.one(
                "SELECT id FROM memories WHERE status='active' AND type=? AND subject=? AND content=?", [type_, subject, content]
            )
            if exists:
                skipped += 1
                continue
            self._insert(type_, subject, content, _clamp(it.get("importance")), "import", None)
            added += 1
        return {"added": added, "skipped": skipped}
