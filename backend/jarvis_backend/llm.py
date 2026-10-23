"""Provider-agnostic access to the user's chosen AI: chat streaming, structured JSON, embeddings.

Busy or rate-limited models (429 / 5xx) are put on a short cooldown and the next
candidate is tried immediately, and the user is told which model answered.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from .providers import PRESETS, Provider, ProviderError, make_provider, provider_class
from .providers.base import NETWORK_ERROR, Step, ToolCall, tools_unsupported
from .settings import SettingsStore

T = TypeVar("T")
_COOLDOWN_S = 120.0
_MAX_COOLDOWN_S = 1800.0
FIRST_RESPONSE_TIMEOUT_S = 30.0  # overloaded models can hang for minutes before admitting it


class LLMNotConfigured(Exception):
    """No provider key / model has been set up yet."""


class LLMError(Exception):
    """The provider returned an error; the message is the real reason."""


class ToolsUnsupported(LLMError):
    """The selected model cannot call tools; chat still works without actions."""


class AgentIncomplete(LLMError):
    """Tools ran, but the model could not produce its final reply afterwards."""


async def _timed(aw: Awaitable[T], seconds: float = FIRST_RESPONSE_TIMEOUT_S) -> T:
    try:
        return await asyncio.wait_for(aw, seconds)
    except asyncio.TimeoutError as e:
        raise ProviderError(f"No response within {seconds:.0f}s", 504) from e


def describe(e: ProviderError, provider: str, model: str | None) -> LLMError:
    name = PRESETS.get(provider, {}).get("name", provider)
    where = f"{name}{f' ({model})' if model else ''}"
    if e.status == 429:
        wait = f" Try again in about {max(1, round(e.retry_after))}s." if e.retry_after else " Try again shortly."
        return LLMError(f"{where}: {e}{wait}")
    if e.status == NETWORK_ERROR:
        return LLMError(f"{where}: the connection dropped before an answer arrived ({e}). Please try again; your internet connection may be unstable.")
    if e.status in (500, 502, 503, 504, 529):
        return LLMError(f"{where} is overloaded right now (error {e.status}). Try again in a moment.")
    if e.status == 401:
        return LLMError(f"{where} rejected the API key: {e}")
    if e.status == 403:
        return LLMError(f"{where} refused the request (403): {e}")
    return LLMError(f"{where}: {e}" + (f" (HTTP {e.status})" if e.status else ""))


class LLM:
    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings
        self._provider: Provider | None = None
        self._signature: tuple[str, str, str] | None = None
        self._models_cache: list[dict[str, Any]] | None = None
        self._cooldown: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    # ---------- configuration ----------

    @property
    def provider_id(self) -> str:
        return self.settings.provider

    def provider(self) -> Provider:
        pid = self.provider_id
        key = self.settings.api_key(pid)
        base = self.settings.provider_cfg(pid)["base_url"]
        if PRESETS[pid]["needs_key"] and not key:
            raise LLMNotConfigured(f"No API key is set for {PRESETS[pid]['name']}. Add it in Settings.")
        sig = (pid, key, base)
        if sig != self._signature or self._provider is None:
            old = self._provider
            try:
                self._provider = make_provider(pid, key, base)
            except ProviderError as e:
                raise LLMNotConfigured(str(e)) from e
            self._signature = sig
            self._models_cache = None
            self._cooldown.clear()
            if old is not None:
                try:
                    asyncio.get_running_loop().create_task(old.aclose())
                except RuntimeError:
                    pass
        return self._provider

    @property
    def chat_model(self) -> str:
        m = self.settings.provider_cfg()["chat_model"]
        if not m:
            raise LLMNotConfigured("No chat model is selected. Choose one in Settings.")
        return m

    @property
    def embed_model(self) -> str:
        """Empty when the provider has no embeddings; memory then uses keyword search."""
        return self.settings.provider_cfg()["embed_model"]

    @property
    def can_embed(self) -> bool:
        return bool(self.embed_model) and PRESETS[self.provider_id]["embeddings"]

    @property
    def ready(self) -> bool:
        pid = self.provider_id
        has_key = bool(self.settings.api_key(pid)) or not PRESETS[pid]["needs_key"]
        return has_key and bool(self.settings.provider_cfg(pid)["chat_model"])

    # ---------- models ----------

    async def list_models(self, provider_id: str | None = None, api_key: str | None = None, base_url: str | None = None) -> list[dict[str, Any]]:
        """Models for the current provider, or for a candidate provider/key/URL being validated."""
        candidate = provider_id is not None or api_key is not None or base_url is not None
        if candidate:
            pid = provider_id or self.provider_id
            p = make_provider(
                pid,
                api_key if api_key is not None else self.settings.api_key(pid),
                base_url if base_url is not None else self.settings.provider_cfg(pid)["base_url"],
            )
            try:
                return await p.list_models()
            except ProviderError as e:
                raise describe(e, pid, None) from e
            finally:
                await p.aclose()
        try:
            models = await self.provider().list_models()
        except ProviderError as e:
            raise describe(e, self.provider_id, None) from e
        self._models_cache = models
        return models

    def default_models(self, provider_id: str, models: list[dict[str, Any]]) -> dict[str, str]:
        cls = provider_class(provider_id)
        return {"chat_model": cls.pick_chat(models), "embed_model": cls.pick_embed(models) if PRESETS[provider_id]["embeddings"] else ""}

    async def _models(self) -> list[dict[str, Any]]:
        if self._models_cache is None:
            try:
                await self.list_models()
            except LLMError:
                self._models_cache = []
        return self._models_cache or []

    # ---------- fallback ----------

    def _available(self, names: list[str]) -> list[str]:
        now = time.monotonic()
        ready = [n for n in names if self._cooldown.get(n, 0) <= now]
        cooling = sorted((n for n in names if n not in ready), key=lambda n: self._cooldown.get(n, 0))
        return ready + cooling  # still try busy models last, soonest-available first

    def _cool(self, model: str, e: ProviderError) -> None:
        """Back off from a busy model; models that keep failing are avoided for longer."""
        if e.status == NETWORK_ERROR:  # the model is fine; the connection was not
            self._cooldown[model] = time.monotonic() + 10.0
            return
        n = self._failures.get(model, 0) + 1
        self._failures[model] = n
        backoff = min(_COOLDOWN_S * 2 ** (n - 1), _MAX_COOLDOWN_S)
        self._cooldown[model] = time.monotonic() + max(e.retry_after or 0, backoff if not e.retry_after else 0, 5.0)

    def _ok(self, model: str) -> None:
        self._failures.pop(model, None)

    async def _chat_candidates(self) -> list[str]:
        primary = self.chat_model
        return self._available([primary] + self.provider().fallbacks(await self._models(), primary))

    async def _utility_candidates(self) -> list[str]:
        p = self.provider()
        primary = self.chat_model
        models = await self._models()
        utility = p.pick_utility(models, primary) if models else primary
        names = [utility] + p.fallbacks(models, primary) + [primary]
        return self._available(list(dict.fromkeys(n for n in names if n)))

    async def _first_success(self, candidates: list[str], call: Callable[[str], Awaitable[T]], timeout: float = FIRST_RESPONSE_TIMEOUT_S) -> T:
        last: ProviderError | None = None
        last_model = candidates[0] if candidates else None
        queue, retried = list(candidates), set()
        while queue:
            model = queue.pop(0)
            try:
                result = await _timed(call(model), timeout)
                self._ok(model)
                return result
            except ProviderError as e:
                if not e.transient:
                    raise describe(e, self.provider_id, model) from e
                if e.status == NETWORK_ERROR and model not in retried:
                    retried.add(model)
                    queue.insert(0, model)
                    await asyncio.sleep(0.3)
                    continue
                self._cool(model, e)
                last, last_model = e, model
        raise describe(last, self.provider_id, last_model) if last else LLMError("No model is available.")

    # ---------- operations ----------

    async def stream_chat(
        self, system: str, history: list[tuple[str, str]], on_notice: Callable[[str], None] | None = None
    ) -> AsyncIterator[str]:
        p = self.provider()
        preferred = self.chat_model
        candidates = await self._chat_candidates()
        last: ProviderError | None = None
        last_model = candidates[0]
        queue, retried = list(candidates), set()
        while queue:
            model = queue.pop(0)
            started = False
            try:
                stream = p.stream_chat(model, system, history).__aiter__()
                while True:
                    try:
                        text = await (stream.__anext__() if started else _timed(stream.__anext__()))
                    except StopAsyncIteration:
                        break
                    if not text:
                        continue
                    if not started and model != preferred and on_notice:
                        on_notice(f"{preferred} is busy right now; this reply came from {model}.")
                    started = True
                    yield text
                self._ok(model)
                return
            except ProviderError as e:
                if started or not e.transient:
                    raise describe(e, self.provider_id, model) from e
                if e.status == NETWORK_ERROR and model not in retried:
                    retried.add(model)
                    queue.insert(0, model)
                    await asyncio.sleep(0.3)
                    continue
                self._cool(model, e)
                last, last_model = e, model
        raise describe(last, self.provider_id, last_model) if last else LLMError("No model is available.")

    async def generate_json(self, prompt: str, schema: dict[str, Any]) -> Any:
        p = self.provider()
        return await self._first_success(await self._utility_candidates(), lambda m: p.generate_json(m, prompt, schema))

    async def embed(self, texts: list[str], task: str) -> list[list[float]]:
        if not self.can_embed:
            raise LLMNotConfigured("No embedding model is selected.")
        p = self.provider()
        model = self.embed_model
        for attempt in range(3):
            try:
                out = await p.embed(model, texts, task)
                break
            except ProviderError as e:
                if attempt == 2 or not e.transient:
                    raise describe(e, self.provider_id, model) from e
                await asyncio.sleep(1.0 * (attempt + 1))
        if len(out) != len(texts):
            raise LLMError(f"The provider returned {len(out)} embeddings for {len(texts)} inputs.")
        return out

    async def run_agent(
        self,
        system: str,
        history: list[tuple[str, str]],
        tools: list[dict[str, Any]],
        execute: Callable[[ToolCall], Awaitable[dict[str, Any]]],
        on_notice: Callable[[str], None] | None = None,
        max_rounds: int = 8,
    ) -> str:
        """Let the model call tools until it produces a final answer. Returns that answer.

        Falls back to another model only on the first step (before any tool ran), because
        some providers tie in-progress tool state to the model that produced it."""
        p = self.provider()
        preferred = self.chat_model
        session = p.tool_session(system, history, tools)
        model = None
        step: Step | None = None
        last: ProviderError | None = None
        last_model = preferred
        queue, retried = await self._chat_candidates(), set()
        while queue:
            candidate = queue.pop(0)
            try:
                step = await _timed(session.step(candidate))
                model = candidate
                self._ok(candidate)
                break
            except ProviderError as e:
                if tools_unsupported(e):
                    raise ToolsUnsupported(str(e)) from e
                if not e.transient:
                    raise describe(e, self.provider_id, candidate) from e
                if e.status == NETWORK_ERROR and candidate not in retried:
                    retried.add(candidate)
                    queue.insert(0, candidate)
                    await asyncio.sleep(0.3)
                    continue
                self._cool(candidate, e)
                last, last_model = e, candidate
        if step is None or model is None:
            if last is not None and last.status == 429:
                raise LLMError(f"Every available {PRESETS[self.provider_id]['name']} model is out of quota right now. {last} "
                               "Try again later, or connect a key with more quota in Settings.")
            raise describe(last, self.provider_id, last_model) if last else LLMError("No model is available.")
        if model != preferred and on_notice:
            on_notice(f"{preferred} is busy right now; this reply came from {model}.")

        for _ in range(max_rounds):
            if not step.calls:
                return step.text
            results = [(call, await execute(call)) for call in step.calls]
            session.add_results(results)
            for attempt in range(2):
                try:
                    step = await _timed(session.step(model), 60.0)
                    break
                except ProviderError as e:
                    if attempt == 1 or not e.transient:
                        # Actions already ran; the caller reports them even though the model can't finish.
                        raise AgentIncomplete(str(describe(e, self.provider_id, model))) from e
                    await asyncio.sleep(min(e.retry_after or 3.0, 20.0))
        return step.text or "I stopped after several steps without finishing; tell me if you want me to keep going."

    async def describe_image(self, prompt: str, image: bytes) -> str:
        p = self.provider()
        return await self._first_success(await self._chat_candidates(), lambda m: p.describe_image(m, prompt, image), timeout=60.0)
