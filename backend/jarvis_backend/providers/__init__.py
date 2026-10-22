"""Supported AI providers. Users bring their own key (or a local server) and choose models in Settings."""
from __future__ import annotations

from typing import Any

from .base import Provider, ProviderError

PRESETS: dict[str, dict[str, Any]] = {
    "gemini": {
        "name": "Google Gemini",
        "needs_key": True,
        "base_url": "",
        "key_hint": "Create a key at aistudio.google.com (has a free tier).",
        "embeddings": True,
    },
    "openai": {
        "name": "OpenAI",
        "needs_key": True,
        "base_url": "https://api.openai.com/v1",
        "key_hint": "Create a key at platform.openai.com/api-keys.",
        "embeddings": True,
    },
    "anthropic": {
        "name": "Anthropic Claude",
        "needs_key": True,
        "base_url": "",
        "key_hint": "Create a key at console.anthropic.com. Claude has no embeddings, so memory uses keyword search.",
        "embeddings": False,
    },
    "openrouter": {
        "name": "OpenRouter",
        "needs_key": True,
        "base_url": "https://openrouter.ai/api/v1",
        "key_hint": "One key for hundreds of models. Create it at openrouter.ai/keys.",
        "embeddings": True,
    },
    "omniroute": {
        "name": "OmniRoute (gateway)",
        "needs_key": False,
        "base_url": "http://localhost:20128/v1",
        "key_hint": "Start OmniRoute (run `omniroute`, or its Docker image), then paste a gateway key from its dashboard's API Keys page if you created one. Hosted instance? Set the base URL to https://your-domain/v1.",
        "embeddings": True,
    },
    "ollama": {
        "name": "Ollama (local, free)",
        "needs_key": False,
        "base_url": "http://localhost:11434/v1",
        "key_hint": "Runs models on this PC. Install from ollama.com, then e.g. `ollama pull llama3.2` and `ollama pull nomic-embed-text`.",
        "embeddings": True,
    },
    "custom": {
        "name": "Other OpenAI-compatible",
        "needs_key": False,
        "base_url": "",
        "key_hint": "Groq, DeepSeek, Together, Mistral, LM Studio and others: enter their OpenAI-compatible base URL and key.",
        "embeddings": True,
    },
}


def provider_class(provider_id: str) -> type[Provider]:
    if provider_id == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider
    if provider_id == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider
    from .openai_compat import OpenAICompatProvider

    return OpenAICompatProvider


def make_provider(provider_id: str, api_key: str, base_url: str) -> Provider:
    if provider_id not in PRESETS:
        raise ProviderError(f"Unknown provider '{provider_id}'.")
    if provider_id == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(api_key)
    if provider_id == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(api_key)
    from .openai_compat import OpenAICompatProvider

    url = (base_url or PRESETS[provider_id]["base_url"]).strip()
    if not url:
        raise ProviderError("Enter the provider's base URL in Settings (it usually ends with /v1).")
    headers = {"X-Title": "JARVIS"} if provider_id == "openrouter" else None
    return OpenAICompatProvider(provider_id, url, api_key, headers)


__all__ = ["PRESETS", "Provider", "ProviderError", "make_provider", "provider_class"]
