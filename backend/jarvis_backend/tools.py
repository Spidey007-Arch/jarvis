"""Tools the AI can call. Each maps to a desktop-agent action or a backend capability.

risk:
  "auto"    - runs immediately (read-only or easily undone)
  "confirm" - the user must press Allow in the chat before it runs
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: str = "auto"
    agent_action: str | None = None  # None: handled inside the backend
    timeout: float = 20.0
    label: str = ""  # short present-tense description for the UI, may use {arg} placeholders

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def obj(props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props or {}, "required": required or []}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}

TOOLS: list[ToolSpec] = [
    ToolSpec("system_status", "Live diagnostics of this PC: CPU, memory, disks, battery, network, uptime, top processes, and any abnormal flags.",
             obj(), agent_action="system_status", timeout=15, label="Checking system status"),
    ToolSpec("open_app", "Open an installed application by name (fuzzy matched against the Start menu), e.g. 'chrome', 'spotify', 'vs code'.",
             obj({"name": S}, ["name"]), agent_action="open_app", timeout=40, label="Opening {name}"),
    ToolSpec("find_apps", "List installed apps whose names match a query. Use when unsure which app the user means.",
             obj({"query": S}, ["query"]), agent_action="find_apps", timeout=40, label="Looking up apps matching {query}"),
    ToolSpec("close_app", "Close a running application by its executable name, e.g. 'chrome', 'notepad', 'spotify'. Unsaved work in it may be lost.",
             obj({"name": S}, ["name"]), risk="confirm", agent_action="close_app", label="Close {name}"),
    ToolSpec("open_url", "Open a web address in the default browser, or in a specific browser (chrome, edge, firefox, brave, opera).",
             obj({"url": S, "browser": S}, ["url"]), agent_action="open_url", label="Opening {url}"),
    ToolSpec("web_search", "Open a search results page in the browser for the user to look at. Engines: google, bing, duckduckgo, youtube, github, wikipedia, amazon, maps.",
             obj({"query": S, "engine": S, "browser": S}, ["query"]), agent_action="web_search", label="Searching for {query}"),
    ToolSpec("web_research", "Search the web yourself and read the top results to answer a factual or current-events question directly, with sources. Use this instead of web_search when the user wants an answer, not a browser tab.",
             obj({"query": S}, ["query"]), timeout=60, label="Researching {query}"),
    # Shopping and food ordering run in JARVIS's own browser window (its own profile; the user signs in there once).
    ToolSpec("web_open", "Open a page in JARVIS's shopping browser window, e.g. a store's search URL: https://www.amazon.in/s?k=..., "
             "https://www.flipkart.com/search?q=..., https://www.swiggy.com/search?query=..., https://www.zomato.com, https://www.dominos.co.in. "
             "Use this (not open_url) when the user wants something bought or ordered.",
             obj({"url": S}, ["url"]), agent_action="web_open", timeout=45, label="Opening {url}"),
    ToolSpec("web_read", "Read the shopping window's current page: title, some text, and numbered controls (links, buttons, inputs). "
             "Pass find to list only controls whose label contains it (e.g. 'add to cart'); offset pages through more controls.",
             obj({"find": S, "offset": I}), agent_action="web_read", timeout=30, label="Reading the page"),
    ToolSpec("web_click", "Click control n from the last web_read (numbers change after every read). Paying or placing the order is "
             "refused by design: stop at the cart or checkout and tell the user to review it and pay themselves.",
             obj({"n": I}, ["n"]), agent_action="web_click", timeout=30, label="Clicking on the page"),
    ToolSpec("web_type", "Type into input n from the last web_read, e.g. a search box or a delivery address field; submit=true presses "
             "Enter. Password, card, OTP and UPI fields are refused by design.",
             obj({"n": I, "text": S, "submit": B}, ["n", "text"]), agent_action="web_type", timeout=30, label="Typing on the page"),
    ToolSpec("volume", "Get or change the system volume. action: get, set (with level 0-100), up, down (step default 10), mute, unmute.",
             obj({"action": S, "level": I, "step": I}, ["action"]), agent_action="volume", label="Adjusting volume"),
    ToolSpec("media", "Control whatever media is playing. action: play_pause, next, previous, stop.",
             obj({"action": S}, ["action"]), agent_action="media", label="Media: {action}"),
    ToolSpec("brightness", "Get the screen brightness, or set it with level 0-100. Works on laptop screens.",
             obj({"level": I}), agent_action="brightness", label="Adjusting brightness"),
    ToolSpec("look_at_screen", "Take a screenshot and look at it to answer a question about what is currently on screen (errors, windows, content).",
             obj({"question": S}, ["question"]), timeout=90, label="Looking at the screen"),
    ToolSpec("screenshot", "Save a screenshot to Pictures\\JARVIS and return its path. It is deleted automatically after 3 minutes; tell the user.", obj(), agent_action="screenshot", label="Taking a screenshot"),
    # Every API in github.com/public-apis/public-apis: weather, news, finance, crypto, sports, games, science, ...
    ToolSpec("find_apis", "Search the catalog of ~2,200 free public APIs (the public-apis list) by topic, e.g. 'weather forecast', "
             "'crypto prices', 'cricket scores', 'jokes', 'currency exchange', 'nasa', 'dictionary', 'recipes', 'movies'. Returns name, "
             "what it does, whether it needs a key, and its docs URL. Optional category (e.g. Weather, Finance, Sports & Fitness) and "
             "no_key_only=true for APIs usable right now.",
             obj({"query": S, "category": S, "no_key_only": B, "limit": I}), label="Looking up APIs for {query}"),
    ToolSpec("call_api", "Make a read-only GET request to a public API endpoint (or read its docs page to learn the endpoints) and get "
             "the JSON or text back. params are query parameters, as JSON ({\"q\": \"Pune\", \"units\": \"metric\"}) or "
             "'q=Pune&units=metric' (or put them in the URL). For an API whose key the user saved, set use_key to the API's name "
             "and say where it goes: key_param (query parameter name, e.g. 'appid', 'apiKey') or key_header (e.g. 'X-Api-Key'); "
             "you never see the key. Only the public internet is reachable.",
             obj({"url": S, "params": S, "use_key": S, "key_param": S, "key_header": S}, ["url"]),
             timeout=45, label="Calling {url}"),
    # Using any app by sight, like a person: look, click, type, scroll.
    ToolSpec("screen_click", "Click something visible on screen, in any app, by describing it: e.g. 'the Search box in the Spotify window', "
             "'the Play button', 'the chat named Mom in WhatsApp', 'File menu in Notepad'. JARVIS takes a fresh screenshot, finds it and "
             "clicks its centre. button: left (default), right, middle; double=true to double-click. Then use type_text / press_keys to "
             "type, and look_at_screen to check what happened. Clicks that send, pay, delete or confirm something ask the user first.",
             obj({"target": S, "button": S, "double": B}, ["target"]), timeout=90, label="Clicking {target}"),
    ToolSpec("screen_scroll", "Scroll the mouse wheel: direction up or down, amount in wheel steps (default 5), optionally over a described "
             "area ('the chat list', 'the web page').",
             obj({"direction": S, "amount": I, "over": S}), timeout=90, label="Scrolling {direction}"),
    ToolSpec("clipboard_get","Read the text currently on the clipboard.", obj(), agent_action="clipboard_get", label="Reading the clipboard"),
    ToolSpec("clipboard_set", "Put text on the clipboard.", obj({"text": S}, ["text"]), agent_action="clipboard_set", label="Copying to clipboard"),
    ToolSpec("press_keys", "Press a keyboard shortcut in the focused window, e.g. [\"ctrl\",\"s\"], [\"win\",\"d\"], [\"alt\",\"tab\"].",
             obj({"keys": {"type": "array", "items": S}}, ["keys"]), agent_action="press_keys", label="Pressing {keys}"),
    ToolSpec("type_text", "Type text into the currently focused window.", obj({"text": S}, ["text"]), agent_action="type_text", label="Typing text"),
    ToolSpec("find_files", "Search the user's Desktop, Documents, Downloads, Pictures, Music and Videos (or a given folder) for files or folders whose name contains the query.",
             obj({"query": S, "folder": S, "extension": S, "limit": I}, ["query"]), agent_action="find_files", timeout=20, label="Searching files for {query}"),
    ToolSpec("open_path", "Open a file or folder with its default app. Relative paths start in the user's home; 'Downloads/x' works.",
             obj({"path": S}, ["path"]), agent_action="open_path", label="Opening {path}"),
    ToolSpec("download_file", "Download a file from a direct link (PDF, document, image, video, audio, archive) into Downloads\\JARVIS and open it "
             "on screen with its default Windows app (open=false to only save it). Programs and scripts are refused by design.",
             obj({"url": S, "open": B}, ["url"]), agent_action="download_file", timeout=600, label="Downloading {url}"),
    # Gmail and WhatsApp are read in JARVIS's browser window, where the user signs in / links once. Read-only.
    ToolSpec("gmail_list", "List the user's newest emails as numbered rows (sender, subject, snippet, date, unread). query is Gmail search "
             "syntax; the default 'in:inbox category:primary' leaves out promotions and social. Examples: 'is:unread category:primary', "
             "'is:important newer_than:2d', 'from:amazon', 'has:attachment'. Use this whenever the user asks about their email.",
             obj({"query": S, "limit": I}), agent_action="gmail_list", timeout=60, label="Checking Gmail"),
    ToolSpec("gmail_open", "Open and read email n from the last gmail_list: the newest messages of the thread, with attachment names.",
             obj({"n": I}, ["n"]), agent_action="gmail_open", timeout=40, label="Reading an email"),
    ToolSpec("whatsapp_chats", "List the user's recent WhatsApp chats (name, last message, time, unread count) from WhatsApp Web; "
             "unread_only=true for just the unread ones.",
             obj({"unread_only": B, "limit": I}), agent_action="whatsapp_chats", timeout=120, label="Checking WhatsApp"),
    ToolSpec("whatsapp_read", "Read the latest messages of one WhatsApp chat by contact or group name (opening it marks it read on the phone).",
             obj({"chat": S, "limit": I}, ["chat"]), agent_action="whatsapp_read", timeout=120, label="Reading WhatsApp: {chat}"),
    ToolSpec("list_folder", "List what is inside a folder.", obj({"path": S}, ["path"]), agent_action="list_folder", label="Listing {path}"),
    ToolSpec("create_folder", "Create a folder (and any missing parents).", obj({"path": S}, ["path"]), agent_action="create_folder", label="Creating folder {path}"),
    ToolSpec("move_path", "Move or rename a file or folder. Never overwrites.", obj({"source": S, "destination": S}, ["source", "destination"]),
             risk="confirm", agent_action="move_path", label="Move {source} to {destination}"),
    ToolSpec("delete_path", "Move a file or folder to the Recycle Bin (recoverable).", obj({"path": S}, ["path"]),
             risk="confirm", agent_action="delete_path", label="Delete {path} (to Recycle Bin)"),
    ToolSpec("organize_folder", "Sort loose files in a folder into category subfolders (Images, Documents, Videos, Audio, Archives, Installers, Code, Other). Call with dry_run=true first to preview, then dry_run=false to do it.",
             obj({"path": S, "dry_run": B}, ["path", "dry_run"]), risk="confirm", agent_action="organize_folder", label="Organize {path}"),
    ToolSpec("lock_screen", "Lock the PC immediately.", obj(), agent_action="lock_screen", label="Locking the PC"),
    ToolSpec("sleep_pc", "Put the PC to sleep.", obj(), risk="confirm", agent_action="sleep_pc", label="Put the PC to sleep"),
    ToolSpec("power", "Schedule a restart or shutdown after delay_seconds (default 60), or cancel a pending one. action: restart, shutdown, cancel.",
             obj({"action": S, "delay_seconds": I}, ["action"]), risk="confirm", agent_action="power", label="Power: {action}"),
    ToolSpec("open_settings", "Open a Windows Settings page (display, sound, bluetooth, wifi, network, battery, power, storage, apps, default apps, notifications, windows update, privacy, personalization, themes, mouse, keyboard, time, language, accounts, about, printers, focus, night light) or 'task manager' / 'control panel'.",
             obj({"page": S}), agent_action="open_settings", label="Opening settings"),
    ToolSpec("set_reminder", "Set a reminder that pops up as a Windows notification at a local date-time. due must be ISO format YYYY-MM-DDTHH:MM in the user's local time.",
             obj({"text": S, "due": S}, ["text", "due"]), label="Setting a reminder"),
    ToolSpec("list_reminders", "List pending reminders.", obj(), label="Checking reminders"),
    ToolSpec("cancel_reminder", "Cancel a pending reminder by its id.", obj({"id": S}, ["id"]), label="Cancelling a reminder"),
]

# Repeating these is normal (another key press, the next track), never a loop.
STEPWISE_TOOLS = frozenset({"press_keys", "type_text", "volume", "media", "brightness", "web_click", "web_type", "screen_click", "screen_scroll"})
# Read-only looks whose answer can change after other actions.
OBSERVE_TOOLS = frozenset({"look_at_screen", "system_status", "clipboard_get", "list_folder", "find_files", "list_reminders", "web_read",
                           "gmail_list", "gmail_open", "whatsapp_chats", "whatsapp_read", "find_apis", "call_api"})

BY_NAME = {t.name: t for t in TOOLS}


def describe_call(spec: ToolSpec, args: dict[str, Any]) -> str:
    class Safe(dict):
        def __missing__(self, k: str) -> str:
            return ""

    shown = {k: (" + ".join(v) if isinstance(v, list) else str(v)) for k, v in args.items()}
    return (spec.label or spec.name).format_map(Safe(shown)).strip()
