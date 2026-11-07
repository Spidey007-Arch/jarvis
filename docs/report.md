# JARVIS: a permissioned personal assistant for the Windows desktop

*Technical report, draft. This describes the design of the software; it reports no user study or benchmark.*

## Abstract

Personal AI assistants are useful in proportion to what they can do, and dangerous for the same reason. JARVIS is an
open-source assistant that runs on a Windows PC and can act on it. This report describes how it is built, and in
particular how it separates what the AI decides from what is allowed to happen. Every tool call passes through a policy
that the AI can't change, content from other people is kept apart from instructions, and requests from other AI systems
are held to a stricter rule than requests from the user.

## 1. Architecture

JARVIS is three processes on one machine. A Tauri shell draws the window. A Python backend (FastAPI) holds the agent
loop, memory and schedules. A separate Python desktop agent performs actions on the PC, and connects to the backend over
a local WebSocket. The split means the part that talks to AI providers never touches the desktop directly. All traffic
stays on `127.0.0.1`, and each start creates a random token that every request must carry.

The AI provider is chosen by the user. Gemini, OpenAI, Anthropic, OpenRouter, Ollama and any OpenAI-compatible service
sit behind one interface, so no feature depends on one vendor. Settings and keys are encrypted with Windows DPAPI.

## 2. The permission model

Each tool call maps to a capability, such as `change_files`, `run_commands` or `send_or_pay`, and the user's policy for
that capability gives allow, ask or block. Serious capabilities are locked: they can be blocked, but never allowed
without asking.

Three rules go beyond a plain allow-list:

- **Information flow.** Once a request has read private data, any tool that can send data out needs approval, whatever
  its usual policy. This addresses the pattern where injected text in an email asks the assistant to send data elsewhere.
- **Provenance.** Tool results written by other people carry a marker that they are data, and are never instructions.
- **Who is asking.** A request that arrives through MCP or A2A sets a flag for its whole run, and an action that would
  otherwise be allowed needs approval instead. The user's standing permission is theirs, and doesn't extend to other AI
  systems. This holds even with the owner's Full control setting on.

## 3. Working over time

JARVIS has five levels of unattended work: reminders, automations, a heartbeat, agents and workflows. They share one
path: each run is an ordinary request through the same agent loop as the chat box, so a permission added to chat holds
everywhere.

An **agent** keeps its state in a notebook it writes at the end of each wake. State is therefore plain text the user
can read, edit or clear. The two-part protocol, an all-clear token and a notes line, keeps a quiet wake from leaving
anything behind or interrupting the user. A **workflow** moves control of order from the model to the user: steps
and branches are data, and the model only fills in the steps. Runaway loops end at a fixed step limit.

## 4. Reading across apps

App connections share one small interface: recent, search and read. There is no write path, so a connection can't be
turned into an action by injected text. One search fans out to every connected source and reports failures one by
one, so one broken app doesn't hide the rest. Apps that need OAuth use each user's own registered client with PKCE and
a one-time listener on loopback, so no shared server holds any user's access.

## 5. Research across private and public sources

Multi-round research mixes public pages and private documents, which creates a leak: a model that has read a private
document may put its contents in the next web search. JARVIS plans further searches only from public sources, while
the final answer can still use both.

## 6. Limitations

- The sandbox for untrusted code is a process-level guard, not a virtual machine.
- Data markers rely on the model to respect them. The permission checks are what must hold when it doesn't.
- The agent's approvals assume the user is present to answer. Unanswered requests are declined after two minutes.
- This report has no measurements. A useful next step is a test suite of prompt-injection attacks against the rules in
  section 2, reporting how often each is stopped.

## 7. Conclusion

An assistant with real access can be built so that safety doesn't rest on the model's good behaviour. The rule that
matters most is simple: what the AI wants to do and what it is allowed to do are decided in different places.
