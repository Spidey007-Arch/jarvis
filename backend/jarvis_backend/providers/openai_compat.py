"""Any OpenAI-compatible Chat Completions API: OpenAI, OpenRouter, Ollama, Groq, DeepSeek, LM Studio, ..."""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

import httpx

from .base import Provider, ProviderError, json_instruction, merge_turns, parse_json_loose

_NOT_CHAT = re.compile(
    r"whisper|tts|dall-e|davinci|babbage|moderation|image|audio|realtime|transcribe|embed|search|computer-use|sora|codex-mini"
)


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _error_text(r: httpx.Response, body: bytes | None = None) -> str:
    raw = body if body is not None else r.content
    try:
        j = json.loads(raw)
        e = j.get("error", j)
        msg = e.get("message") if isinstance(e, dict) else str(e)
        return msg or raw.decode("utf-8", "replace")[:300]
    except ValueError:
        return raw.decode("utf-8", "replace")[:300] or f"HTTP {r.status_code}"


class OpenAICompatProvider(Provider):
    def __init__(self, provider_id: str, base_url: str, api_key: str = "", extra_headers: dict[str, str] | None = None) -> None:
        self.id = provider_id
        self.base = base_url.rstrip("/")
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.http = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(120.0, connect=10.0))

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self.http.post(f"{self.base}{path}", json=payload)
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach {self.base}: {e}") from e
        if r.status_code >= 400:
            raise ProviderError(_error_text(r), r.status_code, _retry_after(r))
        return r.json()

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            r = await self.http.get(f"{self.base}/models")
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach {self.base}: {e}") from e
        if r.status_code >= 400:
            raise ProviderError(_error_text(r), r.status_code)
        out = []
        for m in r.json().get("data", []):
            mid = m.get("id", "")
            if not mid:
                continue
            name = m.get("name") or mid
            if "embed" in mid.lower():
                out.append({"name": mid, "display_name": name, "kind": "embed"})
            elif not _NOT_CHAT.search(mid.lower()):
                out.append({"name": mid, "display_name": name, "kind": "chat"})
        return sorted(out, key=lambda x: x["name"])

    def _messages(self, system: str, history: list[tuple[str, str]]) -> list[dict[str, str]]:
        msgs = [{"role": "system", "content": system}] if system else []
        msgs += [{"role": "assistant" if r == "model" else "user", "content": t} for r, t in merge_turns(history)]
        return msgs

    async def stream_chat(self, model: str, system: str, history: list[tuple[str, str]]) -> AsyncIterator[str]:
        payload = {"model": model, "messages": self._messages(system, history), "stream": True, "temperature": 0.7}
        try:
            async with self.http.stream("POST", f"{self.base}/chat/completions", json=payload) as r:
                if r.status_code >= 400:
                    raise ProviderError(_error_text(r, await r.aread()), r.status_code, _retry_after(r))
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        j = json.loads(data)
                    except ValueError:
                        continue
                    if "error" in j:
                        e = j["error"]
                        raise ProviderError(e.get("message", str(e)) if isinstance(e, dict) else str(e))
                    for choice in j.get("choices", []):
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            yield text
        except httpx.HTTPError as e:
            raise ProviderError(f"Connection to {self.base} failed: {e}") from e

    async def generate_json(self, model: str, prompt: str, schema: dict[str, Any]) -> Any:
        base = {"model": model, "temperature": 0.1}
        attempts = [
            {"messages": [{"role": "user", "content": prompt}],
             "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}}},
            {"messages": [{"role": "user", "content": prompt + json_instruction(schema)}], "response_format": {"type": "json_object"}},
            {"messages": [{"role": "user", "content": prompt + json_instruction(schema)}]},
        ]
        last: ProviderError | None = None
        for extra in attempts:
            try:
                j = await self._post("/chat/completions", {**base, **extra})
            except ProviderError as e:
                # Not every server supports structured output; fall back to plainer requests.
                if e.status in (400, 404, 422) and ("response_format" in str(e) or "json" in str(e).lower() or e.status == 422):
                    last = e
                    continue
                raise
            content = j["choices"][0]["message"].get("content") or ""
            return parse_json_loose(content)
        raise last or ProviderError("Structured output failed.")

    async def embed(self, model: str, texts: list[str], task: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            j = await self._post("/embeddings", {"model": model, "input": texts[i : i + 64]})
            rows = sorted(j.get("data", []), key=lambda d: d.get("index", 0))
            out.extend(d["embedding"] for d in rows)
        return out

    @staticmethod
    def pick_chat(models: list[dict[str, Any]]) -> str:
        chat = [m["name"] for m in models if m["kind"] == "chat"]
        if not chat:
            return ""
        if "openrouter/auto" in chat:
            return "openrouter/auto"
        gpt = [n for n in chat if re.match(r"(openai/)?gpt-\d", n)]
        if gpt:
            def ver(n: str) -> tuple[float, int]:
                m = re.search(r"gpt-(\d+(?:\.\d+)?)", n)
                return (float(m.group(1)) if m else 0.0, 1 if "mini" in n and "nano" not in n else 0)
            return max(gpt, key=ver)
        return chat[0]

    @staticmethod
    def pick_embed(models: list[dict[str, Any]]) -> str:
        emb = [m["name"] for m in models if m["kind"] == "embed"]
        for preferred in ("text-embedding-3-small", "nomic-embed-text", "mxbai-embed-large"):
            hit = [n for n in emb if preferred in n]
            if hit:
                return hit[0]
        return emb[0] if emb else ""
