"""Anthropic Claude via the Messages API (plain HTTP, no SDK needed)."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .base import Provider, ProviderError, json_instruction, merge_turns, parse_json_loose
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
            raise ProviderError(f"Could not reach Anthropic: {e}") from e
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
            raise ProviderError(f"Connection to Anthropic failed: {e}") from e

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
            raise ProviderError(f"Could not reach Anthropic: {e}") from e
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
