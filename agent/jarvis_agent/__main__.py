"""Desktop agent: connects to the backend, heartbeats, and executes OS actions."""
from __future__ import annotations

import asyncio
import ctypes
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
from . import extras, voice, win
from .actions import attach_live
from .live_audio import LiveAudio
from .actions import SCREENSHOT_TTL_S, ActionError, purge_old_screenshots, run, warm_app_index

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
    out.update(_presence())
    return out


_meter: "win.PlaybackMeter | None" = None


def _presence() -> dict:
    """Whether someone is at the PC: seconds since the last mouse or keyboard input, whether the session is
    locked, and whether sound is playing (watching a video is not being away). No content is read."""
    global _meter
    user32 = ctypes.windll.user32

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    idle = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000 if user32.GetLastInputInfo(ctypes.byref(info)) else None
    desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP: fails or cannot switch while locked
    locked = not desk or not user32.SwitchDesktop(desk)
    if desk:
        user32.CloseDesktop(desk)
    if _meter is None:
        _meter = win.PlaybackMeter()
    return {"idle_s": round(idle, 1) if idle is not None else None, "locked": bool(locked), "audio_playing": _meter.peak() > 0.02}


# Voice events come from worker threads; they wait here until a backend connection can send them.
_outbox: asyncio.Queue | None = None


def emit_voice(event: dict) -> None:
    send_backend({"type": "voice", **event})


def send_backend(msg: dict) -> None:
    """Queue a message for the backend from any thread (voice events, live microphone frames)."""
    loop, box = _loop, _outbox
    if loop is not None and box is not None:
        loop.call_soon_threadsafe(_offer, box, msg)


def _offer(box: asyncio.Queue, msg: dict) -> None:
    try:
        box.put_nowait(msg)
    except asyncio.QueueFull:
        pass  # disconnected for a while: drop rather than build up stale audio


_loop: asyncio.AbstractEventLoop | None = None
live: LiveAudio  # set in amain once the event loop exists


async def _drain_outbox(ws) -> None:
    while True:
        msg = await _outbox.get()  # type: ignore[union-attr]
        await ws.send(json.dumps(msg))


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
        drain = asyncio.create_task(_drain_outbox(ws))
        tasks: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "speaker":
                    live.play(msg.get("pcm", ""))
                    continue
                if msg.get("type") == "speaker_flush":
                    live.flush()
                    continue
                if msg.get("type") == "call":
                    t = asyncio.create_task(_handle_call(ws, msg, log))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        finally:
            hb.cancel()
            drain.cancel()
            for t in tasks:
                t.cancel()


async def _purge_screenshots(log: logging.Logger) -> None:
    while True:
        try:
            n = await asyncio.to_thread(purge_old_screenshots)
            if n:
                log.info("deleted %d screenshot(s) older than %ds", n, SCREENSHOT_TTL_S)
        except Exception:  # noqa: BLE001 - a failed sweep must not stop the agent
            log.exception("screenshot cleanup failed")
        await asyncio.sleep(30)


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

    try:
        # Small and mostly idle, but it must answer promptly when a heavy app pins every core.
        psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
    except psutil.Error as e:
        log.warning("could not raise agent priority: %s", e)
    asyncio.get_running_loop().run_in_executor(None, warm_app_index)
    asyncio.create_task(_purge_screenshots(log))
    global _loop, _outbox
    _loop, _outbox = asyncio.get_running_loop(), asyncio.Queue(maxsize=200)
    voice.attach(emit_voice)
    extras.DownloadWatcher(lambda e: send_backend({"type": "event", **e})).start()
    extras.EmergencyHotkey(lambda e: send_backend({"type": "event", **e})).start()
    global live
    live = attach_live(send_backend)
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
