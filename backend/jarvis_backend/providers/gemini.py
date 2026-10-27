"""Google Gemini via the google-genai SDK."""
from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncIterator

import httpx
from google import genai
from google.genai import errors, types

from .base import (NETWORK_ERROR, Provider, ProviderError, is_network_error, Step, ToolCall, ToolSession, merge_turns, parse_json_loose,
                   rejects, split_system, stub, usage_of, with_context)

_LEVELS = {"low": types.ThinkingLevel.LOW, "medium": types.ThinkingLevel.MEDIUM, "high": types.ThinkingLevel.HIGH}
_NO_LEVEL: set[str] = set()  # models that reject a thinking level (older ones think with a budget, or not at all)


def _thinking(model: str, effort: str) -> types.ThinkingConfig | None:
    if model in _NO_LEVEL:
        return None
    return types.ThinkingConfig(include_thoughts=effort != "low", thinking_level=_LEVELS.get(effort, types.ThinkingLevel.MEDIUM))

EMBED_DIM = 256
_BATCH = 100
_SKIP = re.compile(r"tts|image|audio|live|embedding|exp|vision|learnlm|gemma|aqa|robotics|computer-use|transcribe|preview")


def _version_key(name: str) -> tuple[float, int]:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name) or re.search(r"-(\d+(?:\.\d+)?)(?:-|$)", name)
    return (float(m.group(1)) if m else 0.0, 0 if "preview" in name else 1)


def _retry_after(msg: str) -> float | None:
    m = re.search(r"retry in ([\d.]+)\s*(ms|s)", msg, re.I)
    if not m:
        return None
    v = float(m.group(1))
    return v / 1000 if m.group(2).lower() == "ms" else v


def _usage(r: Any) -> dict[str, int]:
    m = getattr(r, "usage_metadata", None)
    if m is None:
        return usage_of()
    out = (m.candidates_token_count or 0) + (getattr(m, "thoughts_token_count", 0) or 0)
    return usage_of(m.prompt_token_count, out, getattr(m, "cached_content_token_count", 0))


def _declarations(tools: list[dict[str, Any]]) -> list[types.FunctionDeclaration]:
    return [types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]) for t in tools]


def _err(e: Exception) -> ProviderError:
    if isinstance(e, errors.APIError):
        msg = str(e.message or e.status or "")
        if e.code == 429:
            everything = f"{msg} {e.details}"
            limit = re.search(r"limit: (\d+)", everything)
            period = "per day" if "PerDay" in everything else "per minute" if "PerMinute" in everything else ""
            quota = f" (limit {limit.group(1)} requests{' ' + period if period else ''})" if limit else ""
            daily = " The daily free quota resets at midnight Pacific time." if period == "per day" else ""
            return ProviderError(f"Quota reached{quota}.{daily}", 429, _retry_after(everything))
        return ProviderError(msg or f"error {e.code}", e.code)
    if is_network_error(e):  # dropped connection, stale keep-alive, timeout, DNS: worth retrying
        return ProviderError(f"connection problem ({type(e).__name__}: {e})", NETWORK_ERROR)
    return ProviderError(f"{type(e).__name__}: {e}")


class GeminiProvider(Provider):
    id = "gemini"

    def __init__(self, api_key: str) -> None:
        self.client = genai.Client(api_key=api_key)

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            raw = await asyncio.to_thread(lambda: list(self.client.models.list()))
        except Exception as e:  # noqa: BLE001
            raise _err(e) from e
        out = []
        for m in raw:
            name = (m.name or "").removeprefix("models/")
            actions = list(m.supported_actions or [])
            if "embedContent" in actions:
                out.append({"name": name, "display_name": m.display_name or name, "kind": "embed"})
            if "generateContent" in actions:
                out.append({"name": name, "display_name": m.display_name or name, "kind": "chat"})
        return sorted(out, key=lambda x: x["name"])

    async def stream_chat(self, model: str, system: str, history: list[tuple[str, str]]) -> AsyncIterator[str]:
        static, dynamic = split_system(system)
        contents = [types.Content(role=r, parts=[types.Part(text=t)]) for r, t in with_context(history, dynamic)]
        config = types.GenerateContentConfig(system_instruction=static, temperature=0.7)
        try:
            stream = await self.client.aio.models.generate_content_stream(model=model, contents=contents, config=config)
            last = None
            async for chunk in stream:
                last = chunk
                if chunk.text:
                    yield chunk.text
            if last is not None:
                self._usage(model, _usage(last))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _err(e) from e

    async def generate_json(self, model: str, prompt: str, schema: dict[str, Any]) -> Any:
        # The schema is enforced by Gemini itself (guaranteed JSON); background jobs think as little as possible.
        config = types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=schema, temperature=0.1,
                                             thinking_config=_thinking(model, "low"))
        try:
            r = await self.client.aio.models.generate_content(model=model, contents=prompt, config=config)
        except Exception as e:  # noqa: BLE001
            err = _err(e)
            if not (config.thinking_config and rejects(err, "think")):
                raise err from e
            _NO_LEVEL.add(model)
            config.thinking_config = None
            try:
                r = await self.client.aio.models.generate_content(model=model, contents=prompt, config=config)
            except Exception as e2:  # noqa: BLE001
                raise _err(e2) from e2
        self._usage(model, _usage(r))
        return parse_json_loose(r.text or "")

    async def embed(self, model: str, texts: list[str], task: str) -> list[list[float]]:
        out: list[list[float]] = []
        config = types.EmbedContentConfig(task_type=task, output_dimensionality=EMBED_DIM)
        for i in range(0, len(texts), _BATCH):
            batch = texts[i : i + _BATCH]
            try:
                r = await self.client.aio.models.embed_content(model=model, contents=batch, config=config)
                vectors = [list(x.values or []) for x in (r.embeddings or [])]
                if len(vectors) != len(batch):
                    # Some models (e.g. gemini-embedding-2) treat a list as one multi-part input and
                    # return a single vector, so embed each text on its own.
                    vectors = []
                    for text in batch:
                        one = await self.client.aio.models.embed_content(model=model, contents=text, config=config)
                        vectors.append(list((one.embeddings or [None])[0].values or []))
            except Exception as e:  # noqa: BLE001
                raise _err(e) from e
            out.extend(vectors)
        return out

    @staticmethod
    def pick_chat(models: list[dict[str, Any]]) -> str:
        pool = [m["name"] for m in models if m["kind"] == "chat" and not _SKIP.search(m["name"])]
        pool = [n for n in pool if re.search(r"gemini-\d", n) and "lite" not in n]
        flash = [n for n in pool if "flash" in n]
        return max(flash or pool, key=_version_key) if pool else ""

    @staticmethod
    def pick_utility(models: list[dict[str, Any]], chat_model: str) -> str:
        pool = [
            m["name"] for m in models
            if m["kind"] == "chat" and "flash-lite" in m["name"] and not _SKIP.search(m["name"]) and re.search(r"gemini-\d", m["name"])
        ]
        return max(pool, key=_version_key) if pool else chat_model

    @staticmethod
    def pick_embed(models: list[dict[str, Any]]) -> str:
        pool = [m["name"] for m in models if m["kind"] == "embed"]
        stable = [n for n in pool if "gemini-embedding" in n and "preview" not in n]
        preferred = stable or [n for n in pool if "gemini-embedding" in n]
        return max(preferred, key=_version_key) if preferred else (pool[0] if pool else "")

    @staticmethod
    def fallbacks(models: list[dict[str, Any]], primary: str) -> list[str]:
        pool = [
            m["name"] for m in models
            if m["kind"] == "chat" and "flash" in m["name"] and not _SKIP.search(m["name"])
            and re.search(r"gemini-\d", m["name"]) and m["name"] != primary
        ]
        pool.sort(key=lambda n: ("lite" in n, tuple(-x for x in _version_key(n))))
        # Free-tier quota is per model, and the lite models have the most of it: always end on one.
        full = [n for n in pool if "lite" not in n][:3]
        lite = [n for n in pool if "lite" in n][:1]
        return full + lite

    @staticmethod
    def pick_advisor(models: list[dict[str, Any]], chat_model: str) -> str:
        pool = [m["name"] for m in models if m["kind"] == "chat" and "pro" in m["name"] and not _SKIP.search(m["name"])
                and re.search(r"gemini-\d", m["name"])]
        return max(pool, key=_version_key) if pool else chat_model

    def tool_session(self, system: str, history: list[tuple[str, str]], tools: list[dict[str, Any]]) -> ToolSession:
        return _GeminiTools(self, system, history, tools)

    async def describe_image(self, model: str, prompt: str, png: bytes) -> str:
        try:
            r = await self.client.aio.models.generate_content(
                model=model, contents=[types.Part.from_bytes(data=png, mime_type="image/jpeg"), prompt]
            )
        except Exception as e:  # noqa: BLE001
            raise _err(e) from e
        return r.text or ""


class _GeminiTools(ToolSession):
    def __init__(self, provider: GeminiProvider, system: str, history: list[tuple[str, str]], tools: list[dict[str, Any]]) -> None:
        self.p = provider
        self.client = provider.client
        static, dynamic = split_system(system)
        # The fixed instructions and tools come first and stay identical between requests, so Gemini's
        # implicit cache can reuse them; what changes rides at the end, in the latest user turn.
        self.contents = [types.Content(role=r, parts=[types.Part(text=t)]) for r, t in with_context(history, dynamic)]
        self.declarations = _declarations(tools)
        self.config = types.GenerateContentConfig(
            system_instruction=static,
            temperature=0.4,
            tools=[types.Tool(function_declarations=self.declarations)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def add_tools(self, tools: list[dict[str, Any]]) -> None:
        have = {d.name for d in self.declarations}
        self.declarations += _declarations([t for t in tools if t["name"] not in have])
        self.config.tools = [types.Tool(function_declarations=self.declarations)]

    def trim_results(self, keep: int) -> int:
        import json

        responses = [p for c in self.contents if c.role == "user" for p in (c.parts or []) if p.function_response is not None]
        shrunk = 0
        for part in responses[:-keep] if keep else responses:
            raw = json.dumps(part.function_response.response or {}, ensure_ascii=False, default=str)
            small = stub(raw)
            if small != raw:
                part.function_response.response = json.loads(small)
                shrunk += 1
        return shrunk

    async def step(self, model: str) -> Step:
        self.config.thinking_config = _thinking(model, self.effort)
        try:
            r = await self.client.aio.models.generate_content(model=model, contents=self.contents, config=self.config)
        except Exception as e:  # noqa: BLE001
            err = _err(e)
            if not (self.config.thinking_config and rejects(err, "think")):
                raise err from e
            _NO_LEVEL.add(model)  # this model doesn't take a thinking level: carry on without one
            self.config.thinking_config = None
            try:
                r = await self.client.aio.models.generate_content(model=model, contents=self.contents, config=self.config)
            except Exception as e2:  # noqa: BLE001
                raise _err(e2) from e2
        self.p._usage(model, _usage(r))
        if not r.candidates or r.candidates[0].content is None:
            reason = getattr(r.prompt_feedback, "block_reason", None) if r.prompt_feedback else None
            raise ProviderError(f"The model returned no answer{f' (blocked: {reason})' if reason else ''}.")
        content = r.candidates[0].content
        self.contents.append(content)  # as-is, so thought signatures are preserved
        calls = [
            ToolCall(id=fc.id or f"call{i}", name=fc.name or "", args=dict(fc.args or {}))
            for i, fc in enumerate(r.function_calls or [])
        ]
        text = "" if calls else "".join(p.text for p in (content.parts or []) if p.text and not getattr(p, "thought", False))
        thought = " ".join(p.text for p in (content.parts or []) if p.text and getattr(p, "thought", False)).strip()
        return Step(text=text, calls=calls, thought=thought)

    def add_results(self, results: list[tuple[ToolCall, dict[str, Any]]]) -> None:
        parts = []
        for call, result in results:
            part = types.Part.from_function_response(name=call.name, response=result)
            if part.function_response is not None and not call.id.startswith("call"):
                part.function_response.id = call.id
            parts.append(part)
        self.contents.append(types.Content(role="user", parts=parts))
