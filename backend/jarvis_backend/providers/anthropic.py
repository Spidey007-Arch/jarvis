"""Anthropic Claude via the Messages API (plain HTTP, no SDK needed)."""
from __future__ import annotations

import base64
import json
from typing import Any, AsyncIterator

import httpx

from .base import NETWORK_ERROR, Provider, ProviderError, Step, ToolCall, ToolSession, json_instruction, merge_turns, parse_json_loose
from .openai_compat import _error_text, _retry_after

API = "https://api.anthropic.com/v1"
VERSION = "2023-06-01"
MAX_TOKENS = 4096


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
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach Anthropic: {e}", NETWORK_ERROR) from e
        if r.status_code >= 400:
            raise ProviderError(_error_text(r), r.status_code)
        # API returns newest first; keep that order.
        return [{"name": m["id"], "display_name": m.get("display_name") or m["id"], "kind": "chat"} for m in r.json().get("data", [])]

    def _messages(self, history: list[tuple[str, str]]) -> list[dict[str, str]]:
        msgs = [{"role": "assistant" if r == "model" else "user", "content": t} for r, t in merge_turns(history)]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    async def stream_chat(self, model: str, system: str, history: list[tuple[str, str]]) -> AsyncIterator[str]:
        payload = {"model": model, "max_tokens": MAX_TOKENS, "system": system, "messages": self._messages(history), "stream": True}
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
                    if kind == "content_block_delta" and j.get("delta", {}).get("type") == "text_delta":
                        yield j["delta"].get("text", "")
                    elif kind == "error":
                        err = j.get("error", {})
                        status = 529 if err.get("type") == "overloaded_error" else None
                        raise ProviderError(err.get("message", "Anthropic stream error."), status)
                    elif kind == "message_stop":
                        return
        except httpx.HTTPError as e:
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
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach Anthropic: {e}", NETWORK_ERROR) from e
        if r.status_code >= 400:
            raise ProviderError(_error_text(r), r.status_code, _retry_after(r))
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
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach Anthropic: {e}", NETWORK_ERROR) from e
        if r.status_code >= 400:
            raise ProviderError(_error_text(r), r.status_code, _retry_after(r))
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


class _AnthropicTools(ToolSession):
    def __init__(self, provider: AnthropicProvider, system: str, history: list[tuple[str, str]], tools: list[dict[str, Any]]) -> None:
        self.p = provider
        self.system = system
        self.messages: list[dict[str, Any]] = provider._messages(history)
        self.tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]

    async def step(self, model: str) -> Step:
        payload = {"model": model, "max_tokens": MAX_TOKENS, "system": self.system, "messages": self.messages, "tools": self.tools}
        try:
            r = await self.p.http.post(f"{API}/messages", json=payload)
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach Anthropic: {e}", NETWORK_ERROR) from e
        if r.status_code >= 400:
            raise ProviderError(_error_text(r), r.status_code, _retry_after(r))
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
