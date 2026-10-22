# JARVIS

A personal AI desktop assistant for Windows, modeled on JARVIS from the Iron Man films.

**Stack:** Tauri 2 (Rust) shell · React + TypeScript UI · Python FastAPI backend · Python desktop agent · cloud LLM

## Status

| Milestone | State |
|---|---|
| 1. Startup & reliability: hidden backend + agent, health checks, auto-restart, single instance, clean shutdown | Done |
| 2. Memory: encrypted settings, streaming AI chat, long-term semantic memory (extract / update / forget / summarize / expire) | Done |
| 3. Desktop automation (apps, files, volume, media, screenshots, reminders) | Not started |
| 4. Persona tuning | Basic persona only |
| 5. Voice (wake word, speech-to-text, text-to-speech) | Not started |
| 6. UI polish (markdown, code highlighting, regenerate/edit) | Partial |
| 7. Packaging (installer, portable, standalone .exe) | Not started |

## Layout

```
src-tauri/   Rust shell: launches and supervises the backend and agent (supervisor.rs)
src/         React UI: particle background, interactive core, chat, glass panels
backend/     FastAPI: chat, memory, settings, agent bridge
agent/       Background process that executes OS actions for the backend
common/      Shared Python: logging, paths, Windows DPAPI encryption
```

Runtime data (settings, database, logs) lives in `%LOCALAPPDATA%\JARVIS`. API keys are stored encrypted with Windows DPAPI and is never written to this repository.

## Development

Requirements: Windows 10/11, Python 3.12, Node 22, Rust (stable MSVC), Visual Studio C++ Build Tools.

```powershell
powershell -ExecutionPolicy Bypass -File setup\install-deps.ps1
npm install
npx tauri dev
```

Then open **Settings** in the app and paste your API key.

Tests:

```powershell
.venv\Scripts\python.exe -m unittest backend.tests.test_memory backend.tests.test_api -v
```
