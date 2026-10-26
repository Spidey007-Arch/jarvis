"""Request/response channel between the backend and the desktop agent."""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from fastapi import WebSocket

HEARTBEAT_TIMEOUT_S = 15.0


class AgentUnavailable(Exception):
    """The desktop agent is not connected (or stopped answering)."""


class AgentError(Exception):
    """The agent ran the action and reported a failure; message is the real reason."""


class AgentBridge:
    def __init__(self) -> None:
        self.ws: WebSocket | None = None
        self.last_heartbeat: float = 0.0
        self.info: dict[str, Any] = {}
        self.stats: dict[str, Any] = {}
        self._pending: dict[str, asyncio.Future] = {}
        # Voice events from the agent (wake word heard, transcript, speaking, ...), numbered for long-polling.
        self.voice_events: list[dict[str, Any]] = []
        self._voice_seq = 0
        self._voice_changed = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self.ws is not None and (time.monotonic() - self.last_heartbeat) < HEARTBEAT_TIMEOUT_S

    @property
    def heartbeat_age(self) -> float | None:
        return None if self.last_heartbeat == 0.0 else round(time.monotonic() - self.last_heartbeat, 2)

    def attach(self, ws: WebSocket, hello: dict[str, Any]) -> None:
        self.ws = ws
        self.info = hello
        self.last_heartbeat = time.monotonic()

    def detach(self, ws: WebSocket) -> None:
        if self.ws is ws:
            self.ws = None
            self.last_heartbeat = 0.0
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AgentUnavailable("The agent disconnected before answering."))
            self._pending.clear()

    def heartbeat(self, stats: dict[str, Any] | None = None) -> None:
        self.last_heartbeat = time.monotonic()
        if stats:
            self.stats = stats

    def voice_event(self, msg: dict[str, Any]) -> None:
        self._voice_seq += 1
        self.voice_events = (self.voice_events + [{**{k: v for k, v in msg.items() if k != "type"}, "seq": self._voice_seq, "at": time.time()}])[-200:]
        self._voice_changed.set()
        self._voice_changed = asyncio.Event()

    @property
    def voice_seq(self) -> int:
        return self._voice_seq

    async def wait_voice(self, after: int, timeout: float = 25.0) -> list[dict[str, Any]]:
        """Events newer than `after`, waiting up to `timeout` for one to arrive (long-poll)."""
        fresh = [e for e in self.voice_events if e["seq"] > after]
        if fresh or timeout <= 0:
            return fresh
        try:
            await asyncio.wait_for(self._voice_changed.wait(), timeout)
        except asyncio.TimeoutError:
            return []
        return [e for e in self.voice_events if e["seq"] > after]

    def resolve(self, msg: dict[str, Any]) -> None:
        fut = self._pending.get(msg.get("id", ""))
        if fut is None or fut.done():
            return
        if msg.get("ok"):
            fut.set_result(msg.get("result"))
        else:
            fut.set_exception(AgentError(msg.get("error") or "The agent reported an unspecified failure."))

    async def push(self, msg: dict[str, Any]) -> None:
        """One-way message to the agent (live audio to play); dropped if the agent is not connected."""
        ws = self.ws
        if ws is not None and self.connected:
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001 - the agent is reconnecting; audio can be lost
                pass

    async def call(self, action: str, args: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
        ws = self.ws
        if ws is None or not self.connected:
            raise AgentUnavailable("The desktop agent is not connected.")
        call_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        try:
            await ws.send_json({"type": "call", "id": call_id, "action": action, "args": args or {}})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            raise AgentUnavailable(f"The agent did not answer '{action}' within {timeout:.0f}s.") from e
        finally:
            self._pending.pop(call_id, None)
