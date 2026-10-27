"""The local AI gateway: start it in the background when it is chosen in Settings or needed as a backup.

The gateway is a separate OpenAI-compatible server on this PC (OmniRoute by default). JARVIS starts it with
its own command, hidden, as a daemon that keeps running on its own. A cold start takes a few minutes: the
port opens first, then the first request warms the server up (over a minute), after which it answers in a
second or two."""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger("jarvis.gateway")

COMMAND = "omniroute"
START_ARGS = ["serve", "--daemon", "--no-open"]
PORT_WAIT_S = 90.0
WARM_UP_S = 180.0
_CREATE_NO_WINDOW = 0x08000000
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: outlives JARVIS

_starting: asyncio.Lock | None = None
_background: asyncio.Task | None = None


async def reachable(base_url: str, timeout: float = 5.0) -> bool:
    """Whether the gateway answers at the base URL (any answer below 500, a login prompt included)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
            r = await c.get(base_url.rstrip("/") + "/models")
        return r.status_code < 500
    except httpx.HTTPError:
        return False


async def _port_open(base_url: str) -> bool:
    u = urlparse(base_url)
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(u.hostname or "127.0.0.1", u.port or 80), 1.5)
    except (OSError, asyncio.TimeoutError):
        return False
    w.close()
    return True


def installed() -> str | None:
    return shutil.which(COMMAND)


async def ensure(base_url: str) -> dict[str, Any]:
    """Make sure the gateway is running: start it in the background if it isn't, then wait until it answers."""
    global _starting
    if await reachable(base_url):
        return {"running": True, "started": False}
    exe = installed()
    if not exe:
        return {"running": False, "error": f"The gateway isn't installed ('{COMMAND}' was not found). Install it, or start your own "
                                           "OpenAI-compatible server at this address."}
    _starting = _starting or asyncio.Lock()
    async with _starting:  # two callers at once start it once
        if await reachable(base_url):
            return {"running": True, "started": False}
        started = False
        if not await _port_open(base_url):
            port = urlparse(base_url).port
            cmd = [exe, *START_ARGS, *(["--port", str(port)] if port else [])]
            log.info("starting the gateway", extra={"fields": {"port": port}})
            try:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=_CREATE_NO_WINDOW | _DETACHED, close_fds=True)
            except OSError as e:
                return {"running": False, "error": f"Could not start the gateway: {e}"}
            started = True
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PORT_WAIT_S
            while not await _port_open(base_url):
                if loop.time() > deadline:
                    return {"running": False, "error": f"The gateway was started but didn't open its port within {PORT_WAIT_S:.0f} s."}
                await asyncio.sleep(1.0)
        # The port is open; the first request can take over a minute while the server warms up.
        if await reachable(base_url, timeout=WARM_UP_S):
            log.info("gateway is up", extra={"fields": {"started": started}})
            return {"running": True, "started": started}
    return {"running": False, "error": f"The gateway is running but didn't answer within {WARM_UP_S / 60:.0f} minutes."}


def start_soon(base_url: str) -> None:
    """Start the gateway in the background without waiting (used when it is needed as a backup mid-reply)."""
    global _background
    if _background is None or _background.done():
        _background = asyncio.get_running_loop().create_task(ensure(base_url))
