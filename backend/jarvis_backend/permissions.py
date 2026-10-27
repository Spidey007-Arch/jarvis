"""Per-action permissions: Allow, Ask or Block for each kind of thing JARVIS can do.

Every tool call is mapped to a capability (or to none, for harmless and read-only steps), and the user's
policy for that capability decides whether it runs, asks first, or is refused. A few capabilities are
locked to at least Ask: they can be blocked, never silently allowed. Anyone else's install starts with
every change on Ask; the owner's "Full control" turns the everyday ones to Allow in one go."""
from __future__ import annotations

import re
from typing import Any

from .tools import ToolSpec

# id -> (what the user sees, locked to at least Ask)
CAPABILITIES: dict[str, tuple[str, bool]] = {
    "change_files": ("Create, edit, copy and move files", False),
    "delete_files": ("Delete files (to the Recycle Bin, recoverable)", False),
    "erase_data": ("Erase for good: delete commands, git reset/force-push, dropping tables", False),
    "run_commands": ("Run commands in PowerShell", False),
    "end_processes": ("Close apps and end processes", False),
    "close_windows": ("Close windows", False),
    "install_software": ("Install or uninstall software", True),
    "system_changes": ("Change system settings: registry, services, network, start-up", True),
    "power": ("Restart, shut down or sleep the PC", True),
    "send_or_pay": ("Press Send, Pay, Buy, Post, Delete or Confirm in an app", True),
    "see_screen": ("Look at and read the screen", False),
    "read_messages": ("Read Gmail and WhatsApp", False),
    "downloads": ("Download files", False),
    "location": ("Know where this PC is (for maps, directions and places near you)", False),
    "connectors": ("Act through connectors (Slack, Notion, GitHub...): reading is always allowed", False),
}
POLICIES = ("allow", "ask", "block")
ALLOWED_BY_DEFAULT = frozenset({"see_screen", "read_messages", "downloads"})
# What "Full control" (the owner's own PC) allows without asking.
FULL_CONTROL = frozenset({"change_files", "delete_files", "run_commands", "end_processes", "close_windows", "location"})

RISKY_CLICK = re.compile(
    r"\b(send|pay|payment|buy|purchase|order|checkout|check out|delete|remove|uninstall|erase|format|wipe|submit|post|publish|tweet|"
    r"transfer|withdraw|confirm|accept|agree|allow|grant|sign ?out|log ?out|shut ?down|restart|discard|unsubscribe|reset|install|"
    r"forward|reply all|empty trash|clear (all|history|data))\b", re.I)

# What a PowerShell command does, most serious first.
_COMMANDS: list[tuple[str, re.Pattern[str]]] = [
    ("power", re.compile(r"\b(restart-computer|stop-computer|shutdown(\.exe)?|logoff)\b", re.I)),
    ("install_software", re.compile(r"\b(winget|choco|scoop|msiexec|add-appxpackage|remove-appxpackage|install-package|"
                                    r"uninstall-package)\b|\buninstall\b", re.I)),
    ("system_changes", re.compile(r"\breg(\.exe)?\s+(add|delete|import)\b|\b(set-itemproperty|new-itemproperty|remove-itemproperty|"
                                  r"netsh|set-executionpolicy|stop-service|set-service|start-service|sc(\.exe)?\s+(config|delete|stop)|"
                                  r"schtasks|bcdedit|set-netfirewall\w*|new-netfirewall\w*)\b|\bdisable-\w+", re.I)),
    ("erase_data", re.compile(r"\b(remove-item|rm|rmdir|rd|del|erase|ri|clear-recyclebin|format-volume)\b|"
                              r"git\s+(push\b.*(\s-f\b|--force)|reset\s+--hard|clean\s+-[a-z]*f)|drop\s+(table|database)|truncate\s+table", re.I)),
    ("end_processes", re.compile(r"\b(stop-process|taskkill|kill)\b", re.I)),
]

_SCREEN = frozenset({"look_at_screen", "screenshot", "read_window", "screen_click", "screen_drag", "click_text", "find_controls"})
_MESSAGES = frozenset({"gmail_list", "gmail_open", "whatsapp_chats", "whatsapp_read"})
_CHANGE_FILES = frozenset({"write_file", "edit_file", "create_folder", "move_path", "copy_path", "organize_folder", "create_document"})


def _near_me(name: str, args: dict[str, Any]) -> bool:
    where = str(args.get("near" if name == "maps_search" else "origin") or "me").strip().lower()
    return where in ("me", "here", "my location", "current location")


def capability(spec: ToolSpec, args: dict[str, Any]) -> str | None:
    """The kind of action a tool call is, or None when it needs no permission."""
    name = spec.name
    if spec.risk in ("connector", "connector_read"):  # a connector tool: reading needs no permission, changing does
        return None if spec.risk == "connector_read" else "connectors"
    if name == "screen_click" and RISKY_CLICK.search(str(args.get("target", ""))) or \
            name == "click_text" and RISKY_CLICK.search(str(args.get("text", ""))):
        return "send_or_pay"
    if name in _SCREEN or name == "type_into" or name == "screen_scroll" and args.get("over"):
        return "see_screen"
    if name in _MESSAGES:
        return "read_messages"
    if name == "download_file":
        return "downloads"
    if name == "my_location" or name in ("maps_search", "maps_directions") and _near_me(name, args):
        return "location"
    if name == "organize_folder":
        return None if args.get("dry_run", True) is not False else "change_files"  # a dry run only reports a plan
    if name in _CHANGE_FILES:
        return "change_files"
    if name == "delete_path":
        return "delete_files"
    if name in ("kill_process", "close_app"):
        return "end_processes"
    if name == "window_control":
        return "close_windows" if str(args.get("action", "")).lower() == "close" else None
    if name in ("power", "sleep_pc"):
        return "power"
    if name == "autostart":
        return None if str(args.get("action", "status")).lower() == "status" else "system_changes"
    if name == "run_command":
        cmd = str(args.get("command", ""))
        return next((cap for cap, rx in _COMMANDS if rx.search(cmd)), "run_commands")
    if spec.risk in ("confirm", "danger"):
        return "system_changes"  # a risky tool not mapped above: treat it as seriously as possible
    return None


def policy(cap: str, saved: dict[str, str] | None, full_control: bool) -> str:
    """allow, ask or block for one capability."""
    label_locked = CAPABILITIES.get(cap, ("", True))
    chosen = (saved or {}).get(cap)
    if chosen not in POLICIES:
        chosen = "allow" if cap in ALLOWED_BY_DEFAULT or (full_control and cap in FULL_CONTROL) else "ask"
    if label_locked[1] and chosen == "allow":
        return "ask"
    return chosen


def decide(spec: ToolSpec, args: dict[str, Any], saved: dict[str, str] | None = None, full_control: bool = False) -> tuple[str, str | None]:
    """(allow | ask | block, capability) for one tool call."""
    cap = capability(spec, args)
    if cap is None:
        return "allow", None
    return policy(cap, saved, full_control), cap


def overview(saved: dict[str, str] | None, full_control: bool) -> list[dict[str, Any]]:
    """Every capability with its current policy, for Settings."""
    return [{"id": cap, "label": label, "locked": locked, "policy": policy(cap, saved, full_control)}
            for cap, (label, locked) in CAPABILITIES.items()]


def blocked_message(cap: str) -> str:
    return (f"Blocked in Settings > Permissions ({CAPABILITIES[cap][0]}), so nothing was done. "
            "Tell the user they can change it there.")
