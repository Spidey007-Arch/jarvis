"""HTTP API for settings, models, conversations, chat streaming and memory management."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .db import DB
from .extraction import EXTRACT_SCHEMA, extract_prompt
from .llm import LLM, LLMError, LLMNotConfigured
from .providers import PRESETS
from .memory import TYPES, MemoryStore
from .persona import build_system_prompt
from .settings import SettingsStore

log = logging.getLogger("jarvis.api")

HISTORY_MESSAGES = 24
# Facts about the user are almost always stated in the first person; other messages skip extraction.
ABOUT_USER = re.compile(
    r"\b(i|i'm|im|i've|i'd|i'll|me|my|mine|myself|we|our|us|call me|forget|remember|don't|dont|no longer)\b", re.I
)


@dataclass
class Services:
    settings: SettingsStore
    llm: LLM
    db: DB
    memory: MemoryStore


class SettingsIn(BaseModel):
    user_name: str | None = Field(default=None, max_length=80)
    address: str | None = Field(default=None, max_length=40)
    provider: str | None = None
    api_key: str | None = Field(default=None, max_length=300)
    base_url: str | None = Field(default=None, max_length=300)
    chat_model: str | None = None
    embed_model: str | None = None


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: str | None = None


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None


class MemoryPatch(BaseModel):
    type: str | None = None
    subject: str | None = None
    content: str | None = None
    importance: float | None = None


class MemoryImport(BaseModel):
    memories: list[dict[str, Any]]


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def build_router(svc: Services, require_auth: Callable[..., None]) -> APIRouter:
    r = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
    background: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        t = asyncio.create_task(coro)
        background.add(t)
        t.add_done_callback(background.discard)

    # ---------------- settings & models ----------------

    @r.get("/settings")
    async def get_settings() -> dict[str, Any]:
        return svc.settings.public()

    @r.get("/providers")
    async def providers() -> list[dict[str, Any]]:
        return [{"id": pid, **preset} for pid, preset in PRESETS.items()]

    @r.put("/settings")
    async def put_settings(body: SettingsIn) -> dict[str, Any]:
        changes = body.model_dump(exclude_unset=True)
        profile = {k: changes.pop(k) for k in ("user_name", "address") if k in changes}
        if profile:
            svc.settings.update_profile(profile)

        pid = changes.pop("provider", None) or svc.settings.provider
        if pid not in PRESETS:
            raise HTTPException(status_code=400, detail=f"Unknown provider '{pid}'.")
        key = changes.pop("api_key", None)
        base_url = changes.pop("base_url", None)
        models: list[dict[str, Any]] | None = None

        if key is not None and not key.strip():
            svc.settings.set_api_key(pid, "")
        elif pid != svc.settings.provider or key is not None or base_url is not None:
            cand_key = key.strip() if key is not None else svc.settings.api_key(pid)
            cand_base = base_url.strip() if base_url is not None else svc.settings.provider_cfg(pid)["base_url"]
            if PRESETS[pid]["needs_key"] and not cand_key:
                # Switch now; the UI then asks for this provider's key.
                if base_url is not None:
                    svc.settings.set_provider_cfg(pid, base_url=cand_base)
                svc.settings.set_provider(pid)
                return svc.settings.public()
            try:
                models = await svc.llm.list_models(provider_id=pid, api_key=cand_key, base_url=cand_base)
            except LLMError as e:
                raise HTTPException(status_code=400, detail=f"Could not connect. {e}") from e
            if key is not None:
                svc.settings.set_api_key(pid, cand_key)
            if base_url is not None:
                svc.settings.set_provider_cfg(pid, base_url=cand_base)
            svc.settings.set_provider(pid)
            cfg = svc.settings.provider_cfg(pid)
            defaults = svc.llm.default_models(pid, models)
            chat_names = {m["name"] for m in models if m["kind"] == "chat"}
            embed_names = {m["name"] for m in models if m["kind"] == "embed"}
            if cfg["chat_model"] not in chat_names and "chat_model" not in changes:
                changes["chat_model"] = defaults["chat_model"]
            if cfg["embed_model"] and cfg["embed_model"] not in embed_names and "embed_model" not in changes:
                changes["embed_model"] = defaults["embed_model"]
            if not cfg["embed_model"] and "embed_model" not in changes:
                changes["embed_model"] = defaults["embed_model"]

        if "chat_model" in changes or "embed_model" in changes:
            if models is None:
                try:
                    models = await svc.llm.list_models()
                except (LLMError, LLMNotConfigured) as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
            chat_names = {m["name"] for m in models if m["kind"] == "chat"}
            embed_names = {m["name"] for m in models if m["kind"] == "embed"}
            if changes.get("chat_model") and changes["chat_model"] not in chat_names:
                raise HTTPException(status_code=400, detail=f"Model '{changes['chat_model']}' is not available from this provider.")
            if changes.get("embed_model") and changes["embed_model"] not in embed_names:
                raise HTTPException(status_code=400, detail=f"Embedding model '{changes['embed_model']}' is not available.")
            svc.settings.set_provider_cfg(pid, **{k: changes[k] or "" for k in ("chat_model", "embed_model") if k in changes})
        return svc.settings.public()

    @r.get("/models")
    async def models() -> dict[str, Any]:
        try:
            ms = await svc.llm.list_models()
        except LLMNotConfigured as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except LLMError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {"chat": [m for m in ms if m["kind"] == "chat"], "embed": [m for m in ms if m["kind"] == "embed"]}

    # ---------------- conversations ----------------

    def _conv_or_404(conv_id: str) -> dict[str, Any]:
        c = svc.db.one("SELECT * FROM conversations WHERE id=?", [conv_id])
        if not c:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return c

    @r.get("/conversations")
    async def conversations(q: str | None = None) -> list[dict[str, Any]]:
        if q:
            return svc.db.all(
                "SELECT DISTINCT c.* FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id"
                " WHERE c.title LIKE ? OR m.content LIKE ? ORDER BY c.pinned DESC, c.updated_at DESC",
                [f"%{q}%", f"%{q}%"],
            )
        return svc.db.all("SELECT * FROM conversations ORDER BY pinned DESC, updated_at DESC")

    @r.get("/conversations/{conv_id}/messages")
    async def messages(conv_id: str) -> list[dict[str, Any]]:
        _conv_or_404(conv_id)
        return svc.db.all("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", [conv_id])

    @r.patch("/conversations/{conv_id}")
    async def patch_conversation(conv_id: str, body: ConversationPatch) -> dict[str, Any]:
        _conv_or_404(conv_id)
        if body.title is not None:
            svc.db.run("UPDATE conversations SET title=? WHERE id=?", [body.title.strip(), conv_id])
        if body.pinned is not None:
            svc.db.run("UPDATE conversations SET pinned=? WHERE id=?", [int(body.pinned), conv_id])
        return _conv_or_404(conv_id)

    @r.delete("/conversations/{conv_id}")
    async def delete_conversation(conv_id: str) -> dict[str, bool]:
        _conv_or_404(conv_id)
        svc.db.run("DELETE FROM conversations WHERE id=?", [conv_id])
        return {"ok": True}

    # ---------------- chat ----------------

    async def _remember(user_text: str, reply: str, candidates: list[dict[str, Any]], conv_id: str) -> None:
        """Steps 6-7 of the memory pipeline; runs after the reply so it never delays the user."""
        if not ABOUT_USER.search(user_text):
            return  # nothing about the user in this message; saves a request against the free-tier quota
        try:
            out = await svc.llm.generate_json(extract_prompt(candidates, user_text, reply), EXTRACT_SCHEMA)
            ops = (out or {}).get("operations", [])
            if ops:
                await svc.memory.apply_ops(ops, conv_id)
                await svc.memory.maybe_summarize()
        except Exception:  # noqa: BLE001 - extraction must never break chat; the failure is logged
            log.exception("memory extraction failed")

    @r.post("/chat")
    async def chat(body: ChatIn, request: Request):
        text = body.message.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Message is empty.")
        if not svc.llm.ready:
            return JSONResponse(
                status_code=409,
                content={"error": "not_configured", "message": "No AI provider is connected yet. Choose one and add your API key in Settings."},
            )
        now = time.time()
        if body.conversation_id:
            _conv_or_404(body.conversation_id)
            conv_id = body.conversation_id
        else:
            conv_id = uuid.uuid4().hex
            title = text.splitlines()[0][:60]
            svc.db.run("INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)", [conv_id, title, now, now])
        prior = svc.db.all(
            "SELECT role, content FROM (SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at DESC LIMIT ?)"
            " ORDER BY created_at",
            [conv_id, HISTORY_MESSAGES],
        )
        user_msg_id = uuid.uuid4().hex
        svc.db.run(
            "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?,?,?,?,?)",
            [user_msg_id, conv_id, "user", text, now],
        )
        svc.db.run("UPDATE conversations SET updated_at=? WHERE id=?", [now, conv_id])

        async def events():
            parts: list[str] = []
            finished = False
            reply_id = uuid.uuid4().hex
            yield _sse("meta", {"conversation_id": conv_id, "user_message_id": user_msg_id, "reply_id": reply_id})
            try:
                core = svc.memory.core()
                try:
                    relevant = await svc.memory.search(text)
                except (LLMError, LLMNotConfigured) as e:
                    log.error("memory search failed: %s", e)
                    relevant = []
                    yield _sse("notice", {"message": f"Memory search unavailable for this reply: {e}"})
                svc.memory.touch([m["id"] for m in core + relevant])
                yield _sse("memory", {"core": len(core), "relevant": len(relevant)})
                system = build_system_prompt(svc.settings.public(), core, relevant)
                history = [(m["role"], m["content"]) for m in prior] + [("user", text)]
                notices: list[str] = []
                async for chunk in svc.llm.stream_chat(system, history, on_notice=notices.append):
                    if await request.is_disconnected():
                        break
                    while notices:
                        yield _sse("notice", {"message": notices.pop(0)})
                    parts.append(chunk)
                    yield _sse("delta", {"text": chunk})
                else:
                    finished = True
                if finished:
                    yield _sse("done", {"reply_id": reply_id})
                    spawn(_remember(text, "".join(parts), core + relevant, conv_id))
            except (LLMError, LLMNotConfigured) as e:
                yield _sse("error", {"message": str(e)})
            finally:
                # Keep what was generated even if the user pressed Stop.
                reply = "".join(parts)
                if reply:
                    svc.db.run(
                        "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?,?,?,?,?)",
                        [reply_id, conv_id, "model", reply, time.time()],
                    )
                    svc.db.run("UPDATE conversations SET updated_at=? WHERE id=?", [time.time(), conv_id])

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    # ---------------- memory management ----------------

    @r.get("/memories")
    async def list_memories(type: str | None = None, q: str | None = None, status: str = "active") -> list[dict[str, Any]]:
        if type and type not in TYPES:
            raise HTTPException(status_code=400, detail="Unknown memory type.")
        return svc.memory.list(type, q, status)

    @r.get("/memories/stats")
    async def memory_stats() -> dict[str, Any]:
        return svc.memory.stats()

    @r.patch("/memories/{mem_id}")
    async def patch_memory(mem_id: str, body: MemoryPatch) -> dict[str, Any]:
        try:
            m = svc.memory.update_manual(mem_id, body.model_dump(exclude_unset=True))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not m:
            raise HTTPException(status_code=404, detail="Memory not found.")
        return m

    @r.delete("/memories/{mem_id}")
    async def delete_memory(mem_id: str) -> dict[str, bool]:
        if not svc.memory.delete(mem_id):
            raise HTTPException(status_code=404, detail="Memory not found.")
        return {"ok": True}

    @r.get("/memories/export")
    async def export_memories() -> dict[str, Any]:
        return {"format": "jarvis-memories", "version": 1, "exported_at": time.time(), "memories": svc.memory.export()}

    @r.post("/memories/export-file")
    async def export_memories_file() -> dict[str, Any]:
        """Write the export to the user's Documents folder (the desktop webview cannot trigger downloads)."""
        docs = Path.home() / "Documents"
        target_dir = docs if docs.is_dir() else Path.home()
        path = target_dir / f"JARVIS memories {time.strftime('%Y-%m-%d %H%M%S')}.json"
        payload = {"format": "jarvis-memories", "version": 1, "exported_at": time.time(), "memories": svc.memory.export()}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"path": str(path), "count": len(payload["memories"])}

    @r.post("/memories/import")
    async def import_memories(body: MemoryImport) -> dict[str, int]:
        return svc.memory.import_items(body.memories)

    @r.post("/memories/maintenance")
    async def run_maintenance() -> dict[str, int]:
        return svc.memory.maintenance()

    return r
