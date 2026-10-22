# JARVIS

A personal AI desktop assistant for Windows, modeled on JARVIS from the Iron Man films.
It remembers you across conversations, speaks in a composed, dryly witty voice, and runs as a native desktop app.

**Bring your own AI.** Pick a provider in Settings and paste your own API key, or run fully local and free with Ollama.

**Stack:** Tauri 2 (Rust) · React + TypeScript · Python (FastAPI backend + desktop agent)

## Supported AI providers

| Provider | Key needed | Memory search | Tested |
|---|---|---|---|
| **★ OmniRoute (recommended: free, self-hosted gateway to hundreds of models)** | Gateway key if you created one ([OmniRoute](https://github.com/cheaper-inference/OmniRoute)) | Semantic or keyword | Protocol tests |
| Google Gemini | Yes ([AI Studio](https://aistudio.google.com), free tier available) | Semantic (embeddings) | Live |
| OpenAI | Yes ([platform.openai.com](https://platform.openai.com/api-keys)) | Semantic (embeddings) | Protocol tests |
| Anthropic Claude | Yes ([console.anthropic.com](https://console.anthropic.com)) | Keyword (Claude has no embeddings API) | Protocol tests |
| OpenRouter (hundreds of models) | Yes ([openrouter.ai/keys](https://openrouter.ai/keys)) | Semantic or keyword | Protocol tests |
| Ollama (local, free) | No ([ollama.com](https://ollama.com)) | Semantic with e.g. `nomic-embed-text` | Protocol tests |
| Any OpenAI-compatible API (Groq, DeepSeek, Together, Mistral, LM Studio, ...) | Depends | Semantic or keyword | Protocol tests |

"Live" means verified end to end against the real service. "Protocol tests" means the request/response handling is tested against a local server that speaks that provider's API; reports from real use are welcome.

If a model is overloaded or rate-limited, JARVIS automatically tries another model from the same provider and tells you which one answered.

## Features

- **Acts on your PC.** Open apps and sites, search or research the web with sources, report system health, control volume, media and brightness, read what's on screen, manage files, lock/sleep/restart, open Windows settings, and set reminders. Risky actions (delete, move, close apps, power) need your Allow in the chat first, and every action reports its real result.
- **Long-term memory.** After each message, durable facts about you (name, preferences, goals, projects, people, events, how you like answers) are extracted, updated when they change, and forgotten on request. Small talk is ignored. Review, edit, delete, export and import everything in the Memory panel.
- **Living interface.** An interactive particle field and an animated core that reacts to what JARVIS is doing (idle, listening, thinking, speaking), with the chat floating in front.
- **Reliable by design.** The desktop shell launches the backend and agent invisibly, health-checks them, restarts them if they crash, and shuts everything down cleanly.
- **Private.** API keys and settings are encrypted with Windows DPAPI and stored only on your PC (`%LOCALAPPDATA%\JARVIS`).

## Roadmap

| Milestone | State |
|---|---|
| 1. Startup & reliability | Done |
| 2. Memory + multi-provider chat | Done |
| 3. Desktop automation: apps, web search & research, system status, volume, media, brightness, screen reading, clipboard, keyboard, files, power, settings, reminders | Done |
| 4. Persona tuning | Basic persona only (next) |
| 5. Voice (wake word, speech-to-text, text-to-speech, all free and local) | Planned |
| 6. UI polish (markdown, code highlighting, regenerate/edit) | Partial |
| 7. Packaging (installer, portable, standalone .exe) | Planned |

## Run from source

Requirements: Windows 10/11, Python 3.12, Node 22, Rust (stable, MSVC), Visual Studio C++ Build Tools.

```powershell
powershell -ExecutionPolicy Bypass -File setup\install-deps.ps1
npm install
npx tauri dev
```

Then open **Settings** (gear icon), choose a provider, and connect.

Tests:

```powershell
.venv\Scripts\python.exe -m unittest backend.tests.test_memory backend.tests.test_api backend.tests.test_providers -v
```

## Layout

```
src-tauri/   Rust shell: launches and supervises the backend and agent
src/         React UI: particles, interactive core, chat, glass panels
backend/     FastAPI: chat, memory, settings, providers, agent bridge
agent/       Background process that executes OS actions for the backend
common/      Shared Python: logging, paths, Windows DPAPI encryption
```

## License

[MIT](LICENSE)
