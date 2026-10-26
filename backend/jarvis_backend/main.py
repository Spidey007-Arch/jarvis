"""FastAPI application: health, agent channel, and (later milestones) chat + memory."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import psutil
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jarvis_common.paths import data_dir, logs_dir, runtime_file

from . import __version__
from .api import Services, build_router
from .bridge import AgentBridge, AgentError, AgentUnavailable, HEARTBEAT_TIMEOUT_S
from .db import DB
from .usage import QuotaTracker
from .llm import LLM
from .memory import MemoryStore
from .monitor import Monitor
from .skills import Skills
from .tasks import Tasks
from .reminders import Reminders
from .settings import SettingsStore

log = logging.getLogger("jarvis.backend")


def _write_runtime(port: int, token: str) -> None:
    """Publish port/token/pid atomically so the shell (or a second launch) can find us."""
    target = runtime_file()
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": port, "token": token, "pid": os.getpid()}), encoding="utf-8")
    os.replace(tmp, target)


def _remove_runtime() -> None:
    try:
        data = json.loads(runtime_file().read_text(encoding="utf-8"))
        if data.get("pid") == os.getpid():
            runtime_file().unlink()
    except (OSError, ValueError):
        pass


def create_app(port: int, token: str | None = None) -> FastAPI:
    token = token or secrets.token_urlsafe(32)
    bridge = AgentBridge()
    started = time.monotonic()
    ui_seen: dict[str, float] = {"at": 0.0}  # when the window last checked in (POST /ui/alive)
    parent_pid = int(os.environ.get("JARVIS_PARENT_PID", "0") or 0)

    async def watch_parent(app: FastAPI) -> None:
        """Exit when the shell that launched us is gone, so nothing is left running."""
        if not parent_pid:
            return
        while True:
            await asyncio.sleep(2)
            try:
                alive = psutil.Process(parent_pid).is_running()
            except psutil.Error:
                alive = False
            if not alive:
                log.warning("shell process %s is gone; shutting down", parent_pid)
                app.state.server.should_exit = True
                return

    settings = SettingsStore(data_dir() / "settings.dat")
    llm = LLM(settings)
    db = DB(data_dir() / "jarvis.db")
    llm.usage = QuotaTracker(db)
    llm.usage.prune()
    services = Services(settings=settings, llm=llm, db=db, memory=MemoryStore(db, llm), bridge=bridge, reminders=Reminders(db, bridge),
                        monitor=Monitor(bridge, settings), tasks=Tasks(db), skills=Skills(db))

    async def daily_maintenance() -> None:
        while True:
            try:
                result = services.memory.maintenance()
                log.info("memory maintenance", extra={"fields": result})
            except Exception:  # noqa: BLE001
                log.exception("memory maintenance failed")
            await asyncio.sleep(24 * 3600)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _write_runtime(port, token)
        log.info("backend ready", extra={"fields": {"port": port, "pid": os.getpid(), "version": __version__}})
        watcher = asyncio.create_task(watch_parent(app))
        maintenance = asyncio.create_task(daily_maintenance())
        reminders = asyncio.create_task(services.reminders.run())
        monitor = asyncio.create_task(services.monitor.run())
        initiative = asyncio.create_task(services.initiative.run()) if services.initiative else None
        try:
            yield
        finally:
            watcher.cancel()
            maintenance.cancel()
            reminders.cancel()
            monitor.cancel()
            if initiative:
                initiative.cancel()
            db.close()
            _remove_runtime()
            log.info("backend stopped")

    app = FastAPI(title="JARVIS backend", version=__version__, lifespan=lifespan)
    app.state.token = token
    app.state.bridge = bridge
    app.state.server = None  # set by __main__ once uvicorn.Server exists

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://tauri.localhost",
            "https://tauri.localhost",
            "tauri://localhost",
            "http://localhost:1420",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not supplied or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Missing or invalid token.")

    app.include_router(build_router(services, require_auth))

    @app.exception_handler(AgentUnavailable)
    async def _agent_unavailable(_, exc: AgentUnavailable):
        return JSONResponse(status_code=503, content={"ok": False, "error": "agent_unavailable", "message": str(exc)})

    @app.exception_handler(AgentError)
    async def _agent_error(_, exc: AgentError):
        return JSONResponse(status_code=422, content={"ok": False, "error": "action_failed", "message": str(exc)})

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "service": "jarvis-backend",
            "status": "ok",
            "version": __version__,
            "pid": os.getpid(),
            "uptime_s": round(time.monotonic() - started, 1),
            "agent_connected": bridge.connected,
            "agent_heartbeat_age_s": bridge.heartbeat_age,
            # How long since the window last checked in; the shell reloads a window that stays silent.
            "ui_age_s": round(time.monotonic() - ui_seen["at"], 1) if ui_seen["at"] else None,
        }

    @app.post("/ui/alive", dependencies=[Depends(require_auth)])
    async def ui_alive() -> dict[str, bool]:
        ui_seen["at"] = time.monotonic()
        return {"ok": True}

    @app.post("/ui/error", dependencies=[Depends(require_auth)])
    async def ui_error(body: dict[str, Any]) -> dict[str, bool]:
        """A page error in the window: written to logs/crash.log (keys never reach the window, so none can leak here)."""
        line = json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "message": str(body.get("message", ""))[:500],
                           "stack": str(body.get("stack", ""))[:2000], "where": str(body.get("where", ""))[:100]})
        with open(logs_dir() / "crash.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        log.error("window error: %s", str(body.get("message", ""))[:200])
        return {"ok": True}

    @app.post("/shutdown", dependencies=[Depends(require_auth)])
    async def shutdown() -> dict[str, bool]:
        log.info("shutdown requested over HTTP")
        if app.state.server is not None:
            app.state.server.should_exit = True
        return {"ok": True}

    @app.get("/api/agent", dependencies=[Depends(require_auth)])
    async def agent_info() -> dict[str, Any]:
        return {"connected": bridge.connected, "info": bridge.info, "stats": bridge.stats, "heartbeat_age_s": bridge.heartbeat_age}

    @app.get("/api/logs", dependencies=[Depends(require_auth)])
    async def logs_tail(file: str = "backend", n: int = 40) -> dict[str, Any]:
        if file not in {"backend", "agent", "error", "automation"}:
            raise HTTPException(status_code=400, detail="Unknown log file.")
        n = max(1, min(n, 200))
        path = logs_dir() / f"{file}.log"
        if not path.exists():
            return {"file": file, "lines": []}
        with path.open("rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - 65536))
            text = f.read().decode("utf-8", "replace")
        out: list[dict[str, str]] = []
        for line in [ln for ln in text.splitlines() if ln.strip()][-n:]:
            try:
                j = json.loads(line)
                out.append({"ts": j.get("ts", ""), "level": j.get("level", "INFO"), "msg": j.get("msg", "")})
            except ValueError:
                out.append({"ts": "", "level": "INFO", "msg": line})
        return {"file": file, "lines": out}

    @app.post("/api/agent/ping", dependencies=[Depends(require_auth)])
    async def agent_ping() -> dict[str, Any]:
        """Round-trips a real request through the agent; proves the channel works both ways."""
        t0 = time.perf_counter()
        result = await bridge.call("ping", timeout=5)
        return {"ok": True, "round_trip_ms": round((time.perf_counter() - t0) * 1000, 1), "agent": result}

    async def _start_live(origin: str, said: str | None = None) -> None:
        try:
            await services.live.start(origin, said=said)
        except Exception as e:  # noqa: BLE001
            bridge.voice_event({"type": "voice", "event": "error", "message": str(e)})

    async def _restore_voice() -> None:
        """A restarted agent forgets that the wake word was on; turn it back on."""
        await asyncio.sleep(1.0)
        try:
            state = await bridge.call("voice_set", {"enabled": True, "mode": services.voice_mode()}, timeout=30)
            log.info("voice restored", extra={"fields": {"enabled": state.get("enabled"), "missing": state.get("missing")}})
        except Exception as e:  # noqa: BLE001
            log.warning("could not restore voice: %s", e)

    @app.websocket("/ws/agent")
    async def agent_socket(ws: WebSocket) -> None:
        supplied = (ws.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not supplied or not hmac.compare_digest(supplied, token):
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            await ws.close(code=4400)
            return
        if hello.get("type") != "hello":
            await ws.close(code=4400)
            return
        old = bridge.ws
        bridge.attach(ws, hello.get("info", {}))
        if old is not None:
            try:
                await old.close(code=4000)
            except Exception:  # noqa: BLE001 - old socket may already be dead
                pass
        log.info("agent connected", extra={"fields": {"agent": bridge.info}})
        if settings.get("voice_enabled"):
            asyncio.create_task(_restore_voice())
        try:
            while True:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=HEARTBEAT_TIMEOUT_S)
                kind = msg.get("type")
                if kind == "heartbeat":
                    bridge.heartbeat(msg.get("stats"))
                elif kind == "result":
                    bridge.resolve(msg)
                elif kind == "mic":
                    if services.live:
                        services.live.feed(msg.get("pcm", ""))
                elif kind == "event":  # something the agent noticed on its own (a finished download, ...)
                    if msg.get("event") == "emergency_stop" and services.emergency_stop:
                        asyncio.create_task(services.emergency_stop())
                    elif services.agent_event:
                        services.agent_event(msg)
                elif kind == "voice":
                    bridge.voice_event(msg)
                    if msg.get("event") == "wake" and services.live and services.voice_mode() == "live":
                        asyncio.create_task(_start_live("wake", (msg.get("text") or "").strip() or None))
                    elif msg.get("event") == "barge" and services.live:
                        asyncio.create_task(services.live.barge())
        except asyncio.TimeoutError:
            log.warning("agent heartbeat timed out; dropping connection")
        except WebSocketDisconnect:
            log.info("agent disconnected")
        finally:
            bridge.detach(ws)
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    return app
