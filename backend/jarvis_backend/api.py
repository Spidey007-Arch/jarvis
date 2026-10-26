"""HTTP API for settings, models, conversations, chat streaming and memory management."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import briefing, public_apis
from .db import DB
from .extraction import EXTRACT_SCHEMA, extract_prompt
from .bridge import AgentBridge, AgentError, AgentUnavailable
from .llm import LLM, AgentIncomplete, LLMError, LLMNotConfigured, ToolsUnsupported
from .providers.base import ToolCall
from .monitor import Monitor
from .reminders import ReminderError, Reminders
from .research import ResearchError, web_research
from .live import LiveError, LiveVoice
from .tools import BY_NAME, OBSERVE_TOOLS, STEPWISE_TOOLS, TOOLS, ToolSpec, describe_call
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
    bridge: AgentBridge
    reminders: Reminders
    monitor: Monitor
    live: Any = None  # LiveVoice, created with the router (it needs the router's tool runner)
    offline_voice_until: float = 0.0  # Live voice paused until then: the connection was too slow for it
    agent_event: Callable[[dict[str, Any]], None] | None = None  # set by the router: events the agent reports on its own
    recent_downloads: list[dict[str, Any]] = field(default_factory=list)  # browser downloads noticed this session, newest last

    def voice_mode(self) -> str:
        """The voice engine to use now: the chosen one, except offline while the line is too slow for Live."""
        mode = self.settings.get("voice_mode") or "live"
        return "local" if mode == "live" and time.time() < self.offline_voice_until else mode


class ToolFailure(Exception):
    pass


TOOL_SCHEMAS = [t.schema() for t in TOOLS]
OFFLINE_FOR_S = 30 * 60  # after Live proves too slow, use the offline voice this long before trying Live again
audit = logging.getLogger("jarvis.automation")

# A click on a control like these needs the user's Allow first (and cannot happen from a voice session).
RISKY_CLICK = re.compile(
    r"\b(send|pay|payment|buy|purchase|order|checkout|check out|delete|remove|uninstall|erase|format|wipe|submit|post|publish|tweet|"
    r"transfer|withdraw|confirm|accept|agree|allow|grant|sign ?out|log ?out|shut ?down|restart|discard|unsubscribe|reset|install|"
    r"forward|reply all|empty trash|clear (all|history|data))\b", re.I)

LOCATE_PROMPT = (
    'You are looking at a screenshot of the user\'s Windows desktop. Find this on it: "{target}".\n'
    'Reply with JSON only, no other text: {{"found": true, "point": [y, x], "label": "<the control\'s visible text, or a short name '
    'if it has none>", "kind": "button|link|field|icon|menu|tab|item|other"}}, where point is the centre of that element as integers '
    'normalized to 0-1000 (y first, from the top; x from the left). If it is not visible, or you are not sure which element is meant, '
    'reply {{"found": false, "reason": "<what you see instead>"}}.'
)


# Commands that remove, uninstall, end or reset something: the user says yes first, even with Full control.
DANGER_COMMAND = re.compile(
    r"(remove-item|\b(rm|rmdir|rd|del|erase|ri)\b|uninstall|stop-process|\bkill\b|taskkill|stop-service|disable-|restart-computer|"
    r"stop-computer|shutdown|logoff|reg(\.exe)?\s+(delete|add)|set-itemproperty|remove-itemproperty|netsh|set-executionpolicy|"
    r"git\s+(push\b.*(\s-f\b|--force)|reset\s+--hard|clean\s+-[a-z]*f)|drop\s+(table|database)|truncate\s+table|"
    r"clear-recyclebin|format-volume)", re.I)


SPOKEN_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|do it|go ahead|go for it|confirm(ed)?|haan|han|ha|okay|ok|proceed|please do|absolutely)\b", re.I)
SPOKEN_NO = re.compile(r"\b(no|nope|don'?t|stop|cancel|wait|nahi|mat|never ?mind)\b", re.I)


def needs_approval(spec: ToolSpec, args: dict[str, Any], full_control: bool = False) -> bool:
    """Whether the user must say yes before this runs. With Full control (the owner's own PC) only destructive or
    irreversible steps ask; without it (the default for anyone else's install) every change asks."""
    if spec.risk == "danger":
        return True
    if spec.name == "screen_click" and RISKY_CLICK.search(str(args.get("target", ""))):
        return True
    if spec.name == "click_text" and RISKY_CLICK.search(str(args.get("text", ""))):
        return True
    if spec.name == "autostart" and str(args.get("action", "status")).lower() != "status":
        return not full_control  # a lasting change to how Windows starts
    if spec.name == "window_control" and str(args.get("action", "")).lower() == "close":
        return not full_control  # the app may lose unsaved work (it usually asks first)
    if spec.name == "run_command" and DANGER_COMMAND.search(str(args.get("command", ""))):
        return True
    if spec.risk == "confirm":
        if spec.name == "organize_folder" and args.get("dry_run", True) is not False:
            return False  # a dry run only reports a plan
        return not full_control
    return False


def _trim(value: Any, limit: int = 6000) -> Any:
    """Keep tool results small enough for the model's context."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "partial_json": text[:limit]}


def call_key(call: ToolCall) -> str:
    return call.name + json.dumps({k: v for k, v in call.args.items() if not k.startswith("_")}, sort_keys=True, default=str)


def repeat_of(call: ToolCall, done: dict[str, tuple[int, int, dict[str, Any]]], step: int) -> dict[str, Any] | None:
    """Within one request, an exact repeat of a call is a loop, not progress: answer it from the
    first run instead of acting again. Observations may repeat once something else has happened,
    and a failed action may be retried once."""
    if call.name in STEPWISE_TOOLS:
        return None
    key = call_key(call)
    if key not in done:
        return None
    at, runs, out = done[key]
    if not out.get("ok") and runs < 2:
        return None
    if call.name in OBSERVE_TOOLS and step > at:
        return None
    return {**out, "repeated": True,
            "note": "You already made this exact call in this request, so it was not run again; the earlier outcome is above. "
                    "Use it, try a different approach, or tell the user what is blocking you."}


class SettingsIn(BaseModel):
    user_name: str | None = Field(default=None, max_length=80)
    address: str | None = Field(default=None, max_length=40)
    proactive_alerts: bool | None = None
    startup_briefing: bool | None = None
    full_control: bool | None = None
    provider: str | None = None
    api_key: str | None = Field(default=None, max_length=300)
    base_url: str | None = Field(default=None, max_length=300)
    chat_model: str | None = None
    embed_model: str | None = None


class ConfirmIn(BaseModel):
    approve: bool


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: str | None = None
    voice: bool = False  # spoken aloud; the reply will be read out


class VoiceIn(BaseModel):
    enabled: bool


class ServiceKeyIn(BaseModel):
    service: str = Field(min_length=1, max_length=80)
    key: str = Field(min_length=1, max_length=500)


class VoiceModeIn(BaseModel):
    mode: str = Field(pattern="^(live|local)$")


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    queue: bool = False  # follow what is already being said (a reply spoken as it streams)


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
        profile = {k: changes.pop(k) for k in ("user_name", "address", "proactive_alerts", "startup_briefing",
                                                   "full_control") if k in changes}
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

    def _situation(current_conv: str) -> dict[str, Any]:
        """Live context so JARVIS can be proactive and pick up earlier threads."""
        recent = []
        rows = svc.db.all(
            "SELECT id, title, updated_at FROM conversations WHERE id != ? AND updated_at > ? ORDER BY updated_at DESC LIMIT 3",
            [current_conv, time.time() - 14 * 86400],
        )
        for c in rows:
            last = svc.db.one(
                "SELECT content FROM messages WHERE conversation_id=? AND role='model' ORDER BY created_at DESC LIMIT 1", [c["id"]]
            )
            when = datetime.fromtimestamp(c["updated_at"])
            label = "today" if when.date() == datetime.now().date() else when.strftime("%A %d %B")
            snippet = (last["content"][:160] + ("…" if len(last["content"]) > 160 else "")) if last else ""
            recent.append({"title": c["title"], "when": f"{label} {when:%H:%M}", "last": snippet.replace("\n", " ")})
        return {
            "pc": svc.bridge.stats if svc.bridge.connected else None,
            "reminders": svc.reminders.pending(),
            "recent": recent,
        }

    @r.get("/alerts")
    async def alerts(since: float = 0.0) -> list[dict[str, Any]]:
        return svc.monitor.recent(since)

    pending_confirmations: dict[str, asyncio.Future] = {}

    @r.post("/confirm/{call_id}")
    async def confirm(call_id: str, body: ConfirmIn) -> dict[str, bool]:
        fut = pending_confirmations.get(call_id)
        if fut is None or fut.done():
            raise HTTPException(status_code=404, detail="That request is no longer waiting for an answer.")
        fut.set_result(body.approve)
        return {"ok": True}

    async def _locate_on_screen(target: str) -> dict[str, Any]:
        """Find a control on screen with the vision model: a fresh screenshot (deleted at once), and a point
        in fractions of the screen that the agent turns into a click."""
        shot = await svc.bridge.call("capture_screen", {}, timeout=20)
        path = Path(shot["path"])
        try:
            image = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
        answer = await svc.llm.describe_image(LOCATE_PROMPT.format(target=target.replace('"', "'")), image, light=True)
        m = re.search(r"\{.*\}", answer or "", re.S)
        try:
            spot = json.loads(m.group(0)) if m else {}
        except ValueError:
            spot = {}
        if not spot.get("found"):
            raise ToolFailure(f"I can't see '{target}' on the screen" + (f": {spot['reason']}" if spot.get("reason") else ".")
                              + " Open or scroll to it first, or describe it differently.")
        try:
            y, x = (float(v) for v in spot["point"])
        except (KeyError, TypeError, ValueError) as e:
            raise ToolFailure("The vision model did not give a usable position; try again.") from e
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            raise ToolFailure("The vision model gave a position off the screen; try again.")
        return {"x": x / 1000, "y": y / 1000, "label": str(spot.get("label") or "")[:80], "kind": spot.get("kind")}

    async def _run_tool(spec: ToolSpec, args: dict[str, Any]) -> Any:
        if spec.agent_action:
            return await svc.bridge.call(spec.agent_action, args, timeout=spec.timeout)
        if spec.name == "web_research":
            return await web_research(str(args.get("query", "")))
        if spec.name == "look_at_screen":
            shot = await svc.bridge.call("capture_screen", {}, timeout=20)
            path = Path(shot["path"])
            try:
                image = path.read_bytes()
            finally:
                path.unlink(missing_ok=True)  # the screenshot never stays on disk
            question = str(args.get("question") or "Describe what is on the screen.")
            answer = await svc.llm.describe_image(
                "You are looking at a screenshot of the user's Windows desktop. "
                f"{question} Be specific and quote any error text exactly.",
                image,
            )
            return {"answer": answer}
        if spec.name == "screen_click":
            target = str(args.get("target", "")).strip()
            if not target:
                raise ToolFailure("Say what to click, e.g. 'the Send button in WhatsApp'.")
            spot = await _locate_on_screen(target)
            label = spot.get("label") or target
            if RISKY_CLICK.search(label) and not RISKY_CLICK.search(target):
                raise ToolFailure(f"That control is '{label}', which sends, pays, deletes or changes something. Nothing was clicked. "
                                  f"If the user wants it, call screen_click again with target naming '{label}' so they are asked to approve it.")
            done = await svc.bridge.call("mouse_click", {"x": spot["x"], "y": spot["y"], "button": args.get("button") or "left",
                                                          "double": bool(args.get("double"))}, timeout=15)
            return {"clicked": label, "kind": spot.get("kind"), "at": done.get("clicked"),
                    "next": "Check the result with look_at_screen before the next step if anything is uncertain."}
        if spec.name == "screen_drag":
            src = await _locate_on_screen(str(args.get("source") or ""))
            dst = await _locate_on_screen(str(args.get("target") or ""))
            done = await svc.bridge.call("mouse_drag", {"x1": src["x"], "y1": src["y"], "x2": dst["x"], "y2": dst["y"]}, timeout=20)
            return {"dragged": src.get("label") or args.get("source"), "onto": dst.get("label") or args.get("target"), **done,
                    "next": "Check the result with look_at_screen or read_window if anything is uncertain."}
        if spec.name == "screen_scroll":
            direction = str(args.get("direction") or "down").lower()
            clicks = max(1, min(int(args.get("amount") or 5), 30)) * (1 if direction == "up" else -1)
            where: dict[str, Any] = {"clicks": clicks}
            if args.get("over"):
                spot = await _locate_on_screen(str(args["over"]))
                where.update(x=spot["x"], y=spot["y"])
            return await svc.bridge.call("mouse_scroll", where, timeout=15)
        if spec.name == "find_apis":
            return public_apis.find(str(args.get("query") or ""), str(args.get("category") or ""), bool(args.get("no_key_only")),
                                    int(args.get("limit") or 12), has_key=lambda n: bool(svc.settings.service_key(n)))
        if spec.name == "call_api":
            key, use = "", str(args.get("use_key") or "").strip()
            if use:
                key = svc.settings.service_key(use)
                if not key:
                    raise ToolFailure(f"No key is saved for '{use}'. The user can add one in Settings > API keys "
                                      f"(saved: {', '.join(svc.settings.service_keys()) or 'none'}).")
            params = public_apis.parse_params(args.get("params"))
            try:
                return await public_apis.call(str(args.get("url", "")), params, key, str(args.get("key_param") or ""),
                                              str(args.get("key_header") or ""))
            except public_apis.APIError as e:
                raise ToolFailure(str(e)) from e
        if spec.name == "set_reminder":
            return svc.reminders.add(str(args.get("text", "")), str(args.get("due", "")))
        if spec.name == "set_alarm":
            return svc.reminders.add_alarm(str(args.get("at", "")), str(args.get("label") or ""))
        if spec.name == "set_timer":
            secs = int(args.get("minutes") or 0) * 60 + int(args.get("seconds") or 0)
            return svc.reminders.add_timer(secs, str(args.get("label") or ""))
        if spec.name == "list_reminders":
            return {"pending": svc.reminders.pending()}
        if spec.name == "cancel_reminder":
            return svc.reminders.cancel(str(args.get("id", "")))
        raise ToolFailure(f"Tool '{spec.name}' has no implementation.")

    async def _execute(call: ToolCall, queue: asyncio.Queue, actions: list[dict[str, Any]]) -> dict[str, Any]:
        """Run one tool call for the AI, asking first for risky ones. Always returns the real outcome."""
        spec = BY_NAME.get(call.name)
        if spec is None:
            return {"ok": False, "error": f"There is no tool named '{call.name}'."}
        args = {k: v for k, v in call.args.items() if not k.startswith("_")}
        cid = uuid.uuid4().hex[:10]
        entry: dict[str, Any] = {"id": cid, "tool": spec.name, "label": describe_call(spec, args), "status": "running"}
        if spec.name == "call_api":  # a quiet "via <API> · <category>" line, not the raw link
            entry.update(label="via " + public_apis.source_of(str(args.get("url", "")), str(args.get("use_key") or "")), quiet=True)
        elif spec.name == "find_apis":
            entry.update(label=f"searched the API index for {args.get('query') or args.get('category') or 'APIs'}", quiet=True)
        actions.append(entry)

        def emit(status: str, **extra: Any) -> None:
            entry.update(status=status, **extra)
            queue.put_nowait(("tool", dict(entry)))

        if needs_approval(spec, args, bool(svc.settings.get("full_control"))):
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            pending_confirmations[cid] = fut
            emit("awaiting_confirmation", detail=json.dumps(args, ensure_ascii=False))
            try:
                approved = await asyncio.wait_for(fut, timeout=120)
            except asyncio.TimeoutError:
                approved = False
            finally:
                pending_confirmations.pop(cid, None)
            if not approved:
                emit("declined")
                return {"ok": False, "error": "The user declined (or did not answer within 2 minutes), so nothing was done and nothing is scheduled. Do not retry unless asked."}
        emit("running")
        audit.info("tool call", extra={"fields": {"tool": spec.name, "args": args}})
        try:
            result = await _run_tool(spec, args)
        except (AgentUnavailable, AgentError, ResearchError, ReminderError, ToolFailure, LLMError, LLMNotConfigured) as e:
            emit("failed", error=str(e))
            audit.warning("tool failed", extra={"fields": {"tool": spec.name, "error": str(e)}})
            return {"ok": False, "error": str(e)}
        emit("done")
        return {"ok": True, "result": _trim(result)}

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

    voice_pending: dict[str, int] = {}  # a call waiting for a spoken yes -> the voice turn it was asked in

    async def _live_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        """A tool call made by the live voice model. One that needs a yes runs only after the user says yes out loud
        in a later turn (the voice gate already makes sure that is the owner speaking)."""
        spec = BY_NAME.get(name)
        if spec is None:
            return {"ok": False, "error": f"There is no tool named '{name}'."}
        if needs_approval(spec, args, bool(svc.settings.get("full_control"))):
            key = call_key(ToolCall(id="", name=name, args=args))
            asked = voice_pending.get(key)
            heard = svc.live.heard if svc.live else ""
            if asked is None or svc.live.turns <= asked or not SPOKEN_YES.search(heard) or SPOKEN_NO.search(heard):
                voice_pending.clear()
                voice_pending[key] = svc.live.turns if svc.live else 0
                return {"ok": False, "needs_confirmation": True,
                        "error": f"Not done yet. Ask the user out loud, in one short sentence, to confirm: {describe_call(spec, args)}. "
                                 "If they then say yes, call this tool again with exactly the same arguments; otherwise drop it."}
            voice_pending.clear()
        audit.info("tool call", extra={"fields": {"tool": spec.name, "args": args, "via": "live"}})
        try:
            return {"ok": True, "result": _trim(await _run_tool(spec, args))}
        except (AgentUnavailable, AgentError, ResearchError, ReminderError, ToolFailure, LLMError, LLMNotConfigured) as e:
            audit.warning("tool failed", extra={"fields": {"tool": spec.name, "error": str(e)}})
            return {"ok": False, "error": str(e)}

    def _recent_conversation(max_age_s: float) -> str | None:
        row = svc.db.one("SELECT id FROM conversations WHERE updated_at > ? ORDER BY updated_at DESC LIMIT 1", [time.time() - max_age_s])
        return row["id"] if row else None

    def _live_prompt() -> str:
        conv = svc.live.conversation_id if svc.live else None
        context = _situation(conv or "")
        context["voice"] = True
        prompt = build_system_prompt(svc.settings.public(), svc.memory.core(), [], tools_enabled=True, context=context)
        if conv:  # a continued conversation: Gemini starts each session blank, so give it what was said
            rows = svc.db.all("SELECT role, content FROM messages WHERE conversation_id=? AND role IN ('user','model') "
                              "ORDER BY created_at DESC LIMIT 12", [conv])
            lines = [f"{'User' if r['role'] == 'user' else 'You'}: {r['content'][:400]}" for r in reversed(rows)]
            if lines:
                prompt += ("\n\nThis conversation so far (continue it; do not greet again or repeat yourself):\n" + "\n".join(lines))
        return prompt

    def _live_save(conv_id: str | None, user: str, reply: str, label: str | None = None) -> str:
        now = time.time()
        if conv_id is None:
            conv_id = uuid.uuid4().hex
            title = ("Voice: " + (label or (user or reply).splitlines()[0]))[:60]
            svc.db.run("INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)", [conv_id, title, now, now])
        if user:
            svc.db.run("INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?,?,?,?,?)",
                       [uuid.uuid4().hex, conv_id, "user", user, now])
        if reply:
            svc.db.run("INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?,?,?,?,?)",
                       [uuid.uuid4().hex, conv_id, "model", reply, now + 0.001])
        svc.db.run("UPDATE conversations SET updated_at=? WHERE id=?", [now, conv_id])
        return conv_id

    async def _go_offline(user: str, acted: bool, origin: str) -> None:
        """Live voice could not keep up: carry on with the offline voice (Parakeet hears, Piper speaks)."""
        try:
            await svc.bridge.call("voice_set", {"enabled": True, "mode": "local"}, timeout=30)
        except (AgentUnavailable, AgentError) as e:
            log.warning("could not switch the agent to the offline voice: %s", e)
        redo = bool(user) and not acted  # an action already taken is not repeated
        notice = "My connection's too slow for live voice right now, so I've switched to my offline voice."
        if redo:
            notice += " One moment."
        try:
            await svc.bridge.call("speak", {"text": notice}, timeout=60)
        except (AgentUnavailable, AgentError) as e:
            log.warning("could not announce the offline voice: %s", e)
        if redo:  # answer what was asked, this time offline
            svc.bridge.voice_event({"type": "voice", "event": "transcript", "text": user})
        elif origin == "briefing":
            status, _report = await _briefing_facts()
            text = briefing.spoken(status, svc.reminders.pending(), (svc.settings.get("address") or "sir").strip(), datetime.now())
            svc.bridge.voice_event({"type": "voice", "event": "briefing", "text": text})
            try:
                await svc.bridge.call("speak", {"text": text, "queue": True}, timeout=120)
            except (AgentUnavailable, AgentError) as e:
                log.warning("briefing: offline voice failed too: %s", e)
        await asyncio.sleep(OFFLINE_FOR_S)
        if time.time() >= svc.offline_voice_until and svc.voice_mode() == "live" and svc.settings.get("voice_enabled"):
            try:  # time to give Live another chance
                await svc.bridge.call("voice_set", {"enabled": True, "mode": "live"}, timeout=30)
                log.info("trying live voice again")
            except (AgentUnavailable, AgentError):
                pass

    def _live_too_slow(reason: str, user: str, acted: bool, origin: str) -> None:
        svc.offline_voice_until = time.time() + OFFLINE_FOR_S
        log.warning("switching to the offline voice for %d minutes: %s", OFFLINE_FOR_S // 60, reason)
        spawn(_go_offline(user, acted, origin))

    async def _say(text: str) -> None:
        """Say something unprompted, unless a live conversation is talking (it would talk over it)."""
        if not svc.settings.get("voice_enabled") or (svc.live and svc.live.active):
            return
        try:
            await svc.bridge.call("speak", {"text": text, "queue": True}, timeout=60)
        except (AgentUnavailable, AgentError) as e:
            log.warning("could not say %r: %s", text[:40], e)

    def _on_agent_event(msg: dict[str, Any]) -> None:
        if msg.get("event") == "download_done":
            item = {k: msg.get(k) for k in ("name", "path", "size_kb", "program", "finished")}
            svc.recent_downloads.append(item)
            del svc.recent_downloads[:-20]
            log.info("download finished", extra={"fields": {"size_kb": item["size_kb"], "program": item["program"]}})
            name = re.sub(r"[_\-]+", " ", Path(str(item["name"])).stem)[:60]
            line = f"Your download {name} has finished."
            if item["program"]:
                line += " It's a program, so I won't open it unless you ask."
            spawn(_say(line))

    svc.agent_event = _on_agent_event

    svc.live = LiveVoice(
        api_key=lambda: svc.settings.api_key("gemini"),
        push_agent=svc.bridge.push,
        call_agent=svc.bridge.call,
        emit=lambda e: svc.bridge.voice_event({"type": "voice", **e}),
        system_prompt=_live_prompt,
        tools=TOOL_SCHEMAS,
        run_tool=_live_tool,
        save_turn=_live_save,
        after_turn=lambda user, reply, conv: spawn(_remember(user, reply, svc.memory.core(), conv)),
        recent_conversation=_recent_conversation,
        on_slow=_live_too_slow,
    )

    briefing_state = {"done": False}

    async def _briefing_facts() -> tuple[dict[str, Any] | None, str]:
        status = None
        for _ in range(30):  # the agent connects a moment after the window opens
            if svc.bridge.connected:
                break
            await asyncio.sleep(0.5)
        if svc.bridge.connected:
            try:
                status = await svc.bridge.call("system_status", timeout=15)
            except (AgentUnavailable, AgentError) as e:
                log.warning("briefing: no system status: %s", e)
        last = svc.db.one("SELECT updated_at FROM conversations ORDER BY updated_at DESC LIMIT 1")
        last_used = None
        if last:
            when = datetime.fromtimestamp(last["updated_at"])
            last_used = ("today at " if when.date() == datetime.now().date() else when.strftime("%A %d %B at ")) + when.strftime("%H:%M")
        try:
            quota = await svc.llm.quota_view()
        except Exception:  # noqa: BLE001 - the quota line is optional
            quota = None
        return status, briefing.facts(status, svc.reminders.pending(), svc.monitor.recent(time.time() - 86400), quota, last_used, datetime.now())

    @r.post("/briefing")
    async def start_briefing(force: bool = False) -> dict[str, Any]:
        """The start-up briefing, spoken. Runs once per launch (the window may reload); force repeats it."""
        if briefing_state["done"] and not force:
            return {"started": False, "reason": "already given"}
        if not svc.settings.get("startup_briefing") and not force:
            return {"started": False, "reason": "turned off"}
        briefing_state["done"] = True
        status, report = await _briefing_facts()
        address = (svc.settings.get("address") or "sir").strip()
        now = datetime.now()
        if svc.live.active:
            return {"started": False, "reason": "a voice conversation is already open"}
        if svc.voice_mode() == "live":
            try:
                await svc.live.start("briefing", opening=briefing.opening_prompt(report, address, now), label="Start-up briefing", wait=True)
                return {"started": True, "mode": "live"}
            except LiveError as e:
                log.warning("briefing: live voice unavailable, using the offline voice: %s", e)
        text = briefing.spoken(status, svc.reminders.pending(), address, now)
        svc.bridge.voice_event({"type": "voice", "event": "briefing", "text": text})
        try:
            await svc.bridge.call("speak", {"text": text}, timeout=120)
        except (AgentUnavailable, AgentError) as e:
            return {"started": True, "mode": "text", "text": text, "error": str(e)}
        return {"started": True, "mode": "local", "text": text}

    @r.get("/voice/events")
    async def voice_events(after: int = 0) -> list[dict[str, Any]]:
        return await svc.bridge.wait_voice(after)

    @r.get("/voice/seq")
    async def voice_seq() -> dict[str, int]:
        """Where the event stream is now, so a freshly opened window only reacts to new events."""
        return {"seq": svc.bridge.voice_seq}

    @r.post("/voice/listen")
    async def voice_listen() -> dict[str, Any]:
        """Push-to-talk: open a live conversation (or, in local mode, take one request) without the wake word.
        Pressing it again during a live conversation ends it."""
        if svc.voice_mode() == "live":
            if svc.live.active:
                await svc.live.stop()
                return {"live": False}
            try:
                return await svc.live.start("button")
            except LiveError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        try:
            return await svc.bridge.call("listen_now", timeout=20)
        except (AgentUnavailable, AgentError) as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

    @r.post("/voice/mode")
    async def voice_mode(body: VoiceModeIn) -> dict[str, Any]:
        svc.settings.update_profile({"voice_mode": body.mode})
        svc.offline_voice_until = 0.0  # the user chose: try it now
        if svc.settings.get("voice_enabled"):
            try:
                await svc.bridge.call("voice_set", {"enabled": True, "mode": body.mode}, timeout=30)
            except (AgentUnavailable, AgentError):
                pass
        return {"mode": body.mode}

    @r.get("/voice")
    async def voice_status() -> dict[str, Any]:
        try:
            return await svc.bridge.call("voice_status", timeout=20)
        except (AgentUnavailable, AgentError) as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

    @r.post("/voice")
    async def voice_toggle(body: VoiceIn) -> dict[str, Any]:
        try:
            state = await svc.bridge.call("voice_set", {"enabled": body.enabled, "mode": svc.voice_mode()}, timeout=30)
        except (AgentUnavailable, AgentError) as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        svc.settings.update_profile({"voice_enabled": bool(state.get("enabled"))})
        return state

    @r.post("/voice/enroll")
    async def voice_enroll() -> dict[str, Any]:
        """Record the user reading aloud for 15 s and make their voiceprint: then only their voice wakes JARVIS."""
        if svc.live and svc.live.active:
            await svc.live.stop()
        try:
            return await svc.bridge.call("voice_enroll", {"seconds": 15}, timeout=45)
        except (AgentUnavailable, AgentError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @r.delete("/voice/enroll")
    async def voice_forget() -> dict[str, Any]:
        try:
            return await svc.bridge.call("voice_forget", timeout=10)
        except (AgentUnavailable, AgentError) as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

    @r.get("/apikeys")
    async def api_keys() -> dict[str, Any]:
        """Which public APIs have a saved key (never the keys themselves), and the catalog size."""
        apis = public_apis.catalog()
        return {"saved": svc.settings.service_keys(), "catalog": len(apis), "need_key": sorted({a["name"] for a in apis if a["auth"] == "apiKey"})}

    @r.put("/apikeys")
    async def save_api_key(body: ServiceKeyIn) -> dict[str, Any]:
        if not public_apis.lookup(body.service):
            raise HTTPException(status_code=400, detail=f"'{body.service}' is not in the public APIs list.")
        svc.settings.set_service_key(body.service, body.key.strip())
        return {"saved": svc.settings.service_keys()}

    @r.delete("/apikeys/{service}")
    async def delete_api_key(service: str) -> dict[str, Any]:
        svc.settings.set_service_key(service, "")
        return {"saved": svc.settings.service_keys()}

    @r.post("/voice/speak")
    async def voice_speak(body: SpeakIn) -> dict[str, Any]:
        try:
            return await svc.bridge.call("speak", {"text": body.text, "queue": body.queue}, timeout=120)
        except (AgentUnavailable, AgentError) as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

    @r.post("/voice/stop")
    async def voice_stop() -> dict[str, Any]:
        if svc.live.active:
            await svc.live.stop()
            return {"stopped": True}
        try:
            return await svc.bridge.call("stop_speaking", timeout=10)
        except (AgentUnavailable, AgentError) as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

    @r.get("/quota")
    async def quota() -> dict[str, Any]:
        """Today's free-tier usage per model as JARVIS counted it (no provider reports this itself)."""
        return await svc.llm.quota_view()

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
            reply = ""
            actions: list[dict[str, Any]] = []
            reply_id = uuid.uuid4().hex
            queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
            agent_task: asyncio.Task | None = None
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
                context = _situation(conv_id)
                context["voice"] = body.voice
                system = build_system_prompt(svc.settings.public(), core, relevant, tools_enabled=True, context=context)
                history = [(m["role"], m["content"]) for m in prior] + [("user", text)]

                done_calls: dict[str, tuple[int, int, dict[str, Any]]] = {}

                async def execute(call: ToolCall) -> dict[str, Any]:
                    step = len(actions)
                    if (cached := repeat_of(call, done_calls, step)) is not None:
                        audit.info("repeated call skipped", extra={"fields": {"tool": call.name}})
                        return cached
                    out = await _execute(call, queue, actions)
                    key = call_key(call)
                    done_calls[key] = (len(actions), done_calls.get(key, (0, 0, {}))[1] + 1, out)
                    return out

                agent_task = asyncio.create_task(
                    svc.llm.run_agent(system, history, TOOL_SCHEMAS, execute,
                                      on_notice=lambda m: queue.put_nowait(("notice", {"message": m})))
                )
                while True:
                    getter = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait({getter, agent_task}, return_when=asyncio.FIRST_COMPLETED)
                    if getter in done:
                        yield _sse(*getter.result())
                        continue
                    getter.cancel()
                    while not queue.empty():
                        yield _sse(*queue.get_nowait())
                    break
                try:
                    reply = agent_task.result()
                except AgentIncomplete as e:
                    # The actions really happened; say exactly what was done instead of an empty reply.
                    done = [a["label"] for a in actions if a["status"] == "done"]
                    failed = [f'{a["label"]} ({a.get("error", "failed")})' for a in actions if a["status"] == "failed"]
                    lines = []
                    if done:
                        lines.append("Done: " + "; ".join(done) + ".")
                    if failed:
                        lines.append("Failed: " + "; ".join(failed) + ".")
                    lines.append(f"I couldn't compose my usual reply afterwards: {e}")
                    reply = " ".join(lines)
                    yield _sse("delta", {"text": reply})
                except ToolsUnsupported:
                    yield _sse("notice", {"message": "This model can't use tools, so I can only talk, not act. Choose another model in Settings for actions."})
                    plain = build_system_prompt(svc.settings.public(), core, relevant, tools_enabled=False, context=context)
                    parts: list[str] = []
                    async for chunk in svc.llm.stream_chat(plain, history):
                        parts.append(chunk)
                        yield _sse("delta", {"text": chunk})
                    reply = "".join(parts)
                else:
                    if reply:
                        yield _sse("delta", {"text": reply})
                yield _sse("done", {"reply_id": reply_id})
                if reply:
                    spawn(_remember(text, reply, core + relevant, conv_id))
            except (LLMError, LLMNotConfigured) as e:
                yield _sse("error", {"message": str(e)})
            finally:
                if agent_task and not agent_task.done():
                    agent_task.cancel()  # user pressed Stop or closed the window
                for fut in list(pending_confirmations.values()):
                    if not fut.done():
                        fut.set_result(False)
                if reply or actions:
                    svc.db.run(
                        "INSERT INTO messages (id, conversation_id, role, content, created_at, actions) VALUES (?,?,?,?,?,?)",
                        [reply_id, conv_id, "model", reply or "(stopped)", time.time(), json.dumps(actions) if actions else None],
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
