"""Gemini access: model discovery, streaming chat, structured JSON, embeddings.

Free-tier keys allow only a few requests per minute per model, and popular models
are sometimes overloaded. When a model answers 429/5xx it is put on cooldown and
the next candidate is tried immediately, instead of hammering the busy one.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from google import genai
from google.genai import errors, types

from .settings import SettingsStore

EMBED_DIM = 256
_EMBED_BATCH = 100
_TRANSIENT = {429, 500, 502, 503, 504}
_OVERLOAD_COOLDOWN_S = 120.0

T = TypeVar("T")


class LLMNotConfigured(Exception):
    """No API key / model has been set up yet."""


class LLMError(Exception):
    """Gemini returned an error; the message is the real reason."""


def _retry_after(e: errors.APIError) -> float | None:
    m = re.search(r"retry in ([\d.]+)\s*(ms|s)", str(e.message or ""), re.I)
    if not m:
        return None
    v = float(m.group(1))
    return v / 1000 if m.group(2).lower() == "ms" else v


def _wrap(e: Exception, model: str | None = None) -> LLMError:
    if isinstance(e, errors.APIError):
        if e.code == 429:
            wait = _retry_after(e)
            limit = re.search(r"limit: (\d+)", str(e.message or ""))
            per = f" ({limit.group(1)} requests/minute on the free tier)" if limit else ""
            when = f" Try again in about {max(1, round(wait))}s." if wait else " Try again shortly."
            return LLMError(f"Gemini rate limit reached for {model or 'this model'}{per}.{when}")
        if e.code in (500, 502, 503, 504):
            return LLMError(f"Gemini is overloaded right now ({model or 'model'}, error {e.code}). Try again in a moment.")
        return LLMError(f"Gemini API error {e.code}: {e.message or e.status}")
    return LLMError(f"{type(e).__name__}: {e}")


def _transient(e: Exception) -> bool:
    return isinstance(e, errors.APIError) and e.code in _TRANSIENT


def _version_key(name: str) -> tuple[float, int]:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name) or re.search(r"-(\d+(?:\.\d+)?)(?:-|$)", name)
    return (float(m.group(1)) if m else 0.0, 0 if "preview" in name else 1)


_SKIP = re.compile(r"tts|image|audio|live|embedding|exp|vision|learnlm|gemma|aqa|robotics|computer-use|transcribe|preview")


def pick_chat_model(models: list[dict[str, Any]]) -> str:
    """Newest stable general-purpose Gemini Flash model (fast, largest free quota)."""
    pool = [m["name"] for m in models if "generateContent" in m["actions"] and not _SKIP.search(m["name"])]
    pool = [n for n in pool if re.search(r"gemini-\d", n) and "lite" not in n]
    if not pool:
        return ""
    flash = [n for n in pool if "flash" in n]
    return max(flash or pool, key=_version_key)


def pick_utility_model(models: list[dict[str, Any]]) -> str:
    """Model for background jobs (memory extraction): Flash-Lite, so it has its own quota."""
    pool = [
        m["name"] for m in models
        if "generateContent" in m["actions"] and "flash-lite" in m["name"] and not _SKIP.search(m["name"])
        and re.search(r"gemini-\d", m["name"])
    ]
    return max(pool, key=_version_key) if pool else ""


def pick_embed_model(models: list[dict[str, Any]]) -> str:
    pool = [m["name"] for m in models if "embedContent" in m["actions"]]
    stable = [n for n in pool if "gemini-embedding" in n and "preview" not in n]
    preferred = stable or [n for n in pool if "gemini-embedding" in n]
    return max(preferred, key=_version_key) if preferred else (pool[0] if pool else "")


def fallback_chat_models(models: list[dict[str, Any]], exclude: set[str], limit: int = 3) -> list[str]:
    """Other stable Flash models to try when the chosen one is busy."""
    pool = [
        m["name"] for m in models
        if "generateContent" in m["actions"] and "flash" in m["name"] and not _SKIP.search(m["name"])
        and re.search(r"gemini-\d", m["name"]) and m["name"] not in exclude
    ]
    pool.sort(key=lambda n: ("lite" in n, tuple(-x for x in _version_key(n))))
    return pool[:limit]


class LLM:
    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings
        self._client: genai.Client | None = None
        self._key = ""
        self._models_cache: list[dict[str, Any]] | None = None
        self._cooldown: dict[str, float] = {}

    def client(self) -> genai.Client:
        key = self.settings.secret("gemini_api_key")
        if not key:
            raise LLMNotConfigured("No Gemini API key is set. Add it in Settings.")
        if key != self._key or self._client is None:
            self._client = genai.Client(api_key=key)
            self._key = key
            self._models_cache = None
            self._cooldown.clear()
        return self._client

    @property
    def chat_model(self) -> str:
        m = self.settings.get("chat_model")
        if not m:
            raise LLMNotConfigured("No chat model is selected. Choose one in Settings.")
        return m

    @property
    def embed_model(self) -> str:
        m = self.settings.get("embed_model")
        if not m:
            raise LLMNotConfigured("No embedding model is selected. Choose one in Settings.")
        return m

    @property
    def ready(self) -> bool:
        return bool(self.settings.secret("gemini_api_key") and self.settings.get("chat_model") and self.settings.get("embed_model"))

    async def list_models(self, api_key: str | None = None) -> list[dict[str, Any]]:
        client = genai.Client(api_key=api_key) if api_key else self.client()
        try:
            raw = await asyncio.to_thread(lambda: list(client.models.list()))
        except Exception as e:  # noqa: BLE001
            raise _wrap(e) from e
        out = []
        for m in raw:
            name = (m.name or "").removeprefix("models/")
            out.append(
                {
                    "name": name,
                    "display_name": m.display_name or name,
                    "actions": list(m.supported_actions or []),
                    "input_token_limit": m.input_token_limit,
                }
            )
        out.sort(key=lambda x: x["name"])
        if not api_key:
            self._models_cache = out
        return out

    async def _models(self) -> list[dict[str, Any]]:
        if self._models_cache is None:
            try:
                await self.list_models()
            except LLMError:
                self._models_cache = []
        return self._models_cache or []

    def _available(self, names: list[str]) -> list[str]:
        """Drop models on cooldown, unless that would leave nothing to try."""
        now = time.monotonic()
        ready = [n for n in names if self._cooldown.get(n, 0) <= now]
        return ready or names[:1]

    def _cool(self, model: str, e: Exception) -> None:
        wait = _retry_after(e) if isinstance(e, errors.APIError) and e.code == 429 else None
        self._cooldown[model] = time.monotonic() + max(wait or 0, _OVERLOAD_COOLDOWN_S if not wait else 0, 5.0)

    async def _chat_candidates(self) -> list[str]:
        primary = self.chat_model
        return self._available([primary] + fallback_chat_models(await self._models(), {primary}))

    async def _utility_candidates(self) -> list[str]:
        models = await self._models()
        utility = pick_utility_model(models)
        primary = self.chat_model
        names = ([utility] if utility else []) + fallback_chat_models(models, {utility, primary}) + [primary]
        return self._available(list(dict.fromkeys(names)))

    async def _first_success(self, candidates: list[str], call: Callable[[str], Awaitable[T]]) -> T:
        last: Exception | None = None
        last_model = candidates[0] if candidates else None
        for model in candidates:
            try:
                return await call(model)
            except (LLMNotConfigured, asyncio.CancelledError):
                raise
            except Exception as e:  # noqa: BLE001
                if not _transient(e):
                    raise _wrap(e, model) from e
                self._cool(model, e)
                last, last_model = e, model
        raise _wrap(last, last_model) if last else LLMError("No Gemini model is available.")

    async def stream_chat(
        self, system: str, history: list[tuple[str, str]], on_notice: Callable[[str], None] | None = None
    ) -> AsyncIterator[str]:
        """Streams the reply. If a model is busy before its first token, the next one is tried
        and the caller is told which model actually answered."""
        contents = [types.Content(role=role, parts=[types.Part(text=text)]) for role, text in history]
        config = types.GenerateContentConfig(system_instruction=system, temperature=0.7)
        candidates = await self._chat_candidates()
        preferred = self.chat_model
        last: Exception | None = None
        last_model = candidates[0]
        for model in candidates:
            started = False
            try:
                stream = await self.client().aio.models.generate_content_stream(model=model, contents=contents, config=config)
                async for chunk in stream:
                    text = chunk.text
                    if text:
                        if not started and model != preferred and on_notice:
                            on_notice(f"{preferred} is busy right now; this reply came from {model}.")
                        started = True
                        yield text
                return
            except (LLMNotConfigured, asyncio.CancelledError):
                raise
            except Exception as e:  # noqa: BLE001
                if started or not _transient(e):
                    raise _wrap(e, model) from e
                self._cool(model, e)
                last, last_model = e, model
        raise _wrap(last, last_model) if last else LLMError("No Gemini model is available.")

    async def generate_json(self, prompt: str, schema: dict[str, Any]) -> Any:
        config = types.GenerateContentConfig(
            response_mime_type="application/json", response_json_schema=schema, temperature=0.1
        )

        async def call(model: str):
            return await self.client().aio.models.generate_content(model=model, contents=prompt, config=config)

        r = await self._first_success(await self._utility_candidates(), call)
        try:
            return json.loads(r.text or "")
        except ValueError as e:
            raise LLMError(f"Gemini returned malformed JSON: {(r.text or '')[:200]!r}") from e

    async def embed(self, texts: list[str], task: str) -> list[list[float]]:
        out: list[list[float]] = []
        config = types.EmbedContentConfig(task_type=task, output_dimensionality=EMBED_DIM)
        for i in range(0, len(texts), _EMBED_BATCH):
            batch = texts[i : i + _EMBED_BATCH]
            r = None
            for attempt in range(3):
                try:
                    r = await self.client().aio.models.embed_content(model=self.embed_model, contents=batch, config=config)
                    break
                except LLMNotConfigured:
                    raise
                except Exception as e:  # noqa: BLE001
                    if attempt == 2 or not _transient(e):
                        raise _wrap(e, self.embed_model) from e
                    await asyncio.sleep(1.0 * (attempt + 1))
            out.extend(list(e.values or []) for e in (r.embeddings or []))
        if len(out) != len(texts):
            raise LLMError(f"Gemini returned {len(out)} embeddings for {len(texts)} inputs.")
        return out
