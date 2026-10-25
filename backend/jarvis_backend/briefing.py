"""The start-up briefing: the first thing JARVIS says when it opens.

facts() turns the agent's system_status, pending reminders, recent alerts and today's AI quota into a
short plain-text report. opening_prompt() asks the live voice model to deliver it out loud in character;
spoken() is the same report as finished sentences, for offline voice or when live voice is unavailable."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def part_of_day(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 22:
        return "evening"
    return "night"


def _apps(status: dict[str, Any]) -> list[str]:
    names = []
    for a in status.get("open_apps") or []:
        name = str(a.get("name", "")).removesuffix(".Root")
        names.append(f"{name} ({a['memory_mb']} MB)" if a.get("memory_mb") else name)
    return names


def facts(status: dict[str, Any] | None, reminders: list[dict[str, Any]], alerts: list[dict[str, Any]],
          quota: dict[str, Any] | None, last_used: str | None, now: datetime) -> str:
    lines = [f"Time: {now:%A %d %B %Y, %H:%M}."]
    if last_used:
        lines.append(f"JARVIS was last used {last_used}.")
    if status:
        mem = status.get("memory") or {}
        lines.append(f"CPU {status.get('cpu_percent', 0):.0f}% across {status.get('cpu_cores', '?')} cores; "
                     f"memory {mem.get('used_percent', 0):.0f}% ({mem.get('used_gb', '?')} of {mem.get('total_gb', '?')} GB); "
                     f"up {status.get('uptime_hours', 0)} hours.")
        for d in status.get("disks") or []:
            lines.append(f"Drive {d['drive']} {d['used_percent']:.0f}% full, {d['free_gb']} GB free.")
        bat = status.get("battery")
        if bat:
            lines.append(f"Battery {bat['percent']}%, {'charging' if bat['charging'] else 'on battery'}"
                         + (f", about {bat['minutes_left']} minutes left." if bat.get("minutes_left") and not bat["charging"] else "."))
        net = status.get("network_kb_per_s") or {}
        if net:
            lines.append(f"Network: {net.get('down', 0)} KB/s down, {net.get('up', 0)} KB/s up.")
        lines.append(f"{status.get('process_count', '?')} processes running.")
        apps = _apps(status)
        lines.append("Open apps: " + (", ".join(apps) if apps else "none besides JARVIS") + ".")
        top_cpu = [f"{p['name']} {p['cpu_percent']}%" for p in status.get("top_cpu") or [] if p.get("cpu_percent", 0) >= 1]
        if top_cpu:
            lines.append("Busiest on CPU: " + ", ".join(top_cpu[:4]) + ".")
        top_mem = [f"{p['name']} {p['memory_mb']} MB" for p in status.get("top_memory") or []]
        if top_mem:
            lines.append("Most memory: " + ", ".join(top_mem[:4]) + ".")
        for f in status.get("flags") or []:
            lines.append(f"Warning: {f}")
    else:
        lines.append("System readings are unavailable: the desktop agent is not connected.")
    if reminders:
        lines.append("Pending reminders: " + "; ".join(f"{r['text']} ({r['due']})" for r in reminders[:5]) + ".")
    else:
        lines.append("No pending reminders.")
    for a in alerts[-3:]:
        lines.append(f"Recent alert: {a['message']}")
    if quota:
        out = [m["model"] for m in quota.get("models", []) if m.get("state") == "out"]
        used = sum(m.get("used", 0) for m in quota.get("models", []))
        lines.append(f"AI requests made today: {used}." + (f" Out of quota: {', '.join(out)}." if out else ""))
    return "\n".join(lines)


def opening_prompt(report: str, address: str, now: datetime) -> str:
    return (
        f"[JARVIS has just been switched on. Speak first: greet {address} for the {part_of_day(now)} and give the "
        "start-up briefing out loud, in character, in about 30 seconds. Order: overall system health in one or two "
        "sentences, then the processes: which apps are open and what is using the most CPU and memory, then anything "
        "abnormal, reminders, and AI quota only if it matters. Round the numbers and skip what is unremarkable; do not "
        "read this like a list. Finish by asking what they need. Do not call any tools.]\n\n" + report
    )


def spoken(status: dict[str, Any] | None, reminders: list[dict[str, Any]], address: str, now: datetime) -> str:
    """The briefing as plain sentences, for the offline voice."""
    out = [f"Good {part_of_day(now)}, {address}. It's {now:%H:%M}."]
    if status:
        mem = status.get("memory") or {}
        out.append(f"Processor at {status.get('cpu_percent', 0):.0f} percent, memory at {mem.get('used_percent', 0):.0f} percent.")
        apps = [str(a["name"]).removesuffix(".Root") for a in status.get("open_apps") or []]
        if apps:
            out.append(f"You have {', '.join(apps[:5])} open, among {status.get('process_count', 'several')} processes.")
        top = (status.get("top_memory") or [{}])[0]
        if top.get("name"):
            out.append(f"{top['name']} is using the most memory, {top['memory_mb']} megabytes.")
        out += status.get("flags") or ["Everything else looks healthy."]
    else:
        out.append("I can't read the system yet; the desktop agent isn't connected.")
    if reminders:
        out.append(f"You have {len(reminders)} reminder{'s' if len(reminders) != 1 else ''} pending; the next is {reminders[0]['text']}.")
    out.append("What do you need?")
    return " ".join(out)
