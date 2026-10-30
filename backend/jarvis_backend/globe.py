"""God's Eye View, the live 3D globe (apps/gods-eye-view, MIT, by Bilawal Sidhu), run inside JARVIS.

It is a separate web app kept unchanged in its own sub-repository. JARVIS runs its server hidden, on this PC
only (127.0.0.1), with the Node 24 it needs from JARVIS's own runtime folder, and shows it in the Globe screen.
Its MCP server becomes a connector, so JARVIS can ask it about flights, satellites, earthquakes and more.
The server stops when JARVIS does."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("jarvis.globe")

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "apps" / "gods-eye-view"
PORT = 4173
URL = f"http://127.0.0.1:{PORT}"
START_TIMEOUT_S = 240.0
_CREATE_NO_WINDOW = 0x08000000

_proc: subprocess.Popen | None = None
_lock: asyncio.Lock | None = None


def node_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JARVIS" / "runtime" / "node24"


def state() -> dict[str, Any]:
    """What is in place: the app, its packages, and Node 24."""
    return {"app": APP_DIR.joinpath("package.json").is_file(), "packages": APP_DIR.joinpath("node_modules").is_dir(),
            "node": node_dir().joinpath("node.exe").is_file(), "url": URL}


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = str(node_dir()) + os.pathsep + env.get("PATH", "")
    env["HOST"] = "127.0.0.1"  # this PC only: the app brokers any keys it holds to whoever can reach it
    env["BROWSER"] = "none"  # it shows inside JARVIS, not in a browser window
    # The app refuses to be shown inside other pages; its supported switch lets JARVIS's own window (and only it)
    # show the embed-mode page. Every other page, and its settings page, stay unframable.
    env["GEV_EMBED_FRAME_ANCESTORS"] = "http://localhost:1420 http://tauri.localhost https://tauri.localhost tauri://localhost"
    return env


async def reachable(timeout: float = 4.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
            r = await c.get(URL + "/")
        return r.status_code < 500
    except httpx.HTTPError:
        return False


async def ensure() -> dict[str, Any]:
    """Start the globe's server if it isn't running, and wait until it answers."""
    global _proc, _lock
    have = state()
    if not (have["app"] and have["packages"] and have["node"]):
        missing = [k for k in ("app", "packages", "node") if not have[k]]
        return {"running": False, "error": "God's Eye View isn't installed yet (missing: " + ", ".join(missing) + ")."}
    if await reachable():
        return {"running": True, "url": URL}
    _lock = _lock or asyncio.Lock()
    async with _lock:
        if await reachable():
            return {"running": True, "url": URL}
        if _proc is None or _proc.poll() is not None:
            logs = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JARVIS" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            out = open(logs / "globe.log", "ab")  # noqa: SIM115 - the server writes to it for as long as it runs
            cmd = [str(node_dir() / "node.exe"), str(APP_DIR / "node_modules" / "vite" / "bin" / "vite.js"),
                   "--host", "127.0.0.1", "--port", str(PORT), "--strictPort"]
            log.info("starting the globe")
            _proc = subprocess.Popen(cmd, cwd=str(APP_DIR), env=_env(), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                     creationflags=_CREATE_NO_WINDOW)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_TIMEOUT_S
        while loop.time() < deadline:
            if _proc.poll() is not None:
                return {"running": False, "error": f"The globe's server stopped (exit code {_proc.returncode}); see logs/globe.log."}
            if await reachable():
                log.info("globe is up")
                return {"running": True, "url": URL}
            await asyncio.sleep(1.0)
    return {"running": False, "error": "The globe's server didn't answer within 4 minutes; see logs/globe.log."}


def connector_config() -> dict[str, Any]:
    """The globe's MCP server as a JARVIS connector (stdio, reading from the running app)."""
    return {"name": "globe", "kind": "local", "enabled": True, "command": str(node_dir() / "node.exe"),
            "args": [str(APP_DIR / "server" / "mcp" / "stdio.js"), "--api-base", URL], "url": ""}


def stop() -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        try:  # vite starts helper processes; end the whole tree
            subprocess.run(["taskkill", "/PID", str(_proc.pid), "/T", "/F"], capture_output=True, timeout=10,
                           creationflags=_CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            _proc.kill()
    _proc = None


_LINK = re.compile(r"https?://(?:127\.0\.0\.1|localhost):%d/[^\s\"'<>)]*" % PORT)


def view_link(text: str) -> str | None:
    """The God's Eye View link in a tool's answer (a view with camera and layers), on JARVIS's own address."""
    m = _LINK.search(text or "")
    if not m:
        return None
    link = m.group(0).replace("localhost", "127.0.0.1", 1)
    base, _, view = link.partition("#")
    return f"{URL}/?embed=1" + (f"#{view}" if view else "")  # the embed page is the one JARVIS may show


LAYERS = ("flights", "military", "satellites", "ais-live-vessels", "earthquakes", "fire-perimeters", "local-firms", "weather-radar",
          "weather-satellite", "weather-cyclones", "weather-lightning", "wind", "air-quality", "traffic", "transit", "cctv",
          "rocket-launches", "telegeography-submarine-cables", "recent-imagery", "street-level")


async def geocode(place: str) -> dict[str, Any]:
    """A place name to its bounding box, from OpenStreetMap (no key; the globe's own place search needs one)."""
    async with httpx.AsyncClient(timeout=15, trust_env=False, headers={"User-Agent": "JARVIS desktop assistant"}) as c:
        r = await c.get("https://nominatim.openstreetmap.org/search", params={"q": place, "format": "jsonv2", "limit": 1})
    hits = r.json() if r.status_code == 200 else []
    if not hits:
        raise ValueError(f"I couldn't find '{place}' on the map.")
    south, north, west, east = (float(x) for x in hits[0]["boundingbox"])
    return {"name": hits[0].get("display_name", place)[:120], "bbox": [west, south, east, north],
            "lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"])}
