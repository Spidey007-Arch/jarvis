"""Precise control of other apps: find and press controls by their real text, read a window's text, and
manage windows, the way a person would but without guessing from pixels.

Controls are found with Windows UI Automation (what screen readers use), and pressed through the control
itself where it allows that (a button's Invoke), so nothing depends on where the mouse lands. When an app
exposes no automation (games, some web canvases), the window is read with Windows' own built-in OCR
instead. Both are part of Windows: nothing extra is installed, and nothing is sent anywhere.

Both run in a short PowerShell script; arguments travel as JSON in an environment variable, never pasted
into the script text. Coordinates are physical pixels everywhere (the script makes itself DPI aware)."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from . import win
from .actions import ActionError, _out_of_the_way, _str, action

user32 = ctypes.windll.user32

_PS_PRELUDE = r"""
$ErrorActionPreference = 'Stop'
Add-Type -Namespace J -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(System.IntPtr v);'
[void][J.Dpi]::SetProcessDpiAwarenessContext([IntPtr](-4))
$a = $env:JARVIS_ARG | ConvertFrom-Json
"""

_UIA_SCRIPT = _PS_PRELUDE + r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$AE = [Windows.Automation.AutomationElement]
$win = $AE::FromHandle([IntPtr][long]$a.hwnd)
$q = ([string]$a.text).Trim().ToLower()
$found = @()
$all = $win.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
foreach ($e in $all) {
  $c = $e.Current
  if (-not $c.Name -or $c.IsOffscreen) { continue }
  $n = $c.Name.Trim().ToLower()
  if ($q -and -not $n.Contains($q)) { continue }
  $r = $c.BoundingRectangle
  if ($r.IsEmpty -or $r.Width -le 1 -or $r.Height -le 1) { continue }
  $found += [pscustomobject]@{ el = $e; name = $c.Name; type = $c.ControlType.ProgrammaticName.Replace('ControlType.', '');
    x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height; enabled = $c.IsEnabled; exact = ($n -eq $q) }
  if ($found.Count -ge 80) { break }
}
$items = @($found | Sort-Object @{ e = { -not $_.exact } }, @{ e = { -not $_.enabled } })
$out = @{ window = $win.Current.Name; count = $items.Count }
if ($a.mode -eq 'type') {
  $fields = @($items | Where-Object { $_.type -in @('Edit', 'ComboBox', 'Document') })
  if ($fields.Count -eq 0) { $fields = $items }
  if ($fields.Count -eq 0) { $out.done = 'none' }
  else {
    $pick = $fields[[Math]::Min([int]$a.index, $fields.Count - 1)]
    $out.picked = @{ name = $pick.name; type = $pick.type; x = $pick.x; y = $pick.y; w = $pick.w; h = $pick.h }
    $p = $null
    if ($pick.el.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$p) -and -not $p.Current.IsReadOnly) {
      $p.SetValue([string]$a.value); $out.done = 'set'
    } else {
      try { $pick.el.SetFocus(); $out.done = 'focused' } catch { $out.done = 'point' }
    }
  }
} elseif ($a.mode -eq 'click') {
  if ($items.Count -eq 0) { $out.done = 'none' }
  else {
    $pick = $items[[Math]::Min([int]$a.index, $items.Count - 1)]
    $out.picked = @{ name = $pick.name; type = $pick.type; x = $pick.x; y = $pick.y; w = $pick.w; h = $pick.h }
    $out.done = 'point'
    if (-not $a.pointer) {
      $el = $pick.el
      $p = $null
      foreach ($pat in @([Windows.Automation.InvokePattern]::Pattern, [Windows.Automation.SelectionItemPattern]::Pattern,
                         [Windows.Automation.TogglePattern]::Pattern, [Windows.Automation.ExpandCollapsePattern]::Pattern)) {
        if ($el.TryGetCurrentPattern($pat, [ref]$p)) {
          if ($p -is [Windows.Automation.InvokePattern]) { $p.Invoke() }
          elseif ($p -is [Windows.Automation.SelectionItemPattern]) { $p.Select() }
          elseif ($p -is [Windows.Automation.TogglePattern]) { $p.Toggle() }
          else { if ($p.Current.ExpandCollapseState -eq 'Collapsed') { $p.Expand() } else { $p.Collapse() } }
          $out.done = 'pressed'; $out.how = $p.GetType().Name.Replace('Pattern', '')
          break
        }
      }
    }
  }
} else {
  $out.items = @($items | Select-Object -First ([int]$a.limit) | ForEach-Object {
    @{ name = $_.name; type = $_.type; x = $_.x; y = $_.y; w = $_.w; h = $_.h; enabled = $_.enabled } })
}
$out | ConvertTo-Json -Depth 5 -Compress
"""

_OCR_SCRIPT = _PS_PRELUDE + r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); [void]$task.Wait(-1); $task.Result }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($a.path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $eng) { throw 'Windows has no OCR language installed.' }
$res = Await ($eng.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
$lines = @(foreach ($l in $res.Lines) {
  $xs = $l.Words | ForEach-Object { $_.BoundingRect }
  $x1 = ($xs | Measure-Object X -Minimum).Minimum; $y1 = ($xs | Measure-Object Y -Minimum).Minimum
  $x2 = ($xs | ForEach-Object { $_.X + $_.Width } | Measure-Object -Maximum).Maximum
  $y2 = ($xs | ForEach-Object { $_.Y + $_.Height } | Measure-Object -Maximum).Maximum
  @{ text = $l.Text; x = [int]$x1; y = [int]$y1; w = [int]($x2 - $x1); h = [int]($y2 - $y1) }
})
$stream.Dispose()
@{ lines = $lines } | ConvertTo-Json -Depth 4 -Compress
"""

_EXPLORER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$a = $env:JARVIS_ARG | ConvertFrom-Json
$shell = New-Object -ComObject Shell.Application
function Find-Win { foreach ($w in $shell.Windows()) { try { if ($w.Document.Folder.Self.Path -eq $a.folder) { return $w } } catch {} } }
$w = Find-Win
if (-not $w) {
  Start-Process explorer.exe -ArgumentList ('"' + $a.folder + '"')
  for ($i = 0; $i -lt 40 -and -not $w; $i++) { Start-Sleep -Milliseconds 150; $w = Find-Win }
}
if (-not $w) { throw 'File Explorer did not open the folder.' }
$doc = $w.Document
$picked = @()
$first = $true
foreach ($it in $doc.Folder.Items()) {
  $name = $it.Name
  $path = $it.Path
  $ext = [IO.Path]::GetExtension($path).TrimStart('.').ToLower()
  if ($a.extension -and $ext -ne $a.extension) { continue }
  if ($a.contains -and -not $name.ToLower().Contains($a.contains)) { continue }
  if ($it.IsFolder -and -not $a.extension -and -not $a.include_folders) { continue }
  $flags = if ($first) { 1 + 4 + 8 + 16 } else { 1 }  # select; first one also deselects the rest, shows and focuses it
  $doc.SelectItem($it, $flags)
  $first = $false
  $picked += $name
  if ($picked.Count -ge 2000) { break }
}
try { (New-Object -ComObject WScript.Shell).AppActivate($w.LocationName) | Out-Null } catch {}
@{ folder = $a.folder; selected = $picked.Count; names = @($picked | Select-Object -First 25) } | ConvertTo-Json -Compress
"""


def _ps(script: str, arg: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    env = {**os.environ, "JARVIS_ARG": json.dumps(arg)}
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env,
                           creationflags=win.CREATE_NO_WINDOW | win.ABOVE_NORMAL_PRIORITY_CLASS)
    except subprocess.TimeoutExpired as e:
        raise ActionError(f"The app did not answer within {timeout:.0f} s.") from e
    if r.returncode != 0:
        lines = [l for l in (r.stderr or r.stdout).strip().splitlines() if l.strip()]
        raise ActionError(lines[0] if lines else f"PowerShell exited with code {r.returncode}")
    line = next((l for l in reversed(r.stdout.strip().splitlines()) if l.lstrip().startswith("{")), "")
    try:
        return json.loads(line, strict=False)  # PowerShell 5 leaves some control characters unescaped in strings
    except ValueError as e:
        detail = (r.stderr or r.stdout).strip()[-200:]
        raise ActionError("Windows returned nothing readable" + (f": {detail}" if detail else ".")) from e


# ---------------- windows ----------------

def _windows() -> list[dict[str, Any]]:
    """Visible top-level app windows, front to back."""
    jarvis = int(os.environ.get("JARVIS_PARENT_PID", "0") or 0)
    fg = user32.GetForegroundWindow()
    out: list[dict[str, Any]] = []

    def cb(hwnd: int, _: int) -> bool:
        rect = wintypes.RECT()
        if not user32.IsWindowVisible(hwnd) or user32.GetWindowLongW(hwnd, win.GWL_EXSTYLE) & win.WS_EX_TOOLWINDOW:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        minimized = bool(user32.IsIconic(hwnd))
        if not n or not (minimized or user32.GetWindowRect(hwnd, ctypes.byref(rect)) and rect.right - rect.left > 100):
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if buf.value in ("Program Manager", "Windows Input Experience"):
            return True
        try:
            import psutil

            app = psutil.Process(pid.value).name().removesuffix(".exe")
        except Exception:  # noqa: BLE001 - a process that just exited
            app = ""
        out.append({"hwnd": hwnd, "title": buf.value, "app": app, "minimized": minimized, "active": hwnd == fg,
                    "jarvis": pid.value == jarvis})
        return True

    user32.EnumWindows(win._WNDENUMPROC(cb), 0)
    return out


def _target(title: str = "") -> dict[str, Any]:
    """The window meant: one whose title or app name contains `title`, else the frontmost one that is not JARVIS."""
    wins = [w for w in _windows() if not w["jarvis"]]
    if title:
        t = title.lower()
        hit = next((w for w in wins if t in w["title"].lower()), None) or next((w for w in wins if t in w["app"].lower()), None)
        if hit is None:
            raise ActionError(f"No open window matches '{title}'. Open: " + "; ".join(w["title"][:40] for w in wins[:12]))
        return hit
    hit = next((w for w in wins if not w["minimized"]), None)
    if hit is None:
        raise ActionError("No app window is open.")
    return hit


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    with win.physical_pixels():
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _bring_up(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.keybd_event(0x12, 0, 0, 0)  # a tap of Alt lets this process move another window to the front
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)
    time.sleep(0.25)


def snapshot() -> Any:
    """A tiny grey picture of the whole screen, to tell whether an action changed anything."""
    from PIL import ImageGrab

    with win.physical_pixels():
        return ImageGrab.grab(all_screens=True).convert("L").resize((192, 108))


def changed(before: Any, after: Any) -> bool:
    """Whether the screen visibly changed between two snapshots (a menu opened, a page loaded, text appeared)."""
    from PIL import ImageChops, ImageStat

    diff = ImageChops.difference(before, after)
    return ImageStat.Stat(diff).mean[0] > 0.6 or diff.point(lambda v: 255 if v > 40 else 0).getbbox() is not None


def _click_point(x: int, y: int, button: str = "left", double: bool = False) -> None:
    left, top, w, h = win.virtual_screen()
    fx, fy = (x - left) / w, (y - top) / h
    with _out_of_the_way(fx, fy):
        win.mouse_click(fx, fy, button, double)
        time.sleep(0.25)


@action
def list_windows(_: dict[str, Any]) -> dict[str, Any]:
    wins = [{k: v for k, v in w.items() if k not in ("hwnd", "jarvis")} for w in _windows() if not w["jarvis"]]
    return {"windows": wins[:40], "count": len(wins)}


@action
def window_control(args: dict[str, Any]) -> dict[str, Any]:
    """minimize, maximize, restore, focus or close a window found by title or app name."""
    what = _str(args, "action").lower()
    w = _target(_str(args, "window", False) or "")
    hwnd = w["hwnd"]
    if what in ("focus", "switch", "show", "activate"):
        _bring_up(hwnd)
    elif what in ("minimize", "minimise"):
        user32.ShowWindow(hwnd, 6)
    elif what in ("maximize", "maximise"):
        user32.ShowWindow(hwnd, 3)
        _bring_up(hwnd)
    elif what == "restore":
        user32.ShowWindow(hwnd, 9)
    elif what == "close":
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: the app may still ask to save
        time.sleep(0.6)
        still = bool(user32.IsWindow(hwnd)) and bool(user32.IsWindowVisible(hwnd))
        return {"window": w["title"], "app": w["app"], "closed": not still,
                **({"note": "It is still open; it may be asking whether to save."} if still else {})}
    else:
        raise ActionError("action must be focus, minimize, maximize, restore or close.")
    return {"window": w["title"], "app": w["app"], "done": what}


# ---------------- controls by text ----------------

def _ocr(hwnd: int) -> tuple[list[dict[str, Any]], tuple[int, int, int, int]]:
    """OCR one window. The capture is deleted as soon as it is read."""
    from PIL import ImageGrab

    _bring_up(hwnd)
    box = _rect(hwnd)
    with win.physical_pixels():
        img = ImageGrab.grab(bbox=box, all_screens=True)
    fd, path = tempfile.mkstemp(prefix="jarvis-ocr-", suffix=".png")
    os.close(fd)
    try:
        img.save(path)
        lines = _ps(_OCR_SCRIPT, {"path": path}, timeout=40).get("lines") or []
    finally:
        Path(path).unlink(missing_ok=True)
    return lines, box


@action
def find_controls(args: dict[str, Any]) -> dict[str, Any]:
    """Controls in a window whose visible name contains text (all named controls if text is empty)."""
    w = _target(_str(args, "window", False) or "")
    out = _ps(_UIA_SCRIPT, {"hwnd": w["hwnd"], "text": _str(args, "text", False) or "", "mode": "find",
                            "limit": max(1, min(int(args.get("limit") or 30), 80))}, timeout=40)
    return {"window": out.get("window") or w["title"], "app": w["app"], "matches": out.get("count", 0), "controls": out.get("items") or []}


@action
def click_text(args: dict[str, Any]) -> dict[str, Any]:
    """Press the control showing `text` in a window: through UI Automation first, else by OCR and a click."""
    text = _str(args, "text").strip()
    button = (_str(args, "button", False) or "left").lower()
    double = bool(args.get("double"))
    index = max(0, int(args.get("index") or 0))
    w = _target(_str(args, "window", False) or "")
    _bring_up(w["hwnd"])
    before = snapshot()
    out = _click_text(w, text, index, button, double)
    time.sleep(0.4)
    out["screen_changed"] = changed(before, snapshot())
    if not out["screen_changed"]:
        out["note"] = "Nothing visibly changed. Check with read_window or look_at_screen before assuming it worked."
    return out


def _click_text(w: dict[str, Any], text: str, index: int, button: str, double: bool) -> dict[str, Any]:
    pointer = button != "left" or double  # right clicks and double clicks need the real mouse
    try:
        out = _ps(_UIA_SCRIPT, {"hwnd": w["hwnd"], "text": text, "mode": "click", "index": index, "pointer": pointer}, timeout=40)
    except ActionError:
        out = {"done": "none"}
    if out.get("done") == "pressed":
        return {"window": w["title"], "pressed": out["picked"]["name"], "control": out["picked"]["type"], "via": "ui automation",
                "action": out.get("how"), "other_matches": max(0, out.get("count", 1) - 1)}
    if out.get("done") == "point":
        p = out["picked"]
        _click_point(p["x"] + p["w"] // 2, p["y"] + p["h"] // 2, button, double)
        return {"window": w["title"], "clicked": p["name"], "control": p["type"], "via": "ui automation",
                "other_matches": max(0, out.get("count", 1) - 1)}
    lines, (left, top, _r, _b) = _ocr(w["hwnd"])
    t = text.lower()
    hits = [l for l in lines if l["text"].strip().lower() == t] or [l for l in lines if t in l["text"].lower()]
    if not hits:
        seen = "; ".join(l["text"] for l in lines[:30])
        raise ActionError(f"'{text}' is not visible in {w['title'] or w['app']}." + (f" Visible text: {seen}" if seen else ""))
    hit = hits[min(index, len(hits) - 1)]
    _click_point(left + hit["x"] + hit["w"] // 2, top + hit["y"] + hit["h"] // 2, button, double)
    return {"window": w["title"], "clicked": hit["text"], "via": "ocr", "other_matches": len(hits) - 1}


@action
def type_into(args: dict[str, Any]) -> dict[str, Any]:
    """Type into a field found by its label (e.g. 'Email', 'Search', 'Message'): set through UI Automation where the
    field allows it, else focused (or clicked) and typed into. submit=true presses Enter after."""
    from .actions import type_text

    label = _str(args, "label").strip()
    value = str(args.get("text") or "")
    w = _target(_str(args, "window", False) or "")
    _bring_up(w["hwnd"])
    out = _ps(_UIA_SCRIPT, {"hwnd": w["hwnd"], "text": label, "mode": "type", "value": value,
                            "index": max(0, int(args.get("index") or 0))}, timeout=40)
    done = out.get("done")
    if done == "none":
        raise ActionError(f"No field labelled '{label}' in {w['title'] or w['app']}. Try find_controls to see the field names.")
    if done in ("focused", "point"):
        if done == "point":
            p = out["picked"]
            _click_point(p["x"] + p["w"] // 2, p["y"] + p["h"] // 2)
        win.press("ctrl", "a")
        type_text({"text": value})
    if args.get("submit"):
        time.sleep(0.15)
        win.press("enter")
    return {"window": w["title"], "field": out["picked"]["name"], "control": out["picked"]["type"],
            "how": "set directly" if done == "set" else "typed", "characters": len(value), "submitted": bool(args.get("submit"))}


@action
def read_window(args: dict[str, Any]) -> dict[str, Any]:
    """The visible text of a window, read with Windows OCR, top to bottom."""
    w = _target(_str(args, "window", False) or "")
    lines, _box = _ocr(w["hwnd"])
    text = "\n".join(l["text"] for l in sorted(lines, key=lambda l: (l["y"] // 8, l["x"])))
    return {"window": w["title"], "app": w["app"], "text": text[:8000], "lines": len(lines)}


# ---------------- music ----------------

def _song_rows(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Track rows on Spotify's search page: a title line with a 'Song · artist' line just under it."""
    rows = []
    for top in lines:
        under = next((l for l in lines if 0 < l["y"] - top["y"] < 30 and abs(l["x"] - top["x"]) < 12
                      and l["text"].strip().lower().startswith("song")), None)
        if under:
            artist = under["text"].strip()[4:]
            while artist and not artist[0].isalnum():  # the '·' after "Song" comes out of OCR as a stray symbol
                artist = artist[1:]
            rows.append({**top, "artist": artist.strip()})
    return sorted(rows, key=lambda r: r["y"])


def _spotify() -> dict[str, Any] | None:
    return next((w for w in _windows() if w["app"].lower() == "spotify"), None)


@action
def play_music(args: dict[str, Any]) -> dict[str, Any]:
    """Play a song on Spotify: open Spotify's own search for it, double-click the best matching track, and confirm
    it is playing from the window title (Spotify shows 'Artist - Song' while it plays)."""
    from urllib.parse import quote

    query = _str(args, "query").strip()
    try:
        os.startfile("spotify:search:" + quote(query))
    except OSError as e:
        raise ActionError("Spotify isn't installed (or can't open search links). Play it on YouTube with open_url instead.") from e
    w = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and (w := _spotify()) is None:
        time.sleep(0.5)
    if w is None:
        raise ActionError("Spotify did not open within 20 s.")
    words = {x for x in query.lower().split() if x not in ("by", "the", "song", "a")}
    rows: list[dict[str, Any]] = []
    left = top = 0
    for _ in range(4):  # results take a moment to load, longer when Spotify has just started
        time.sleep(1.5)
        lines, (left, top, _r, _b) = _ocr(w["hwnd"])
        if rows := _song_rows(lines):
            break
    if not rows:
        raise ActionError(f"Spotify's search for '{query}' showed no songs I could read.")

    def score(r: dict[str, Any]) -> int:
        have = set((r["text"] + " " + r["artist"]).lower().replace("-", " ").split())
        return len(words & have)

    pick = max(rows, key=score)  # max keeps the first (top) row among equals
    before = w["title"]
    _click_point(left + pick["x"] + min(pick["w"], 80) // 2, top + pick["y"] + pick["h"] // 2, "left", True)
    title = before
    for _ in range(16):
        time.sleep(0.4)
        now = next((x for x in _windows() if x["hwnd"] == w["hwnd"]), None)
        title = now["title"] if now else ""
        if title != before and " - " in title:
            return {"playing": title, "searched": query, "picked": pick["text"], "artist": pick["artist"]}
    if " - " in before and pick["text"].lower() in before.lower():
        return {"playing": before, "searched": query, "note": "That song was already playing."}
    raise ActionError(f"I double-clicked '{pick['text']}' in Spotify but it didn't start (window title: '{title}'). "
                      "Check with read_window.")


# ---------------- mouse, waiting, Explorer ----------------

@action
def mouse_drag(args: dict[str, Any]) -> dict[str, Any]:
    """Press at one point and release at another (fractions of the whole screen), moving in steps like a hand."""
    from .actions import _fraction

    x1, y1, x2, y2 = (_fraction(args, k) for k in ("x1", "y1", "x2", "y2"))
    with _out_of_the_way(x1, y1):
        win.mouse_move(x1, y1)
        time.sleep(0.1)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
        for i in range(1, 21):
            win.mouse_move(x1 + (x2 - x1) * i / 20, y1 + (y2 - y1) * i / 20)
            time.sleep(0.02)
        time.sleep(0.1)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
        time.sleep(0.3)
    left, top, w, h = win.virtual_screen()
    return {"dragged_from": {"x": left + int(x1 * w), "y": top + int(y1 * h)}, "to": {"x": left + int(x2 * w), "y": top + int(y2 * h)}}


@action
def wait_for_window(args: dict[str, Any]) -> dict[str, Any]:
    """Wait (up to 15 s) until a window whose title contains `title` is in front, or until the front window
    changes when no title is given. Returns what is in front now."""
    want = (_str(args, "title", False) or "").lower()
    limit = max(0.5, min(float(args.get("seconds") or 5), 15.0))
    first = next((w for w in _windows() if w["active"]), None)
    start = time.monotonic()
    while time.monotonic() - start < limit:
        now = next((w for w in _windows() if w["active"]), None)
        if now and (want and want in now["title"].lower() or not want and (first is None or now["hwnd"] != first["hwnd"] or now["title"] != first["title"])):
            return {"changed": True, "active": now["title"], "app": now["app"], "waited_s": round(time.monotonic() - start, 1)}
        time.sleep(0.25)
    now = next((w for w in _windows() if w["active"]), None)
    return {"changed": False, "active": now["title"] if now else "", "app": now["app"] if now else "", "waited_s": round(limit, 1)}


@action
def select_files(args: dict[str, Any]) -> dict[str, Any]:
    """Open a folder in File Explorer (or reuse its window) and select the matching files there."""
    from .actions import _resolve

    folder = _resolve(_str(args, "folder"))
    if not folder.is_dir():
        raise ActionError(f"{folder} is not a folder.")
    ext = (_str(args, "extension", False) or "").lower().lstrip(".").lstrip("*.")
    out = _ps(_EXPLORER_SCRIPT, {"folder": str(folder), "extension": ext, "contains": (_str(args, "name_contains", False) or "").lower(),
                                 "include_folders": bool(args.get("include_folders"))}, timeout=60)
    if not out.get("selected"):
        raise ActionError(f"Nothing in {folder} matches" + (f" .{ext}" if ext else "") + "; the folder is open in Explorer.")
    return out
