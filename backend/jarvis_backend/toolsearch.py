"""Tool search: send each request only the tools it is likely to need.

JARVIS has around seventy tools; describing all of them in every request costs thousands of tokens. A
request gets a small core set that covers everyday work, plus the tools whose name and description match
what was asked (with a few synonyms, e.g. "mail" finds the Gmail tools). If JARVIS needs something it was
not given, it calls more_tools and the matching tools are added from the next step on.

Live voice sessions still get every tool: Gemini Live fixes its tool list when the session opens."""
from __future__ import annotations

import re
from typing import Any

from .tools import TOOLS

MORE_TOOLS = "more_tools"
MORE_TOOLS_SCHEMA: dict[str, Any] = {
    "name": MORE_TOOLS,
    "description": "You were given only the tools this request seemed to need. If you need one you don't have (e.g. Gmail, "
                   "WhatsApp, shopping, files, windows, power, alarms, public APIs, hardware, processes), describe what you need to "
                   "do and the matching tools are added for your next step.",
    "parameters": {"type": "object", "properties": {"need": {"type": "string"}}, "required": ["need"]},
}

CORE = (
    "system_status", "open_app", "open_url", "web_search", "web_research", "look_at_screen", "click_text", "screen_click",
    "type_text", "press_keys", "find_files", "open_path", "read_file", "run_command", "set_reminder", "set_plan",
    "list_windows", "window_control", "volume", "media",
)
EXTRA = 12  # matched tools added on top of the core set

# Words people use that don't appear in the tool descriptions.
SYNONYMS = {
    "mail": "gmail email inbox", "email": "gmail", "inbox": "gmail", "whatsapp": "whatsapp chats", "message": "whatsapp gmail",
    "chat": "whatsapp", "buy": "web_open web_click cart order", "order": "web_open cart", "cart": "web_open web_read",
    "amazon": "web_open", "flipkart": "web_open", "zomato": "web_open", "swiggy": "web_open", "food": "web_open",
    "weather": "find_apis call_api", "crypto": "call_api find_apis", "price": "call_api find_apis", "news": "find_apis web_research",
    "api": "find_apis call_api", "alarm": "set_alarm", "timer": "set_timer", "wake": "set_alarm", "remind": "set_reminder list_reminders",
    "delete": "delete_path", "remove": "delete_path", "move": "move_path", "rename": "move_path", "copy": "copy_path",
    "folder": "list_folder create_folder organize_folder", "organize": "organize_folder", "tidy": "organize_folder",
    "download": "download_file recent_downloads", "downloaded": "recent_downloads", "recent": "recent_files recent_downloads",
    "shutdown": "power", "restart": "power", "sleep": "sleep_pc", "lock": "lock_screen", "brightness": "brightness",
    "settings": "open_settings", "bluetooth": "open_settings", "wifi": "open_settings", "clipboard": "clipboard_get clipboard_set",
    "paste": "clipboard_get", "gpu": "hardware_info", "temperature": "hardware_info", "hot": "hardware_info",
    "process": "list_processes kill_process", "kill": "kill_process", "close": "close_app window_control", "edit": "edit_file write_file",
    "write": "write_file edit_file", "code": "write_file edit_file run_command", "screenshot": "screenshot", "scroll": "screen_scroll",
    "drag": "screen_drag", "select": "select_files", "startup": "autostart", "start": "autostart", "pdf": "read_pdf",
    "word": "create_document", "excel": "create_document", "powerpoint": "create_document", "document": "create_document",
}

_STOP = {"the", "a", "an", "to", "and", "or", "of", "in", "on", "for", "my", "me", "please", "can", "you", "it", "this", "that",
         "with", "jarvis", "sir", "i", "is", "be", "do", "from", "at", "all", "some", "now", "then", "what", "how", "get"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in _STOP and len(w) > 2}


_INDEX = {t.name: _words(t.name.replace("_", " ") + " " + t.name + " " + t.description) for t in TOOLS}
_BY_NAME = {t.name: t for t in TOOLS}


def _expand(text: str) -> set[str]:
    words = _words(text)
    for w in list(words):
        for key, more in SYNONYMS.items():
            if w.startswith(key):
                words |= set(more.split())
    return words


def find(need: str, limit: int = 8, exclude: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Tool schemas matching a description of what is needed, best first."""
    want = _expand(need)
    scored = []
    for name, words in _INDEX.items():
        if name in exclude:
            continue
        score = len(want & words) + (3 if name in want else 0)
        if score:
            scored.append((score, name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [_BY_NAME[n].schema() for _, n in scored[:limit]]


def select(request: str, recent: str = "") -> list[dict[str, Any]]:
    """The tools to send with a request: the core set, what matches it, and more_tools."""
    core = [_BY_NAME[n].schema() for n in CORE if n in _BY_NAME]
    matched = find(request + " " + recent, limit=EXTRA, exclude=frozenset(CORE))
    return core + matched + [MORE_TOOLS_SCHEMA]
