"""System extras: graphics card and temperatures, starting with Windows, copying, recent files, and noticing
downloads the user starts in a browser.

The Downloads watcher only looks at file names, sizes and times in the top level of the Downloads folder
(JARVIS's own downloads go to a subfolder and are reported by download_file itself). A file counts as
finished when it is no longer a partial download and its size has stopped changing. Programs and scripts
are flagged, never opened."""
from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import subprocess
import threading
import time
import winreg
from pathlib import Path
from typing import Any, Callable

import psutil

from . import win
from .actions import ActionError, _guard, _resolve, _str, action

log = logging.getLogger("jarvis.extras")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "JARVIS"
PARTIAL = {".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload"}
PROGRAMS = {".exe", ".msi", ".msix", ".appx", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".jse", ".wsf", ".scr", ".com", ".lnk", ".reg", ".jar"}


# ---------------- hardware ----------------

def _nvidia() -> list[dict[str, Any]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    out = subprocess.run([exe, "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, creationflags=win.CREATE_NO_WINDOW)
    gpus = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue

        def num(v: str) -> float | None:
            try:
                return float(v)
            except ValueError:
                return None

        gpus.append({"name": parts[0], "temperature_c": num(parts[1]), "busy_percent": num(parts[2]),
                     "memory_used_mb": num(parts[3]), "memory_total_mb": num(parts[4]),
                     "power_w": num(parts[5]) if len(parts) > 5 else None})
    return gpus


@action
def hardware_info(_: dict[str, Any]) -> dict[str, Any]:
    """Graphics cards (with live load, memory and temperature where the driver reports them) and temperatures."""
    gpus = _nvidia()
    try:
        names = win.powershell("(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + '|' + $_.DriverVersion }) -join \"`n\"", timeout=15)
        for line in names.splitlines():
            name, _, driver = line.partition("|")
            if name and not any(g["name"] in name or name in g["name"] for g in gpus):
                gpus.append({"name": name.strip(), "driver": driver.strip()})
    except win.WinError:
        pass
    temps: dict[str, Any] = {}
    try:  # ACPI thermal zones: many laptops report them, some only to administrators
        zones = win.powershell("(Get-CimInstance -Namespace root/wmi MSAcpi_ThermalZoneTemperature -ErrorAction Stop | "
                               "ForEach-Object { [math]::Round($_.CurrentTemperature / 10 - 273.15, 1) }) -join ','", timeout=15)
        vals = [float(v) for v in zones.split(",") if v.strip()]
        if vals:
            temps["cpu_area_c"] = max(vals)
    except (win.WinError, ValueError):
        temps["cpu_area_c"] = None
        temps["note"] = "Windows does not share the CPU temperature with normal apps on this PC."
    for g in gpus:
        if g.get("temperature_c") is not None:
            temps[f"gpu_c ({g['name']})"] = g["temperature_c"]
    battery = psutil.sensors_battery()
    return {"gpus": gpus, "temperatures": temps, "cpu_percent": psutil.cpu_percent(interval=0.5),
            "battery_percent": round(battery.percent) if battery else None}


# ---------------- start with Windows ----------------

def _app_exe() -> str:
    parent = int(os.environ.get("JARVIS_PARENT_PID", "0") or 0)
    try:
        return psutil.Process(parent).exe() if parent else ""
    except psutil.Error:
        return ""


@action
def autostart(args: dict[str, Any]) -> dict[str, Any]:
    """status, enable or disable starting JARVIS when the user signs in to Windows."""
    what = (_str(args, "action", False) or "status").lower()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        try:
            current = winreg.QueryValueEx(key, RUN_NAME)[0]
        except FileNotFoundError:
            current = ""
        if what == "status":
            return {"starts_with_windows": bool(current), "command": current}
        if what == "disable":
            if current:
                winreg.DeleteValue(key, RUN_NAME)
            return {"starts_with_windows": False}
        if what != "enable":
            raise ActionError("action must be status, enable or disable.")
        exe = _app_exe()
        if not exe or not exe.lower().endswith("jarvis.exe"):
            raise ActionError("I can't tell where the JARVIS app is installed, so I can't add it to start-up.")
        if "\\target\\debug\\" in exe.lower():
            raise ActionError("This is the development build, which needs its dev server running; start-up only works with the installed app.")
        winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, f'"{exe}"')
        return {"starts_with_windows": True, "command": f'"{exe}"'}


# ---------------- files ----------------

@action
def copy_path(args: dict[str, Any]) -> dict[str, Any]:
    """Copy a file or folder. Never overwrites."""
    src = _resolve(_str(args, "source"))
    dst = _resolve(_str(args, "destination"))
    _guard(dst)
    if not src.exists():
        raise ActionError(f"{src} does not exist.")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise ActionError(f"{dst} already exists; I won't overwrite it.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return {"copied_from": str(src), "copied_to": str(dst)}


def _shortcut_targets(links: list[Path]) -> dict[str, str]:
    """Resolve .lnk files to their targets in one PowerShell call."""
    if not links:
        return {}
    listing = ";".join(str(p) for p in links)
    script = ("$s = New-Object -ComObject WScript.Shell; foreach ($p in $env:JARVIS_LINKS.Split(';')) "
              "{ try { $p + '|' + $s.CreateShortcut($p).TargetPath } catch {} }")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
                       timeout=20, env={**os.environ, "JARVIS_LINKS": listing}, creationflags=win.CREATE_NO_WINDOW)
    out = {}
    for line in r.stdout.splitlines():
        link, _, target = line.partition("|")
        if target.strip():
            out[link.strip()] = target.strip()
    return out


@action
def recent_files(args: dict[str, Any]) -> dict[str, Any]:
    """Files the user opened recently (Windows' Recent items), newest first, optionally by type or name."""
    limit = max(1, min(int(args.get("limit") or 20), 60))
    ext = (_str(args, "extension", False) or "").lower().lstrip(".")
    query = (_str(args, "query", False) or "").lower()
    recent = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Recent"
    links = sorted((p for p in recent.glob("*.lnk")), key=lambda p: p.stat().st_mtime, reverse=True)[:200]
    targets = _shortcut_targets(links)
    rows = []
    for link in links:
        target = targets.get(str(link))
        if not target:
            continue
        t = Path(target)
        if not t.is_file() or (ext and t.suffix.lower().lstrip(".") != ext) or (query and query not in t.name.lower()):
            continue
        rows.append({"path": str(t), "name": t.name, "opened": dt.datetime.fromtimestamp(link.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
        if len(rows) >= limit:
            break
    return {"files": rows, "count": len(rows)}


def _downloads_dir() -> Path:
    return Path.home() / "Downloads"


def _describe(p: Path) -> dict[str, Any]:
    st = p.stat()
    return {"path": str(p), "name": p.name, "size_kb": round(st.st_size / 1024), "program": p.suffix.lower() in PROGRAMS,
            "finished": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")}


@action
def recent_downloads(args: dict[str, Any]) -> dict[str, Any]:
    """The newest finished files in Downloads; programs and scripts are flagged."""
    limit = max(1, min(int(args.get("limit") or 10), 50))
    files = [p for p in _downloads_dir().iterdir() if p.is_file() and p.suffix.lower() not in PARTIAL]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {"folder": str(_downloads_dir()), "files": [_describe(p) for p in files[:limit]]}


class EmergencyHotkey:
    """Ctrl+Alt+Shift+S, from any app: JARVIS stops talking at once and the backend stops every task and
    pending approval. A Windows hotkey, so it works whichever window is in front."""

    MODS = 0x0001 | 0x0002 | 0x0004 | 0x4000  # Alt, Ctrl, Shift, no auto-repeat
    KEY = ord("S")

    def __init__(self, report: Callable[[dict[str, Any]], None]) -> None:
        self.report = report

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="emergency-hotkey").start()

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, self.MODS, self.KEY):
            log.warning("Ctrl+Alt+Shift+S is taken by another app; the emergency stop shortcut is unavailable")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                log.warning("emergency stop pressed")
                try:
                    from .actions import run

                    run("stop_speaking", {})
                except Exception:  # noqa: BLE001 - the backend still stops everything else
                    log.exception("could not stop speaking")
                self.report({"event": "emergency_stop"})


class DownloadWatcher:
    """Notices files the user downloads in a browser and reports each once, when it is complete."""

    POLL_S = 3.0

    def __init__(self, report: Callable[[dict[str, Any]], None]) -> None:
        self.report = report
        self._seen: set[str] = set()
        self._sizes: dict[str, tuple[int, int]] = {}  # name -> (size, polls unchanged)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="downloads").start()

    def _scan(self) -> list[Path]:
        try:
            return [p for p in _downloads_dir().iterdir() if p.is_file()]
        except OSError:
            return []

    def _run(self) -> None:
        self._seen = {p.name for p in self._scan()}  # what was already there is not news
        while True:
            time.sleep(self.POLL_S)
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - never stop watching over one odd file
                log.exception("download watcher")

    def tick(self) -> None:
        for p in self._scan():
            if p.name in self._seen or p.suffix.lower() in PARTIAL or p.name.startswith("~$"):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            last, same = self._sizes.get(p.name, (-1, 0))
            same = same + 1 if size == last and size > 0 else 0
            self._sizes[p.name] = (size, same)
            if same >= 1:  # unchanged for a whole poll: finished
                self._seen.add(p.name)
                self._sizes.pop(p.name, None)
                self.report({"event": "download_done", **_describe(p)})
