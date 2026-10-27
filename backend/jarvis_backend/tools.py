"""Tools the AI can call. Each maps to a desktop-agent action or a backend capability.

risk:
  "auto"    - runs immediately (read-only or easily undone)
  "confirm" - asks the user first, unless the owner turned on Full control in Settings
  "danger"  - always asks the user first, Full control or not
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
    ToolSpec("read_webpage", "Read one web page or online PDF the user names or that came up in research, and get the passages "
             "answering question (or the start of the page). Use it to check a source or follow a link. Public internet only.",
             obj({"url": S, "question": S}, ["url"]), timeout=60, label="Reading {url}"),
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
    # Precise control: controls by their real text (Windows UI Automation, then OCR), windows, drag, waiting.
    ToolSpec("click_text", "Press a button, menu item, tab, link or label by the exact text it shows, in a window (by title or app "
             "name; default: the frontmost app). Uses Windows UI Automation, which presses the control itself, and falls back to "
             "reading the window with OCR; faster and more reliable than screen_click and needs no AI call, so prefer it whenever "
             "the target has visible text. index picks among several matches (0 = best); button right or double=true use the mouse.",
             obj({"text": S, "window": S, "index": I, "button": S, "double": B}, ["text"]), agent_action="click_text", timeout=60,
             label="Pressing \"{text}\""),
    ToolSpec("type_into", "Type into a field by its label, in a window (by title or app; default the frontmost app), e.g. label "
             "'Search', 'Email', 'To', 'Message'. Sets the value through Windows UI Automation where it can, else focuses the "
             "field and types. submit=true presses Enter after. More reliable than clicking a field and using type_text.",
             obj({"label": S, "text": S, "window": S, "index": I, "submit": B}, ["label", "text"]), agent_action="type_into",
             timeout=60, label="Typing into {label}"),
    ToolSpec("find_controls", "List the controls in a window whose visible name contains text (all named controls when text is "
             "empty): name, type (Button, MenuItem, Edit, ListItem...), position, enabled. Use it to see what can be pressed.",
             obj({"window": S, "text": S, "limit": I}), agent_action="find_controls", timeout=60, label="Looking at the controls"),
    ToolSpec("read_window", "Read all the visible text of a window (by title or app name; default: the frontmost app) with Windows' "
             "built-in OCR. Free and local; use it instead of look_at_screen when only the text matters.",
             obj({"window": S}), agent_action="read_window", timeout=60, label="Reading the window"),
    ToolSpec("list_windows", "List the open app windows, front to back: title, app, minimized, which one is active.",
             obj(), agent_action="list_windows", label="Checking open windows"),
    ToolSpec("window_control", "Bring a window to the front (focus), or minimize, maximize, restore or close it. window is part of "
             "its title or the app name, e.g. 'chrome', 'Downloads', 'Spotify'.",
             obj({"action": S, "window": S}, ["action", "window"]), agent_action="window_control", label="{action} {window}"),
    ToolSpec("wait_for_window", "Wait up to 15 s for a window whose title contains `title` to come to the front (or, with no title, for "
             "the front window to change), e.g. after opening an app or a dialog. Returns what is in front.",
             obj({"title": S, "seconds": I}), agent_action="wait_for_window", timeout=25, label="Waiting for {title}"),
    ToolSpec("screen_drag", "Drag something visible to somewhere visible, by describing both, e.g. from 'the report.pdf icon' to "
             "'the Documents folder in the sidebar', or a slider handle to its right end.",
             obj({"source": S, "target": S}, ["source", "target"]), timeout=120, label="Dragging {source} to {target}"),
    ToolSpec("select_files", "Open a folder in File Explorer and SELECT the matching files there, e.g. 'open Downloads and select all "
             "the zip files' = folder 'Downloads', extension 'zip'. name_contains filters by name. Use this instead of clicking "
             "or keyboard shortcuts to select files.",
             obj({"folder": S, "extension": S, "name_contains": S, "include_folders": B}, ["folder"]), agent_action="select_files",
             timeout=75, label="Selecting files in {folder}"),
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
    # Full control, like a developer at their own terminal: commands, files, processes.
    ToolSpec("run_command", "Run a PowerShell command on this PC and get its real output: anything a power user would do in a terminal "
             "(git, python, npm, winget install/upgrade, pip, building or running code, system queries, editing settings via commands, "
             "managing services). cwd is the folder to run in (default: home); shell 'cmd' for cmd.exe syntax; timeout seconds "
             "(default 120, max 600); background=true starts a long-running process (a server, a watcher) and returns its pid and log. "
             "Commands that delete, uninstall, kill or reset ask the user first; ones that would break Windows are refused.",
             obj({"command": S, "cwd": S, "shell": S, "timeout": I, "background": B}, ["command"]),
             risk="confirm", agent_action="run_command", timeout=620, label="Run: {command}"),
    ToolSpec("read_file", "Read a text or code file with line numbers. offset is the first line (default 1), limit the number of lines "
             "(default 400). Read before you edit.",
             obj({"path": S, "offset": I, "limit": I}, ["path"]), agent_action="read_file", label="Reading {path}"),
    ToolSpec("write_file", "Create a text or code file, or replace its whole content (append=true adds to the end). The old version is "
             "backed up first, so it can be restored.",
             obj({"path": S, "content": S, "append": B}, ["path", "content"]), risk="confirm", agent_action="write_file",
             label="Write {path}"),
    ToolSpec("edit_file", "Change part of a file: replace old_text (copied exactly from read_file, without the line numbers, and unique "
             "in the file) with new_text; replace_all=true for every occurrence. The old version is backed up first.",
             obj({"path": S, "old_text": S, "new_text": S, "replace_all": B}, ["path", "old_text", "new_text"]),
             risk="confirm", agent_action="edit_file", label="Edit {path}"),
    ToolSpec("list_processes", "List running processes with pid, memory and CPU, heaviest first (sort: cpu or memory); query filters by name.",
             obj({"query": S, "sort": S, "limit": I}), agent_action="list_processes", label="Checking processes"),
    ToolSpec("kill_process", "End a running process by pid (from list_processes), or every process with an executable name.",
             obj({"pid": I, "name": S}), risk="confirm", agent_action="kill_process", label="End process {name}{pid}"),
    ToolSpec("lock_screen", "Lock the PC immediately.", obj(), agent_action="lock_screen", label="Locking the PC"),
    ToolSpec("sleep_pc", "Put the PC to sleep.", obj(), risk="confirm", agent_action="sleep_pc", label="Put the PC to sleep"),
    ToolSpec("power", "Schedule a restart or shutdown after delay_seconds (default 60), or cancel a pending one. action: restart, shutdown, cancel.",
             obj({"action": S, "delay_seconds": I}, ["action"]), risk="danger", agent_action="power", label="Power: {action}"),
    ToolSpec("open_settings", "Open a Windows Settings page (display, sound, bluetooth, wifi, network, battery, power, storage, apps, default apps, notifications, windows update, privacy, personalization, themes, mouse, keyboard, time, language, accounts, about, printers, focus, night light) or 'task manager' / 'control panel'.",
             obj({"page": S}), agent_action="open_settings", label="Opening settings"),
    ToolSpec("set_reminder", "Set a reminder that JARVIS says out loud at a local date-time. due must be ISO format YYYY-MM-DDTHH:MM in the user's local time.",
             obj({"text": S, "due": S}, ["text", "due"]), label="Setting a reminder"),
    # Documents, read and made on this PC (no AI service involved in the reading or the making)
    ToolSpec("read_document", "Read a PDF, Word, Excel, PowerPoint, text file or image (by path or file-store id). With question: "
             "the passages that best answer it, each with its page/slide/sheet; with pages ('3' or '2-5'): those parts; with "
             "neither: the start. Quote the exact words you use and say where they are.",
             obj({"file": S, "question": S, "pages": S}, ["file"]), agent_action="read_document", timeout=120,
             label="Reading {file}"),
    ToolSpec("store_file", "Keep a file in JARVIS's own file store: copied and read once, so later questions about it are instant "
             "and work even if the original moves. Use when the user will ask about a document more than once.",
             obj({"path": S}, ["path"]), agent_action="store_file", timeout=120, label="Keeping {path}"),
    ToolSpec("stored_files", "List the files in JARVIS's file store (id, name, when added).", obj(), agent_action="stored_files",
             label="Checking stored files"),
    ToolSpec("forget_file", "Remove a file from JARVIS's file store (the original is not touched).", obj({"id": S}, ["id"]),
             agent_action="forget_file", label="Removing {id} from the store"),
    ToolSpec("create_document", "Create a Word (docx), Excel (xlsx), PowerPoint (pptx) or PDF file. content is markdown-style "
             "text: # / ## / ### headings, - bullets, | tables |, paragraphs. PowerPoint: each # or ## heading starts a slide "
             "with the lines under it as bullets. Excel: each # heading starts a sheet; rows are | table | lines or "
             "comma-separated lines, the first row is the header. Saved to Documents\\JARVIS unless path is given; never "
             "overwrites. open=true opens it after.",
             obj({"kind": S, "title": S, "content": S, "path": S, "open": B}, ["kind", "content"]), risk="confirm",
             agent_action="create_document", timeout=120, label="Creating {kind}: {title}"),
    ToolSpec("set_plan", "For a job of three or more steps, post the plan first (the user sees it in a task panel and can pause, "
             "take over or stop it), then call again with current set to the index of the step you are starting as you go. "
             "steps: short imperative phrases. Re-plan with new steps if the plan changes.",
             obj({"steps": {"type": "array", "items": S}, "current": I}, ["steps"]), label="Planning"),
    ToolSpec("set_alarm", "Set an alarm JARVIS says out loud: at is a time today (HH:MM, 24-hour; tomorrow if already past) or "
             "an ISO date-time YYYY-MM-DDTHH:MM; label is optional (e.g. 'gym').",
             obj({"at": S, "label": S}, ["at"]), label="Setting an alarm for {at}"),
    ToolSpec("set_timer", "Start a countdown timer that JARVIS announces when it ends: minutes and/or seconds, optional label "
             "(e.g. 'tea', 'pasta').", obj({"minutes": I, "seconds": I, "label": S}), label="Starting a timer"),
    ToolSpec("list_reminders", "List pending reminders, alarms and timers (timers show the time left).", obj(), label="Checking reminders"),
    ToolSpec("cancel_reminder", "Cancel a pending reminder, alarm or timer by its id.", obj({"id": S}, ["id"]), label="Cancelling a reminder"),
    # System extras
    ToolSpec("hardware_info", "Graphics card (name, load, memory, temperature) and temperatures of this PC, best effort.",
             obj(), agent_action="hardware_info", timeout=40, label="Checking the hardware"),
    ToolSpec("autostart", "Whether JARVIS starts when the user signs in to Windows: action status, enable or disable.",
             obj({"action": S}, ["action"]), agent_action="autostart", label="Start with Windows: {action}"),
    ToolSpec("copy_path", "Copy a file or folder to another folder or name. Never overwrites.",
             obj({"source": S, "destination": S}, ["source", "destination"]), risk="confirm", agent_action="copy_path",
             label="Copy {source} to {destination}"),
    ToolSpec("recent_files", "Files the user opened recently, newest first; optional extension (e.g. 'pdf') or name query. Use for "
             "'the file I was working on', 'that PDF from yesterday'.",
             obj({"extension": S, "query": S, "limit": I}), agent_action="recent_files", timeout=40, label="Checking recent files"),
    ToolSpec("recent_downloads", "The newest finished files in the Downloads folder (programs flagged). Use for 'what did I just "
             "download', 'open the file I downloaded'.", obj({"limit": I}), agent_action="recent_downloads", label="Checking downloads"),
]

# Repeating these is normal (another key press, the next track), never a loop.
STEPWISE_TOOLS = frozenset({"set_plan", "press_keys", "type_text", "volume", "media", "brightness", "web_click", "web_type", "screen_click", "screen_scroll",
                            "click_text", "window_control", "screen_drag", "wait_for_window", "type_into"})
# Read-only looks whose answer can change after other actions.
OBSERVE_TOOLS = frozenset({"look_at_screen", "system_status", "clipboard_get", "list_folder", "find_files", "list_reminders", "web_read",
                           "gmail_list", "gmail_open", "whatsapp_chats", "whatsapp_read", "find_apis", "call_api",
                           "read_file", "list_processes", "run_command", "find_controls", "read_window", "list_windows",
                           "hardware_info", "recent_files", "recent_downloads",
                           "read_document", "stored_files", "read_webpage"})

BY_NAME = {t.name: t for t in TOOLS}


def describe_call(spec: ToolSpec, args: dict[str, Any]) -> str:
    class Safe(dict):
        def __missing__(self, k: str) -> str:
            return ""

    shown = {k: (" + ".join(v) if isinstance(v, list) else str(v)) for k, v in args.items()}
    return (spec.label or spec.name).format_map(Safe(shown)).strip()
