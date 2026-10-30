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
    "list_windows", "window_control", "volume", "media", "play_music", "maps_search", "my_location",
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
    "write": "write_file edit_file", "code": "write_file edit_file run_command run_sandboxed", "analyse": "run_sandboxed", "analyze": "run_sandboxed",
    "calculate": "run_sandboxed", "csv": "run_sandboxed read_document", "data": "run_sandboxed", "sandbox": "run_sandboxed", "screenshot": "screenshot", "scroll": "screen_scroll",
    "drag": "screen_drag", "type": "type_into type_text", "fill": "type_into", "field": "type_into", "form": "type_into", "select": "select_files", "startup": "autostart", "start": "autostart",
    "word": "create_document read_document", "excel": "create_document read_document", "powerpoint": "create_document read_document",
    "document": "create_document read_document", "pdf": "read_document create_document", "report": "create_document read_document",
    "slides": "create_document", "presentation": "create_document", "spreadsheet": "create_document read_document",
    "file": "read_document find_files", "read": "read_document read_webpage",
    "near": "maps_search my_location", "nearby": "maps_search", "location": "my_location maps_search", "where": "my_location",
    "map": "maps_search maps_directions", "directions": "maps_directions", "route": "maps_directions", "distance": "maps_directions",
    "restaurant": "maps_search", "cafe": "maps_search", "hotel": "maps_search", "hospital": "maps_search",
    "link": "read_webpage", "url": "read_webpage", "page": "read_webpage", "website": "read_webpage", "article": "read_webpage", "store": "store_file stored_files",
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


def find(need: str, limit: int = 8, exclude: set[str] | frozenset[str] = frozenset(),
         extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Tool schemas matching a description of what is needed, best first (extra: connectors' tools too)."""
    want = _expand(need)
    pool = {name: (words, _BY_NAME[name].schema()) for name, words in _INDEX.items()}
    for t in extra or []:  # a connector's name counts strongly: "on slack" should bring Slack's tools
        server = t["name"].split("__")[0]
        pool[t["name"]] = (_words(t["name"].replace("__", " ").replace("_", " ") + " " + t["description"]) | {server}, t)
    scored = []
    for name, (words, _schema) in pool.items():
        if name in exclude:
            continue
        score = len(want & words) + (3 if name in want or name.split("__")[0] in want else 0)
        if score:
            scored.append((score, name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [pool[n][1] for _, n in scored[:limit]]


def select(request: str, recent: str = "", extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The tools to send with a request: the core set, what matches it, and more_tools."""
    core = [_BY_NAME[n].schema() for n in CORE if n in _BY_NAME]
    matched = find(request + " " + recent, limit=EXTRA, exclude=frozenset(CORE), extra=extra)
    return core + matched + [MORE_TOOLS_SCHEMA]
