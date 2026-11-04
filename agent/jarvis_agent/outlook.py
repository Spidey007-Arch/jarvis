"""The calendar in classic Outlook on this PC, through Outlook's own automation (COM). No account, key or sign-in
of JARVIS's own: whatever Outlook is signed in to (Outlook.com, Microsoft 365, Exchange, Gmail added to Outlook) is
what JARVIS sees. The new Outlook for Windows has no automation, so only classic Outlook works.

Values go to PowerShell as base64 JSON, never pasted into the script, so a title can't break out of it."""
from __future__ import annotations

import base64
import json
from typing import Any

from . import win
from .actions import ActionError, _str, action

_PREAMBLE = r"""
$ErrorActionPreference = 'Stop'
$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json
try { $ol = New-Object -ComObject Outlook.Application } catch { Write-Output 'NO_OUTLOOK'; exit 0 }
$ns = $ol.GetNamespace('MAPI')
$cal = $ns.GetDefaultFolder(9)
"""

_READ = r"""
$from = [datetime]::Parse($p.from); $to = [datetime]::Parse($p.to)
$items = $cal.Items
$items.IncludeRecurrences = $true
$items.Sort('[Start]')
$f = "[Start] < '" + $to.ToString('g') + "' AND [End] > '" + $from.ToString('g') + "'"
$r = $items.Restrict($f)
$out = @(); $i = $r.GetFirst(); $n = 0
while ($i -and $n -lt 300) {
  $out += [pscustomobject]@{ title = $i.Subject; start = $i.Start.ToString('s'); end = $i.End.ToString('s');
    all_day = [bool]$i.AllDayEvent; location = $i.Location; organizer = $i.Organizer; busy = [int]$i.BusyStatus }
  $n++; $i = $r.GetNext()
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 3
"""

_ADD = r"""
$a = $ol.CreateItem(1)
$a.Subject = $p.title
$a.Start = [datetime]::Parse($p.start)
$a.End = [datetime]::Parse($p.end)
$a.AllDayEvent = [bool]$p.all_day
if ($p.location) { $a.Location = $p.location }
if ($p.notes) { $a.Body = $p.notes }
if ($p.remind_minutes -ge 0) { $a.ReminderSet = $true; $a.ReminderMinutesBeforeStart = [int]$p.remind_minutes } else { $a.ReminderSet = $false }
$a.Save()
Write-Output ('SAVED|' + $a.Start.ToString('s'))
"""

NO_OUTLOOK = ("Classic Outlook isn't installed or can't be opened on this PC (the new Outlook for Windows can't be automated). "
              "Use JARVIS's own calendar, or a Google or Outlook.com calendar added by its private address.")


def _run(script: str, payload: dict[str, Any], timeout: float) -> str:
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    out = win.powershell(_PREAMBLE.replace("{payload}", encoded) + script, timeout=timeout)
    if out.strip() == "NO_OUTLOOK":
        raise ActionError(NO_OUTLOOK)
    return out


@action
def outlook_events(args: dict[str, Any]) -> dict[str, Any]:
    """Events in Outlook's default calendar that overlap [from, to) (local ISO date-times)."""
    out = _run(_READ, {"from": _str(args, "from"), "to": _str(args, "to")}, timeout=60)
    try:
        rows = json.loads(out or "[]")
    except ValueError as e:
        raise ActionError(f"Outlook gave an unreadable answer: {out[:200]}") from e
    return {"events": rows if isinstance(rows, list) else [rows]}


@action
def outlook_add_event(args: dict[str, Any]) -> dict[str, Any]:
    """Add an appointment to Outlook's default calendar (it syncs to the account Outlook is signed in to)."""
    payload = {"title": _str(args, "title"), "start": _str(args, "start"), "end": _str(args, "end"), "all_day": bool(args.get("all_day")),
               "location": _str(args, "location", False) or "", "notes": _str(args, "notes", False) or "",
               "remind_minutes": int(args["remind_minutes"]) if args.get("remind_minutes") is not None else -1}
    out = _run(_ADD, payload, timeout=60)
    if not out.startswith("SAVED|"):
        raise ActionError(f"Outlook didn't confirm the event was saved: {out[:200]}")
    return {"saved": True, "start": out.split("|", 1)[1]}
