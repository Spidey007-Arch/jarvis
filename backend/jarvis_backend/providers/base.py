"""Common interface every AI provider implements."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


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


class Provider(ABC):
    id: str = ""
    supports_embeddings = True

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
