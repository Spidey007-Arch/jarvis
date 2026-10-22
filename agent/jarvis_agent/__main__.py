"""Desktop agent: connects to the backend, heartbeats, and executes OS actions."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sys
import time

import psutil
import websockets
from jarvis_common import logs

from . import __version__
from .actions import ActionError, run

HEARTBEAT_INTERVAL_S = 5.0
MAX_BACKOFF_S = 10.0


_last_net: tuple[float, int, int] | None = None


def _stats() -> dict:
    """Live readings sent with every heartbeat; the UI shows these as-is."""
    global _last_net
    vm = psutil.virtual_memory()
    out: dict = {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": vm.percent,
        "ram_used_gb": round(vm.used / 1024**3, 1),
        "ram_total_gb": round(vm.total / 1024**3, 1),
    }
    try:
        drive = os.environ.get("SystemDrive", "C:") + "\\"
        out["disk_percent"] = psutil.disk_usage(drive).percent
    except OSError:
        out["disk_percent"] = None
    battery = psutil.sensors_battery()
    out["battery_percent"] = None if battery is None else round(battery.percent)
    out["battery_plugged"] = None if battery is None else battery.power_plugged
    net = psutil.net_io_counters()
    now = time.monotonic()
    if _last_net is not None and now > _last_net[0]:
        dt = now - _last_net[0]
        out["net_down_kbps"] = round((net.bytes_recv - _last_net[1]) / 1024 / dt, 1)
        out["net_up_kbps"] = round((net.bytes_sent - _last_net[2]) / 1024 / dt, 1)
    _last_net = (now, net.bytes_recv, net.bytes_sent)
    return out


async def _heartbeat(ws) -> None:
    while True:
        await ws.send(json.dumps({"type": "heartbeat", "stats": _stats()}))
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


async def _handle_call(ws, msg: dict, log: logging.Logger) -> None:
    call_id, action, args = msg.get("id"), msg.get("action", ""), msg.get("args") or {}
    audit = logging.getLogger("jarvis.automation")
    try:
        # Actions are blocking Win32/OS calls; keep the event loop (and heartbeats) free.
        result = await asyncio.to_thread(run, action, args)
        audit.info("action ok", extra={"fields": {"action": action}})
        reply = {"type": "result", "id": call_id, "ok": True, "result": result}
    except ActionError as e:
        audit.warning("action failed", extra={"fields": {"action": action, "error": str(e)}})
        reply = {"type": "result", "id": call_id, "ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - report the real failure instead of dying
        log.exception("action crashed: %s", action)
        reply = {"type": "result", "id": call_id, "ok": False, "error": f"{type(e).__name__}: {e}"}
    await ws.send(json.dumps(reply, default=str))


async def _session(url: str, token: str, log: logging.Logger) -> None:
    # Token goes in a header, never the URL: URLs end up in access logs.
    async with websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=10,
        ping_interval=None,
        max_size=16 * 1024 * 1024,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "info": {"version": __version__, "pid": os.getpid(), "os": platform.platform()},
                }
            )
        )
        log.info("connected to backend")
        hb = asyncio.create_task(_heartbeat(ws))
        tasks: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "call":
                    t = asyncio.create_task(_handle_call(ws, msg, log))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        finally:
            hb.cancel()
            for t in tasks:
                t.cancel()


async def _watch_parent(parent_pid: int, log: logging.Logger) -> None:
    while True:
        await asyncio.sleep(2)
        try:
            alive = psutil.Process(parent_pid).is_running()
        except psutil.Error:
            alive = False
        if not alive:
            log.warning("shell process %s is gone; agent exiting", parent_pid)
            os._exit(0)


async def amain() -> int:
    log = logs.setup("agent")
    port = os.environ.get("JARVIS_PORT")
    token = os.environ.get("JARVIS_TOKEN")
    if not port or not token:
        log.error("JARVIS_PORT and JARVIS_TOKEN must be set by the launcher")
        return 2
    parent = int(os.environ.get("JARVIS_PARENT_PID", "0") or 0)
    if parent:
        asyncio.create_task(_watch_parent(parent, log))

    psutil.cpu_percent(interval=None)  # prime the counter so the first reading is meaningful
    url = f"ws://127.0.0.1:{port}/ws/agent"
    backoff = 1.0
    while True:
        try:
            await _session(url, token, log)
            backoff = 1.0
            log.info("backend closed the connection; reconnecting")
        except (OSError, websockets.WebSocketException) as e:
            log.warning("connection failed: %s; retrying in %.0fs", e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_S)


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
