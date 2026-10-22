"""Actions the backend can ask the agent to run.

Every action returns a JSON-serialisable result or raises ActionError with the
real reason it failed. Nothing here may swallow an error and report success.
Automation actions (apps, volume, files, ...) are registered here in milestone 3.
"""
from __future__ import annotations

import getpass
import os
import platform
import time
from typing import Any, Callable

from . import __version__


class ActionError(Exception):
    """Raised with a human-readable, truthful failure reason."""


def ping(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "pong": True,
        "agent_version": __version__,
        "pid": os.getpid(),
        "user": getpass.getuser(),
        "host": platform.node(),
        "os": platform.platform(),
        "time": time.time(),
    }


REGISTRY: dict[str, Callable[[dict[str, Any]], Any]] = {
    "ping": ping,
}


def run(action: str, args: dict[str, Any]) -> Any:
    fn = REGISTRY.get(action)
    if fn is None:
        raise ActionError(f"Unknown action '{action}'.")
    return fn(args)
