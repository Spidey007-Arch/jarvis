# JARVIS

A personal AI desktop assistant for Windows, modeled on JARVIS from the Iron Man films.
It remembers you across conversations, speaks in a composed, dryly witty voice, and runs as a native desktop app.

**Bring your own AI.** Pick a provider in Settings and paste your own API key, or run fully local and free with Ollama.

**Stack:** Tauri 2 (Rust) · React + TypeScript · Python (FastAPI backend + desktop agent)

## Supported AI providers

| Provider | Key needed | Memory search | Tested |
|---|---|---|---|
| Google Gemini | Yes ([AI Studio](https://aistudio.google.com), free tier available) | Semantic (embeddings) | Live |
| OpenAI | Yes ([platform.openai.com](https://platform.openai.com/api-keys)) | Semantic (embeddings) | Protocol tests |
| Anthropic Claude | Yes ([console.anthropic.com](https://console.anthropic.com)) | Keyword (Claude has no embeddings API) | Protocol tests |
| OpenRouter (hundreds of models) | Yes ([openrouter.ai/keys](https://openrouter.ai/keys)) | Semantic or keyword | Protocol tests |
| Ollama (local, free) | No ([ollama.com](https://ollama.com)) | Semantic with e.g. `nomic-embed-text` | Protocol tests |
| Any OpenAI-compatible API or self-hosted gateway (Groq, DeepSeek, Together, Mistral, LM Studio, ...) | Depends | Semantic or keyword | Protocol tests |

"Live" means verified end to end against the real service. "Protocol tests" means the request/response handling is tested against a local server that speaks that provider's API; reports from real use are welcome.

If a model is overloaded or rate-limited, JARVIS automatically tries another model from the same provider and tells you which one answered.

## What needs an API key

JARVIS ships with no keys. Add your own in **Settings**; everything is stored encrypted on your PC.

| Feature | Needs |
|---|---|
| Chat, memory, PC actions, screen reading, vision clicking | A key for any supported AI provider above (or a local Ollama, no key) |
| Live speech-to-speech voice | A Google Gemini key (Gemini Live). Without it, the offline voice (Parakeet/Whisper + Piper) is used |
| Web research | The chosen AI provider |
| ~2,200 public APIs (weather, crypto, news, sports, ...) | Most need no key; for the rest, save that service's key in **Settings → API keys** |
| Offline voice, wake word, speaker verification | No key (local models) |

## Features

- **Acts on your PC.** Open apps and sites, search or research the web with sources, report system health, control volume, media and brightness, read what's on screen and click in any app, manage files, lock/sleep/restart, open Windows settings, and set reminders. Every action reports its real result.
- **Works like a coding assistant.** Runs PowerShell commands (git, python, npm, winget...), reads, writes and edits files (with automatic backups), and lists or ends processes.
- **You decide how much it may do.** By default every change asks for your Allow (or a spoken "yes"). On your own PC, **Settings → Full control** lets it act without asking; deleting, uninstalling, killing processes, power actions and sending or paying still ask. Commands that would break Windows are always refused.
- **Behaves like JARVIS.** Composed, dryly witty, infers what you mean, warns before sweeping or risky actions, stays calm when you're under pressure, and picks up earlier threads. It also speaks up on its own about low battery, a nearly full drive or sustained overload (can be turned off).
- **Long-term memory.** After each message, durable facts about you (name, preferences, goals, projects, people, events, how you like answers) are extracted, updated when they change, and forgotten on request. Small talk is ignored. Review, edit, delete, export and import everything in the Memory panel.
- **Living interface.** An interactive particle field and an animated core that reacts to what JARVIS is doing (idle, listening, thinking, speaking), with the chat floating in front.
- **Reliable by design.** The desktop shell launches the backend and agent invisibly, health-checks them, restarts them if they crash, and shuts everything down cleanly.
- **Private.** API keys and settings are encrypted with Windows DPAPI and stored only on your PC (`%LOCALAPPDATA%\JARVIS`).

## Roadmap

| Milestone | State |
|---|---|
| 1. Startup & reliability | Done |
| 2. Memory + multi-provider chat | Done |
| 3. Desktop automation: apps, web search & research, system status, volume, media, brightness, screen reading, clipboard, keyboard, files, power, settings, reminders, shopping & food carts (you pay) | Done |
| 4. Persona: Section 2 behaviours, situational awareness, proactive alerts, scored evaluation | Done |
| 5. Voice (wake word, speech-to-text, text-to-speech, all free and local) | Done |
| 6. UI polish (markdown, code highlighting, regenerate/edit) | Partial |
| 7. Packaging (installer, portable, standalone .exe) | Planned |

## Voice setup

Voice runs entirely on your PC. Download these free files into `%LOCALAPPDATA%\JARVIS\voice\downloads`, then turn on **Settings → Voice**:

| File | Source |
|---|---|
| `whisper-bin-x64.zip` | [whisper.cpp release b5130](https://github.com/ggml-org/whisper.cpp/releases/tag/b5130) |
| `ggml-base.en-q5_1.bin` | [whisper.cpp models](https://huggingface.co/ggerganov/whisper.cpp) |
| `piper_windows_amd64.zip` | [Piper 2023.11.14-2](https://github.com/rhasspy/piper/releases/tag/2023.11.14-2) |
| `en_GB-alan-medium.onnx` and `.onnx.json` | [Piper voices](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_GB/alan/medium) |
| `melspectrogram.onnx`, `embedding_model.onnx`, `hey_jarvis_v0.1.onnx` | [openWakeWord v0.5.1](https://github.com/dscripka/openWakeWord/releases/tag/v0.5.1) |
| `sherpa-onnx-nemo-parakeet_tdt_ctc_110m-en-36000-int8.tar.bz2` (optional, recommended) | [sherpa-onnx ASR models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models): NVIDIA Parakeet speech recognition, far more accurate than Whisper base. The larger `sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2` also works and is used if present. |
| `wespeaker_en_voxceleb_resnet34_LM.onnx` (optional) | [sherpa-onnx speaker models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models): lets **Settings → Voice → Teach JARVIS my voice** make it answer only you. |

Also install the Python packages into the app's environment: `pip install numpy onnxruntime sherpa-onnx`.

Say **"Jarvis"** with your request ("Jarvis, open Spotify"). Saying "Hey JARVIS" while it talks stops it. Replies are always spoken, typed or not.

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
.venv\Scripts\python.exe -m unittest backend.tests.test_memory backend.tests.test_api backend.tests.test_providers backend.tests.test_monitor -v
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
