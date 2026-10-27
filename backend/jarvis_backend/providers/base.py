"""Common interface every AI provider implements."""
from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

NETWORK_ERROR = 599  # our own code: the connection dropped or could not be made (no HTTP status)
NO_REPLY = 598  # our own code: we stopped waiting; nobody sent an error, so it is not "overloaded"
TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504, 529, NETWORK_ERROR, NO_REPLY}


def _network_exceptions() -> tuple[type[BaseException], ...]:
    import httpcore
    import httpx

    # SDKs sometimes leak httpcore's errors without wrapping them in httpx's.
    return (httpx.TransportError, httpcore.NetworkError, httpcore.ProtocolError, httpcore.TimeoutException,
            ConnectionError, TimeoutError, asyncio.TimeoutError)


NETWORK_EXCEPTIONS = _network_exceptions()


def is_network_error(e: BaseException) -> bool:
    return isinstance(e, NETWORK_EXCEPTIONS)


class ProviderError(Exception):
    """A provider rejected or failed a request. `message` is the provider's real reason."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        return self.status in TRANSIENT_STATUS or self.status is None and "timed out" in str(self).lower()


def merge_turns(history: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Collapse consecutive same-role turns (e.g. a user message whose reply failed);
    several providers require strictly alternating roles."""
    out: list[tuple[str, str]] = []
    for role, text in history:
        if out and out[-1][0] == role:
            out[-1] = (role, out[-1][1] + "\n\n" + text)
        else:
            out.append((role, text))
    return out


# The system prompt is two parts: what never changes between requests (character, abilities, how to use tools)
# and what does (the time, memories, what is on screen). Providers cache a request's unchanging beginning, so
# the changing part is moved to the end, into the latest user turn, and everything before it can be reused.
DYNAMIC_MARK = "\n\n[[jarvis:dynamic]]\n"


def split_system(system: str) -> tuple[str, str]:
    """(fixed part, changing part) of a system prompt."""
    static, _, dynamic = system.partition(DYNAMIC_MARK)
    return static.strip(), dynamic.strip()


def with_context(history: list[tuple[str, str]], dynamic: str) -> list[tuple[str, str]]:
    """The conversation with the changing context put in front of the latest user message."""
    turns = merge_turns(history)
    if not dynamic:
        return turns
    for i in range(len(turns) - 1, -1, -1):
        if turns[i][0] == "user":
            turns[i] = ("user", f"[Context right now, from JARVIS, not from the user]\n{dynamic}\n\n[The user says]\n{turns[i][1]}")
            return turns
    return turns + [("user", f"[Context right now, from JARVIS]\n{dynamic}")]


def usage_of(input_tokens: Any = 0, output_tokens: Any = 0, cached: Any = 0, cache_write: Any = 0) -> dict[str, int]:
    """Token counts in one shape for every provider (cached: read from the provider's cache)."""
    return {"input": int(input_tokens or 0), "output": int(output_tokens or 0), "cached": int(cached or 0),
            "cache_write": int(cache_write or 0)}


def parse_json_loose(text: str) -> Any:
    """Parse JSON, tolerating code fences or prose around a single JSON object."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    raise ProviderError(f"The model did not return valid JSON: {text[:200]!r}")


def json_instruction(schema: dict[str, Any]) -> str:
    return (
        "\n\nRespond with ONLY a JSON object (no prose, no code fences) that matches this JSON Schema:\n"
        + json.dumps(schema)
    )


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Step:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)


class ToolSession(ABC):
    """One multi-step tool-using exchange. Keeps the provider's native message history,
    so provider-specific details (tool ids, thought signatures) survive between steps."""

    # Whether a half-finished exchange may continue on a different model. False where the history
    # carries model-bound state (Gemini's thought signatures); plain message lists are portable.
    portable = False

    @abstractmethod
    async def step(self, model: str) -> Step: ...

    @abstractmethod
    def add_results(self, results: list[tuple[ToolCall, dict[str, Any]]]) -> None: ...

    def add_tools(self, tools: list[dict[str, Any]]) -> None:
        """Offer more tools from the next step on (tool search loads them on demand)."""

    def trim_results(self, keep: int) -> int:
        """Shrink all but the last `keep` tool results to short stubs so a long task stays small.
        Returns how many were shrunk."""
        return 0


STUB_CHARS = 240


def stub(result_json: str) -> str:
    """A tool result shrunk for a long task: its start, and a note that the rest was cleared."""
    if len(result_json) <= STUB_CHARS + 40:
        return result_json
    return json.dumps({"cleared_to_save_space": True, "start": result_json[:STUB_CHARS]})


def tools_unsupported(e: ProviderError) -> bool:
    msg = str(e).lower()
    return e.status in (400, 404, 422) and ("tool" in msg or "function" in msg)


class Provider(ABC):
    id: str = ""
    supports_embeddings = True
    on_usage: Any = None  # set by the LLM: (model, usage_of(...)) after every request that reports tokens

    def _usage(self, model: str, usage: dict[str, int]) -> None:
        if self.on_usage and (usage["input"] or usage["output"]):
            try:
                self.on_usage(model, usage)
            except Exception:  # noqa: BLE001 - counting must never break a reply
                pass

    @abstractmethod
    async def list_models(self) -> list[dict[str, Any]]:
        """[{name, display_name, kind: 'chat' | 'embed'}]"""

    @abstractmethod
    def stream_chat(self, model: str, system: str, history: list[tuple[str, str]]) -> AsyncIterator[str]:
        """history roles are 'user' and 'model'."""

    @abstractmethod
    async def generate_json(self, model: str, prompt: str, schema: dict[str, Any]) -> Any: ...

    async def embed(self, model: str, texts: list[str], task: str) -> list[list[float]]:
        raise ProviderError(f"{self.id} does not provide embeddings.")

    def tool_session(self, system: str, history: list[tuple[str, str]], tools: list[dict[str, Any]]) -> ToolSession:
        raise ProviderError(f"{self.id} does not support tool calling.", 400)

    async def describe_image(self, model: str, prompt: str, png: bytes) -> str:
        raise ProviderError("This provider cannot look at images.", 400)

    # Model choice heuristics; users can override every pick in Settings.
    @staticmethod
    def pick_chat(models: list[dict[str, Any]]) -> str:
        chat = [m["name"] for m in models if m["kind"] == "chat"]
        return chat[0] if chat else ""

    @staticmethod
    def pick_utility(models: list[dict[str, Any]], chat_model: str) -> str:
        return chat_model

    @staticmethod
    def pick_embed(models: list[dict[str, Any]]) -> str:
        emb = [m["name"] for m in models if m["kind"] == "embed"]
        return emb[0] if emb else ""

    @staticmethod
    def fallbacks(models: list[dict[str, Any]], primary: str) -> list[str]:
        return []

    async def aclose(self) -> None:
        return None
