"""Anthropic Claude via the Messages API (plain HTTP, no SDK needed)."""
from __future__ import annotations

import base64
import json
from typing import Any, AsyncIterator

import httpx

from .base import (NETWORK_ERROR, NETWORK_EXCEPTIONS, Provider, ProviderError, Step, ToolCall, ToolSession, json_instruction, merge_turns,
                   parse_json_loose, split_system, stub, usage_of, with_context)
from .openai_compat import _error_text, _retry_after

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"
MAX_TOKENS = 4096
# The fixed instructions (and the tool list before them) are the same in every request: cache them for an hour,
# which outlasts the gaps between a person's requests. The growing conversation is cached for the default 5 minutes.
LONG_CACHE = {"type": "ephemeral", "ttl": "1h"}
SHORT_CACHE = {"type": "ephemeral"}


def _usage(j: dict[str, Any]) -> dict[str, int]:
    u = j.get("usage") or {}
    return usage_of(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                    u.get("output_tokens"), u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens"))


def _system(static: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": static, "cache_control": LONG_CACHE}] if static else []


def _mark_last(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A copy of the messages with a cache breakpoint on the last block, so the next step reuses all of it."""
    if not messages:
        return messages
    out = messages[:-1]
    last = dict(messages[-1])
    content = last["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [dict(b) for b in content]
    if blocks:
        blocks[-1] = {**blocks[-1], "cache_control": SHORT_CACHE}
    last["content"] = blocks
    return out + [last]


class AnthropicProvider(Provider):
    id = "anthropic"
    supports_embeddings = False  # Anthropic has no embeddings API; memory falls back to keyword search

    def __init__(self, api_key: str) -> None:
        self.http = httpx.AsyncClient(
            headers={"x-api-key": api_key, "anthropic-version": VERSION, "content-type": "application/json"},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            r = await self.http.get(f"{API}/models", params={"limit": 100})
        except (httpx.HTTPError, *NETWORK_EXCEPTIONS) as e:
            raise ProviderError(f"Could not reach Anthropic: {e}", NETWORK_ERROR) from e
        if r.status_code >= 400:
            raise ProviderError(_error_text(r), r.status_code)
        # API returns newest first; keep that order.
        return [{"name": m["id"], "display_name": m.get("display_name") or m["id"], "kind": "chat"} for m in r.json().get("data", [])]

    def _messages(self, history: list[tuple[str, str]], dynamic: str = "") -> list[dict[str, str]]:
        msgs = [{"role": "assistant" if r == "model" else "user", "content": t} for r, t in with_context(history, dynamic)]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    async def stream_chat(self, model: str, system: str, history: list[tuple[str, str]]) -> AsyncIterator[str]:
        static, dynamic = split_system(system)
        payload = {"model": model, "max_tokens": MAX_TOKENS, "system": _system(static), "messages": self._messages(history, dynamic),
                   "stream": True}
        usage: dict[str, Any] = {}
        try:
            async with self.http.stream("POST", f"{API}/messages", json=payload) as r:
                if r.status_code >= 400:
                    raise ProviderError(_error_text(r, await r.aread()), r.status_code, _retry_after(r))
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        j = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    kind = j.get("type")
                    if kind == "message_start":
                        usage.update((j.get("message") or {}).get("usage") or {})
                    elif kind == "message_delta":
                        usage.update(j.get("usage") or {})
                    if kind == "content_block_delta" and j.get("delta", {}).get("type") == "text_delta":
                        yield j["delta"].get("text", "")
                    elif kind == "error":
                        err = j.get("error", {})
                        status = 529 if err.get("type") == "overloaded_error" else None
                        raise ProviderError(err.get("message", "Anthropic stream error."), status)
                    elif kind == "message_stop":
                        self._usage(model, _usage({"usage": usage}))
                        return
        except (httpx.HTTPError, *NETWORK_EXCEPTIONS) as e:
            raise ProviderError(f"Connection to Anthropic failed: {e}", NETWORK_ERROR) from e

    async def generate_json(self, model: str, prompt: str, schema: dict[str, Any]) -> Any:
        payload = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": prompt + json_instruction(schema)}],
        }
        try:
            r = await self.http.post(f"{API}/messages", json=payload)
        except (httpx.HTTPError, *NETWORK_EXCEPTIONS) as e:
            raise ProviderError(f"Could not reach Anthropic: {e}", NETWORK_ERROR) from e
        if r.status_code >= 400:
            raise ProviderError(_error_text(r), r.status_code, _retry_after(r))
        self._usage(model, _usage(r.json()))
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        return parse_json_loose(text)

    @staticmethod
    def pick_chat(models: list[dict[str, Any]]) -> str:
        names = [m["name"] for m in models]
        sonnet = [n for n in names if "sonnet" in n]
        return (sonnet or names or [""])[0]

    @staticmethod
    def pick_utility(models: list[dict[str, Any]], chat_model: str) -> str:
        haiku = [m["name"] for m in models if "haiku" in m["name"]]
        return haiku[0] if haiku else chat_model

    @staticmethod
    def pick_embed(models: list[dict[str, Any]]) -> str:
        return ""

    def tool_session(self, system: str, history: list[tuple[str, str]], tools: list[dict[str, Any]]) -> ToolSession:
        return _AnthropicTools(self, system, history, tools)

    async def describe_image(self, model: str, prompt: str, png: bytes) -> str:
        payload = {"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(png).decode()}},
            {"type": "text", "text": prompt}]}]}
        try:
            r = await self.http.post(f"{API}/messages", json=payload)
        except (httpx.HTTPError, *NETWORK_EXCEPTIONS) as e:
            raise ProviderError(f"Could not reach Anthropic: {e}", NETWORK_ERROR) from e
        if r.status_code >= 400:
            raise ProviderError(_error_text(r), r.status_code, _retry_after(r))
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


class _AnthropicTools(ToolSession):
    portable = True

    def __init__(self, provider: AnthropicProvider, system: str, history: list[tuple[str, str]], tools: list[dict[str, Any]]) -> None:
        self.p = provider
        static, dynamic = split_system(system)
        self.system = _system(static)
        self.messages: list[dict[str, Any]] = provider._messages(history, dynamic)
        self.tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]

    def add_tools(self, tools: list[dict[str, Any]]) -> None:
        have = {t["name"] for t in self.tools}
        self.tools += [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                       for t in tools if t["name"] not in have]

    def trim_results(self, keep: int) -> int:
        results = [b for m in self.messages if m["role"] == "user" and isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        shrunk = 0
        for b in results[:-keep] if keep else results:
            small = stub(b["content"])
            if small != b["content"]:
                b["content"] = small
                shrunk += 1
        return shrunk

    async def step(self, model: str) -> Step:
        payload = {"model": model, "max_tokens": MAX_TOKENS, "system": self.system, "messages": _mark_last(self.messages),
                   "tools": self.tools}
        try:
            r = await self.p.http.post(f"{API}/messages", json=payload)
        except (httpx.HTTPError, *NETWORK_EXCEPTIONS) as e:
            raise ProviderError(f"Could not reach Anthropic: {e}", NETWORK_ERROR) from e
        if r.status_code >= 400:
            raise ProviderError(_error_text(r), r.status_code, _retry_after(r))
        self.p._usage(model, _usage(r.json()))
        blocks = r.json().get("content", [])
        self.messages.append({"role": "assistant", "content": blocks})
        calls = [ToolCall(id=b["id"], name=b["name"], args=b.get("input") or {}) for b in blocks if b.get("type") == "tool_use"]
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return Step(text="" if calls else text, calls=calls)

    def add_results(self, results: list[tuple[ToolCall, dict[str, Any]]]) -> None:
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(res, ensure_ascii=False, default=str),
             "is_error": not res.get("ok", True)}
            for c, res in results
        ]})
