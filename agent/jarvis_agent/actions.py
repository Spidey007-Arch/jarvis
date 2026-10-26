"""Actions the backend can ask the agent to run on this PC.

Each action returns a JSON-serialisable result describing exactly what happened,
or raises ActionError with the real reason it failed. Nothing here reports
success without having done the thing.
"""
from __future__ import annotations

import datetime as dt
import difflib
import getpass
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import winreg
from pathlib import Path
from typing import Any, Callable

import psutil

from . import __version__, live_audio, mailchat, voice, webdrive, win


class ActionError(Exception):
    """Raised with a human-readable, truthful failure reason."""


REGISTRY: dict[str, Callable[[dict[str, Any]], Any]] = {}


def action(fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    REGISTRY[fn.__name__] = fn
    return fn


def run(name: str, args: dict[str, Any]) -> Any:
    fn = REGISTRY.get(name)
    if fn is None:
        raise ActionError(f"Unknown action '{name}'.")
    try:
        return fn(args)
    except (win.WinError, webdrive.WebError) as e:
        raise ActionError(str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise ActionError(f"Windows did not respond in time ({e.timeout:.0f}s).") from e
    except PermissionError as e:
        raise ActionError(f"Access denied: {e.filename or e}") from e
    except FileNotFoundError as e:
        raise ActionError(f"Not found: {e.filename or e}") from e


def _str(args: dict[str, Any], key: str, required: bool = True) -> str:
    v = args.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ActionError(f"Missing '{key}'.")
        return ""
    return str(v).strip()


@action
def ping(_: dict[str, Any]) -> dict[str, Any]:
    return {"pong": True, "agent_version": __version__, "pid": os.getpid(), "user": getpass.getuser(),
            "host": platform.node(), "os": platform.platform(), "time": time.time()}


# ======================= system =======================

@action
def system_status(_: dict[str, Any]) -> dict[str, Any]:
    net0 = psutil.net_io_counters()
    for p in psutil.process_iter():
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    cpu = psutil.cpu_percent(interval=1.0)
    net1 = psutil.net_io_counters()
    vm = psutil.virtual_memory()
    procs = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            procs.append((p.cpu_percent(None) / psutil.cpu_count(), p.info["memory_info"].rss, p.info["name"]))
        except (psutil.Error, AttributeError):
            pass
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "fixed" not in part.opts and part.fstype == "":
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({"drive": part.mountpoint, "used_percent": u.percent, "free_gb": round(u.free / 1024**3, 1)})
        except OSError:
            pass
    bat = psutil.sensors_battery()
    battery = None if bat is None else {
        "percent": round(bat.percent), "charging": bat.power_plugged,
        "minutes_left": None if bat.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) else round(bat.secsleft / 60),
    }
    flags = []
    if cpu > 85:
        flags.append(f"CPU is high at {cpu:.0f}%.")
    if vm.percent > 85:
        flags.append(f"Memory is high at {vm.percent:.0f}%.")
    flags += [f"Drive {d['drive']} is {d['used_percent']:.0f}% full." for d in disks if d["used_percent"] > 90]
    if battery and battery["percent"] < 20 and not battery["charging"]:
        flags.append(f"Battery is low at {battery['percent']}% and not charging.")
    return {
        "cpu_percent": cpu,
        "cpu_cores": psutil.cpu_count(),
        "process_count": len(procs),
        "open_apps": _open_apps(),
        "memory": {"used_percent": vm.percent, "used_gb": round(vm.used / 1024**3, 1), "total_gb": round(vm.total / 1024**3, 1)},
        "disks": disks,
        "battery": battery,
        "network_kb_per_s": {"down": round((net1.bytes_recv - net0.bytes_recv) / 1024, 1), "up": round((net1.bytes_sent - net0.bytes_sent) / 1024, 1)},
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "top_cpu": [{"name": n, "cpu_percent": round(c, 1)} for c, _, n in sorted(procs, reverse=True)[:5]],
        "top_memory": [{"name": n, "memory_mb": round(m / 1024**2)} for _, m, n in sorted(procs, key=lambda x: x[1], reverse=True)[:5]],
        "flags": flags,
    }


_SHELL_WINDOWS = {"program manager", "windows input experience", "settings", "microsoft text input application", "nvidia geforce overlay"}


def _open_apps() -> list[dict[str, Any]]:
    """The apps with a visible window, by name, with their window count and memory use."""
    apps: dict[str, dict[str, Any]] = {}
    for pid, title in win.titled_windows():
        if title.strip().lower() in _SHELL_WINDOWS:
            continue
        try:
            proc = psutil.Process(pid)
            name = proc.name().removesuffix(".exe")
            mem = proc.memory_info().rss
        except psutil.Error:
            continue
        if name.lower() in {"jarvis", "textinputhost", "applicationframehost", "systemsettings", "shellexperiencehost"}:
            continue
        app = apps.setdefault(name, {"name": name, "windows": 0, "memory_mb": round(mem / 1024**2)})
        app["windows"] += 1
    return sorted(apps.values(), key=lambda a: a["memory_mb"], reverse=True)[:12]


# ======================= apps =======================

_APPS: tuple[float, list[dict[str, str]]] = (0.0, [])
_ALIASES = {
    "chrome": "google chrome", "vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
    "edge": "microsoft edge", "explorer": "file explorer", "files": "file explorer", "word": "word", "excel": "excel",
    "powerpoint": "powerpoint", "cmd": "command prompt", "terminal": "terminal", "spotify": "spotify",
    "calc": "calculator", "settings": "settings", "store": "microsoft store", "teams": "microsoft teams",
}


def _apps() -> list[dict[str, str]]:
    global _APPS
    if time.time() - _APPS[0] < 600 and _APPS[1]:
        return _APPS[1]
    raw = win.powershell("Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress", timeout=30)
    data = json.loads(raw or "[]")
    apps = [{"name": a["Name"], "id": a["AppID"]} for a in (data if isinstance(data, list) else [data]) if a.get("Name")]
    _APPS = (time.time(), apps)
    return apps


def _match_apps(query: str) -> list[tuple[float, dict[str, str]]]:
    q = _ALIASES.get(query.lower().strip(), query.lower().strip())
    scored = []
    for a in _apps():
        n = a["name"].lower()
        if n == q:
            s = 1.0
        elif n.startswith(q):
            s = 0.92
        elif re.search(rf"\b{re.escape(q)}\b", n):
            s = 0.85
        elif q in n:
            s = 0.75
        else:
            s = difflib.SequenceMatcher(None, q, n).ratio() * 0.8
        # uninstallers and helpers are rarely what anyone means
        if re.search(r"uninstall|readme|help|documentation|release notes", n):
            s -= 0.3
        scored.append((s, a))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


@action
def find_apps(args: dict[str, Any]) -> dict[str, Any]:
    q = _str(args, "query")
    return {"matches": [{"name": a["name"], "score": round(s, 2)} for s, a in _match_apps(q)[:8] if s > 0.4]}


@action
def open_app(args: dict[str, Any]) -> dict[str, Any]:
    name = _str(args, "name")
    ranked = _match_apps(name)
    if ranked and ranked[0][0] >= 0.6:
        app = ranked[0][1]
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['id']}"], creationflags=win.CREATE_NO_WINDOW)
        others = [a["name"] for s, a in ranked[1:4] if s >= 0.6]
        return {"opened": app["name"], "requested": name, "other_close_matches": others}
    exe = shutil.which(name) or shutil.which(name + ".exe")
    if exe:
        subprocess.Popen([exe], creationflags=subprocess.DETACHED_PROCESS)
        return {"opened": exe, "requested": name}
    close = [a["name"] for s, a in ranked[:5] if s > 0.35]
    raise ActionError(f"No installed app matches '{name}'." + (f" Closest: {', '.join(close)}." if close else ""))


@action
def close_app(args: dict[str, Any]) -> dict[str, Any]:
    """Politely terminate every process whose executable name matches."""
    name = _str(args, "name").lower().removesuffix(".exe")
    protected = {"explorer", "winlogon", "csrss", "lsass", "services", "svchost", "system", "dwm", "jarvis", "python", "pythonw"}
    if name in protected:
        raise ActionError(f"Closing '{name}' could destabilise Windows or JARVIS itself, so I won't.")
    targets = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().removesuffix(".exe") == name]
    if not targets:
        raise ActionError(f"No running process is named '{name}'. Use the executable name, e.g. 'chrome' or 'notepad'.")
    for p in targets:
        try:
            p.terminate()
        except psutil.Error:
            pass
    gone, alive = psutil.wait_procs(targets, timeout=5)
    return {"closed_processes": len(gone), "still_running": len(alive), "name": name}


# ======================= web =======================

_SEARCH = {
    "google": "https://www.google.com/search?q={}",
    "bing": "https://www.bing.com/search?q={}",
    "duckduckgo": "https://duckduckgo.com/?q={}",
    "youtube": "https://www.youtube.com/results?search_query={}",
    "github": "https://github.com/search?q={}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={}",
    "amazon": "https://www.amazon.com/s?k={}",
    "maps": "https://www.google.com/maps/search/{}",
}
_BROWSERS = {"chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe", "brave": "brave.exe", "opera": "opera.exe"}


def _browser_exe(browser: str) -> str:
    exe = _BROWSERS.get(browser.lower())
    if not exe:
        raise ActionError(f"Unknown browser '{browser}'. Known: {', '.join(_BROWSERS)}.")
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                path = winreg.QueryValue(k, None)
                if path and Path(path.strip('"')).exists():
                    return path.strip('"')
        except OSError:
            continue
    raise ActionError(f"{browser.capitalize()} does not appear to be installed.")


def _open_url(url: str, browser: str = "") -> str:
    if browser:
        subprocess.Popen([_browser_exe(browser), url], creationflags=subprocess.DETACHED_PROCESS)
        return browser
    os.startfile(url)  # default browser
    return "default browser"


@action
def open_url(args: dict[str, Any]) -> dict[str, Any]:
    url = _str(args, "url")
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[\w-]+(\.[\w-]+)+(/.*)?$", url):
            url = "https://" + url
        else:
            raise ActionError(f"'{url}' is not a web address.")
    used = _open_url(url, _str(args, "browser", False))
    return {"opened": url, "in": used}


@action
def web_search(args: dict[str, Any]) -> dict[str, Any]:
    query = _str(args, "query")
    engine = (_str(args, "engine", False) or "google").lower()
    if engine not in _SEARCH:
        raise ActionError(f"Unknown search engine '{engine}'. Known: {', '.join(_SEARCH)}.")
    url = _SEARCH[engine].format(urllib.parse.quote_plus(query))
    used = _open_url(url, _str(args, "browser", False))
    return {"searched": query, "engine": engine, "url": url, "in": used}


# ======================= shopping browser =======================
# Paying stays with the user: webdrive refuses pay/place-order controls and secret fields in code.

def _num(args: dict[str, Any], key: str) -> int:
    try:
        return int(args.get(key))
    except (TypeError, ValueError) as e:
        raise ActionError(f"'{key}' must be a control number from web_read.") from e


@action
def web_open(args: dict[str, Any]) -> dict[str, Any]:
    url = _str(args, "url")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return webdrive.open_page(url)


@action
def web_read(args: dict[str, Any]) -> dict[str, Any]:
    return webdrive.read_page(_str(args, "find", False), int(args.get("offset") or 0))


@action
def web_click(args: dict[str, Any]) -> dict[str, Any]:
    return webdrive.click(_num(args, "n"))


@action
def web_type(args: dict[str, Any]) -> dict[str, Any]:
    return webdrive.type_into(_num(args, "n"), _str(args, "text"), bool(args.get("submit")))


# ======================= Gmail and WhatsApp (read-only) =======================

@action
def gmail_list(args: dict[str, Any]) -> dict[str, Any]:
    return mailchat.gmail_list(_str(args, "query", False), int(args.get("limit") or 15))


@action
def gmail_open(args: dict[str, Any]) -> dict[str, Any]:
    return mailchat.gmail_open(_num(args, "n"))


@action
def whatsapp_chats(args: dict[str, Any]) -> dict[str, Any]:
    return mailchat.whatsapp_chats(bool(args.get("unread_only")), int(args.get("limit") or 15))


@action
def whatsapp_read(args: dict[str, Any]) -> dict[str, Any]:
    return mailchat.whatsapp_read(_str(args, "chat"), int(args.get("limit") or 20))


# ======================= downloads =======================

# Never fetched: things that run code when opened. Everything downloaded is marked as coming from the
# internet, so Windows SmartScreen and Office's Protected View still apply to what is allowed.
_RUNNABLE = {"exe", "msi", "msix", "appx", "bat", "cmd", "com", "scr", "pif", "ps1", "psm1", "vbs", "vbe", "js", "jse", "wsf",
             "wsh", "hta", "jar", "lnk", "reg", "cpl", "msc", "dll", "sys", "iso", "img", "vhd", "vhdx", "url", "scf", "inf", "application"}
MAX_DOWNLOAD = 1024 * 1024 * 1024  # 1 GB


def _download_name(url: str, disposition: str) -> str:
    m = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?", disposition or "", re.I)
    name = urllib.parse.unquote((m.group(1) or m.group(2)) if m else Path(urllib.parse.urlparse(url).path).name)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .") or "download"
    return name[:150]


@action
def download_file(args: dict[str, Any]) -> dict[str, Any]:
    """Download a document, image, video or audio file into Downloads\\JARVIS and (by default) open it."""
    url = _str(args, "url")
    if not re.match(r"^https?://", url, re.I):
        raise ActionError(f"'{url}' is not a web address.")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JARVIS"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except (urllib.error.URLError, OSError) as e:
        raise ActionError(f"Could not download {url}: {e}") from e
    with resp:
        name = _download_name(resp.geturl(), resp.headers.get("Content-Disposition", ""))
        ctype = (resp.headers.get_content_type() or "").lower()
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if not ext:
            guess = mimetypes.guess_extension(ctype) or ""
            ext = guess.lstrip(".")
            name = name + guess
        if ext in _RUNNABLE or ctype in ("application/x-msdownload", "application/x-msdos-program", "application/x-ms-installer"):
            raise ActionError(f"'{name}' is a program or script. JARVIS does not download things that run code; the user can do that themselves.")
        if ctype == "text/html" and ext in ("", "htm", "html"):
            raise ActionError("That address is a web page, not a file. Use open_url to show it, or find the file's direct link.")
        size = int(resp.headers.get("Content-Length") or 0)
        if size > MAX_DOWNLOAD:
            raise ActionError(f"'{name}' is {size / 1024**3:.1f} GB; JARVIS downloads files up to 1 GB.")
        folder = Path.home() / "Downloads" / "JARVIS"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        stem, suffix, i = target.stem, target.suffix, 1
        while target.exists():
            target = folder / f"{stem} ({i}){suffix}"
            i += 1
        part = target.with_name(target.name + ".part")
        done = 0
        try:
            with part.open("wb") as f:
                while chunk := resp.read(256 * 1024):
                    done += len(chunk)
                    if done > MAX_DOWNLOAD:
                        raise ActionError(f"'{name}' is over 1 GB; stopped.")
                    f.write(chunk)
            part.replace(target)
        finally:
            part.unlink(missing_ok=True)
    try:  # Mark of the Web: Windows treats it as from the internet (SmartScreen, Protected View)
        with open(str(target) + ":Zone.Identifier", "w", encoding="ascii") as z:
            z.write(f"[ZoneTransfer]\nZoneId=3\nHostUrl={url.split('?')[0]}\n")
    except OSError:
        pass
    opened = args.get("open", True) is not False
    if opened:
        os.startfile(str(target))
    return {"saved": str(target), "size_mb": round(done / 1024**2, 2), "type": ctype, "opened": opened}


# ======================= voice =======================

@action
def voice_enroll(args: dict[str, Any]) -> dict[str, Any]:
    try:
        return voice.instance().enroll(float(args.get("seconds") or 15))
    except (RuntimeError, ValueError) as e:
        raise ActionError(str(e)) from e


@action
def voice_forget(_: dict[str, Any]) -> dict[str, Any]:
    from . import speaker

    v = speaker.verifier()
    if v:
        v.forget()
    return {"enrolled": False}


@action
def voice_set(args: dict[str, Any]) -> dict[str, Any]:
    return voice.instance().set_enabled(bool(args.get("enabled")), args.get("mode"))


_LIVE: live_audio.LiveAudio | None = None


def attach_live(send: Any) -> live_audio.LiveAudio:
    global _LIVE
    _LIVE = live_audio.LiveAudio(send)
    voice.instance().live_busy = lambda: _LIVE.mic_on if _LIVE else False
    voice.instance().live_talking = lambda: _LIVE.talking if _LIVE else False
    voice.instance().on_barge = lambda: _LIVE.flush() if _LIVE else None
    return _LIVE


@action
def live_mic(args: dict[str, Any]) -> dict[str, Any]:
    if _LIVE is None:
        raise ActionError("Live audio is not ready.")
    _LIVE.set_mic(bool(args.get("on")))
    return {"mic": _LIVE.mic_on}


@action
def voice_status(_: dict[str, Any]) -> dict[str, Any]:
    from . import speaker

    v = voice.instance()
    sv = speaker.verifier()
    return {"enabled": v.enabled, **voice.prepare(), "speaker_model": sv is not None, "voiceprint": bool(sv and sv.enrolled)}


@action
def speak(args: dict[str, Any]) -> dict[str, Any]:
    try:
        return voice.instance().speak(_str(args, "text"))
    except RuntimeError as e:
        raise ActionError(f"Could not speak: {e}") from e


@action
def listen_now(_: dict[str, Any]) -> dict[str, Any]:
    """Push-to-talk: start taking a request now, without the wake word."""
    setup = voice.prepare()
    if not setup["whisper"] or voice.FILES["whisper_model"] in setup["missing"]:
        raise ActionError("Whisper is not installed yet, so JARVIS cannot listen. See Settings > Voice.")
    voice.instance().on_wake(1.0, by_voice=False)  # the user pressed the button: no voice check needed
    return {"listening": True}


@action
def stop_speaking(_: dict[str, Any]) -> dict[str, Any]:
    voice.instance().stop_speaking()
    return {"stopped": True}


# ======================= audio, media, display =======================

@action
def volume(args: dict[str, Any]) -> dict[str, Any]:
    op = (_str(args, "action", False) or "get").lower()
    before, muted = win.get_volume()
    step = int(args.get("step") or 10)
    if op == "set":
        if args.get("level") is None:
            raise ActionError("Missing 'level' (0-100).")
        win.set_volume(int(args["level"]))
    elif op == "up":
        win.set_volume(before + step)
    elif op == "down":
        win.set_volume(before - step)
    elif op in ("mute", "unmute"):
        win.set_mute(op == "mute")
    elif op != "get":
        raise ActionError(f"Unknown volume action '{op}'.")
    after, muted_after = win.get_volume()
    return {"volume_before": before, "volume_now": after, "muted_before": muted, "muted_now": muted_after}


@action
def media(args: dict[str, Any]) -> dict[str, Any]:
    op = _str(args, "action").lower()
    keys = {"play_pause": "media_play_pause", "play": "media_play_pause", "pause": "media_play_pause",
            "next": "media_next", "previous": "media_previous", "stop": "media_stop"}
    if op not in keys:
        raise ActionError(f"Unknown media action '{op}'. Use play_pause, next, previous or stop.")
    win.press(keys[op])
    return {"sent": op, "note": "Sent the media key to whichever app is currently playing."}


@action
def brightness(args: dict[str, Any]) -> dict[str, Any]:
    get = "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop).CurrentBrightness"
    try:
        before = int(win.powershell(get).splitlines()[0])
    except (win.WinError, ValueError, IndexError) as e:
        raise ActionError("This display does not support software brightness control (usually only laptop screens do).") from e
    level = args.get("level")
    if level is None:
        return {"brightness": before}
    level = max(0, min(100, int(level)))
    win.powershell(
        "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
        f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=[uint32]1; Brightness=[byte]{level}}} | Out-Null"
    )
    return {"brightness_before": before, "brightness_now": int(win.powershell(get).splitlines()[0])}


# ======================= screen, clipboard, keyboard =======================

def screenshots_dir() -> Path:
    d = Path.home() / "Pictures" / "JARVIS"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _grab():
    """The screen as the user sees it behind JARVIS: JARVIS's own window would otherwise cover what was asked about."""
    from PIL import ImageGrab

    with win.windows_hidden(int(os.environ.get("JARVIS_PARENT_PID", "0") or 0)):
        return ImageGrab.grab(all_screens=True)


SCREENSHOT_TTL_S = 180  # the user's rule: no screenshot JARVIS takes outlives three minutes


def purge_old_screenshots(now: float | None = None) -> int:
    """Permanently delete screenshots JARVIS made that are older than SCREENSHOT_TTL_S: saved ones in
    Pictures/JARVIS and any look_at_screen capture the backend did not get to delete (e.g. after a crash).
    Only JARVIS's own file names are touched."""
    import tempfile

    now = now or time.time()
    candidates = list(screenshots_dir().glob("Screenshot *.png")) + list(Path(tempfile.gettempdir()).glob("jarvis-screen-*.jpg"))
    removed = 0
    for p in candidates:
        try:
            if now - p.stat().st_mtime > SCREENSHOT_TTL_S:
                p.unlink()
                removed += 1
        except OSError:
            pass  # in use or already gone; the next sweep retries
    return removed


def warm_app_index() -> None:
    """Build the installed-apps list ahead of the first 'open X' request; Get-StartApps is slow."""
    try:
        _apps()
    except Exception:  # noqa: BLE001 - the first real request will retry and report the error
        pass


@action
def screenshot(_: dict[str, Any]) -> dict[str, Any]:
    img = _grab()
    path = screenshots_dir() / f"Screenshot {dt.datetime.now():%Y-%m-%d %H%M%S}.png"
    img.save(path)
    return {"saved_to": str(path), "width": img.width, "height": img.height, "deleted_after_seconds": SCREENSHOT_TTL_S}


@action
def capture_screen(_: dict[str, Any]) -> dict[str, Any]:
    """Temporary, downscaled capture for the AI to look at; the backend deletes it after use."""
    import tempfile

    img = _grab().convert("RGB")
    img.thumbnail((1600, 1600))
    fd, path = tempfile.mkstemp(prefix="jarvis-screen-", suffix=".jpg")
    os.close(fd)
    img.save(path, "JPEG", quality=80)
    return {"path": path, "width": img.width, "height": img.height}


def _fraction(args: dict[str, Any], key: str) -> float:
    try:
        v = float(args[key])
    except (KeyError, TypeError, ValueError) as e:
        raise ActionError(f"'{key}' must be a fraction of the screen between 0 and 1.") from e
    if not 0.0 <= v <= 1.0:
        raise ActionError(f"'{key}' must be between 0 and 1.")
    return v


def _out_of_the_way(fx: float, fy: float):
    """JARVIS's own window must not catch a click meant for the app behind it: hide it if it covers the point."""
    import contextlib

    pid = int(os.environ.get("JARVIS_PARENT_PID", "0") or 0)
    left, top, w, h = win.virtual_screen()
    if pid and win.windows_at(pid, left + int(fx * w), top + int(fy * h)):
        return win.windows_hidden(pid, settle=0.15)
    return contextlib.nullcontext()


@action
def mouse_click(args: dict[str, Any]) -> dict[str, Any]:
    """Click at a point given as fractions of the whole screenshot (x from the left, y from the top)."""
    fx, fy = _fraction(args, "x"), _fraction(args, "y")
    button = str(args.get("button") or "left").lower()
    if button not in ("left", "right", "middle"):
        raise ActionError("button must be left, right or middle.")
    with _out_of_the_way(fx, fy):
        win.mouse_click(fx, fy, button, bool(args.get("double")))
        time.sleep(0.25)
    left, top, w, h = win.virtual_screen()
    return {"clicked": {"x": left + int(fx * w), "y": top + int(fy * h)}, "button": button, "double": bool(args.get("double"))}


@action
def mouse_scroll(args: dict[str, Any]) -> dict[str, Any]:
    clicks = int(args.get("clicks") or 5)
    over = ("x" in args and "y" in args and args["x"] is not None)
    win.mouse_scroll(clicks, _fraction(args, "x") if over else None, _fraction(args, "y") if over else None)
    return {"scrolled": clicks}


@action
def clipboard_get(_: dict[str, Any]) -> dict[str, Any]:
    import pyperclip

    text = pyperclip.paste()
    return {"text": text[:4000], "length": len(text), "truncated": len(text) > 4000}


@action
def clipboard_set(args: dict[str, Any]) -> dict[str, Any]:
    import pyperclip

    text = str(args.get("text", ""))
    pyperclip.copy(text)
    if pyperclip.paste() != text:
        raise ActionError("Windows did not accept the new clipboard contents.")
    return {"copied_characters": len(text)}


@action
def press_keys(args: dict[str, Any]) -> dict[str, Any]:
    keys = args.get("keys")
    if not isinstance(keys, list) or not keys:
        raise ActionError("Give 'keys' as a list, e.g. [\"ctrl\", \"c\"].")
    combo = [str(k) for k in keys]
    if sorted(k.lower() for k in combo) in (["alt", "ctrl", "delete"], ["alt", "ctrl", "del"]):
        raise ActionError("Ctrl+Alt+Delete cannot be sent by software; Windows blocks it by design.")
    win.press(*combo)
    return {"pressed": "+".join(combo)}


@action
def type_text(args: dict[str, Any]) -> dict[str, Any]:
    """Types into the focused window by pasting, then restores the clipboard."""
    import pyperclip

    text = str(args.get("text", ""))
    if not text:
        raise ActionError("Missing 'text'.")
    saved = pyperclip.paste()
    pyperclip.copy(text)
    time.sleep(0.05)
    win.press("ctrl", "v")
    time.sleep(0.15)
    pyperclip.copy(saved)
    return {"typed_characters": len(text), "into": "the focused window"}


# ======================= files =======================

_USER_DIRS = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"]
_SYSTEM_ROOTS = [os.environ.get("SystemRoot", r"C:\Windows"), os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("ProgramData", r"C:\ProgramData")]


def _resolve(path: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
    if not p.is_absolute():
        first = p.parts[0].lower() if p.parts else ""
        named = next((d for d in _USER_DIRS if d.lower() == first), None)
        p = (Path.home() / named / Path(*p.parts[1:])) if named else (Path.home() / p)
    return p.resolve()


def _guard(p: Path) -> None:
    s = str(p).lower()
    if any(s == r.lower() or s.startswith(r.lower() + "\\") for r in _SYSTEM_ROOTS if r):
        raise ActionError(f"{p} is a protected system location; I won't modify it.")
    if p == p.anchor or p == Path.home():
        raise ActionError(f"Refusing to operate on {p} as a whole.")


@action
def find_files(args: dict[str, Any]) -> dict[str, Any]:
    query = _str(args, "query").lower()
    roots = [_resolve(_str(args, "folder"))] if args.get("folder") else [Path.home() / d for d in _USER_DIRS]
    ext = (_str(args, "extension", False) or "").lower().lstrip(".")
    limit = min(int(args.get("limit") or 25), 100)
    deadline = time.time() + 8
    hits: list[dict[str, Any]] = []
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__", "AppData")]
            for n in dirnames + filenames:
                if query in n.lower() and (not ext or n.lower().endswith("." + ext)):
                    full = Path(dirpath) / n
                    try:
                        st = full.stat()
                    except OSError:
                        continue
                    hits.append({"path": str(full), "folder": full.is_dir(), "size_kb": round(st.st_size / 1024),
                                 "modified": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
            if len(hits) >= limit or time.time() > deadline:
                break
        if len(hits) >= limit or time.time() > deadline:
            break
    hits.sort(key=lambda h: h["modified"], reverse=True)
    return {"query": query, "searched": [str(r) for r in roots], "results": hits[:limit],
            "complete": time.time() <= deadline}


@action
def open_path(args: dict[str, Any]) -> dict[str, Any]:
    p = _resolve(_str(args, "path"))
    if not p.exists():
        raise ActionError(f"{p} does not exist.")
    os.startfile(str(p))
    return {"opened": str(p), "kind": "folder" if p.is_dir() else "file"}


@action
def list_folder(args: dict[str, Any]) -> dict[str, Any]:
    p = _resolve(_str(args, "path"))
    if not p.is_dir():
        raise ActionError(f"{p} is not a folder.")
    entries = []
    for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:200]:
        try:
            entries.append({"name": e.name, "folder": e.is_dir(), "size_kb": None if e.is_dir() else round(e.stat().st_size / 1024)})
        except OSError:
            continue
    return {"folder": str(p), "entries": entries, "total": len(list(p.iterdir()))}


@action
def create_folder(args: dict[str, Any]) -> dict[str, Any]:
    p = _resolve(_str(args, "path"))
    _guard(p)
    existed = p.exists()
    p.mkdir(parents=True, exist_ok=True)
    return {"folder": str(p), "already_existed": existed}


@action
def move_path(args: dict[str, Any]) -> dict[str, Any]:
    src = _resolve(_str(args, "source"))
    dst = _resolve(_str(args, "destination"))
    _guard(src)
    _guard(dst)
    if not src.exists():
        raise ActionError(f"{src} does not exist.")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise ActionError(f"{dst} already exists; I won't overwrite it.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"moved_from": str(src), "moved_to": str(dst)}


@action
def delete_path(args: dict[str, Any]) -> dict[str, Any]:
    p = _resolve(_str(args, "path"))
    _guard(p)
    if not p.exists():
        raise ActionError(f"{p} does not exist.")
    win.recycle(str(p))
    if p.exists():
        raise ActionError(f"{p} is still there; Windows did not delete it.")
    return {"moved_to_recycle_bin": str(p)}


_CATEGORIES = {
    "Images": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "svg", "tif", "tiff"},
    "Documents": {"pdf", "doc", "docx", "txt", "rtf", "odt", "xls", "xlsx", "csv", "ppt", "pptx", "md"},
    "Videos": {"mp4", "mkv", "mov", "avi", "wmv", "webm"},
    "Audio": {"mp3", "wav", "flac", "aac", "ogg", "m4a"},
    "Archives": {"zip", "rar", "7z", "tar", "gz"},
    "Installers": {"exe", "msi", "msix"},
    "Code": {"py", "js", "ts", "html", "css", "json", "java", "c", "cpp", "rs", "go"},
}


@action
def organize_folder(args: dict[str, Any]) -> dict[str, Any]:
    """Sort loose files in a folder into category subfolders. dry_run=True only reports the plan."""
    p = _resolve(_str(args, "path"))
    _guard(p)
    if not p.is_dir():
        raise ActionError(f"{p} is not a folder.")
    plan = []
    for f in p.iterdir():
        if not f.is_file():
            continue
        cat = next((c for c, exts in _CATEGORIES.items() if f.suffix.lower().lstrip(".") in exts), "Other")
        plan.append((f, p / cat / f.name))
    if args.get("dry_run", True):
        counts: dict[str, int] = {}
        for _, d in plan:
            counts[d.parent.name] = counts.get(d.parent.name, 0) + 1
        return {"folder": str(p), "would_move": len(plan), "by_category": counts, "dry_run": True}
    moved, skipped = [], []
    for src, dst in plan:
        if dst.exists():
            skipped.append(src.name)
            continue
        dst.parent.mkdir(exist_ok=True)
        shutil.move(str(src), str(dst))
        moved.append(f"{src.name} -> {dst.parent.name}")
    return {"folder": str(p), "moved": moved, "skipped_name_conflicts": skipped}


# ======================= power & Windows =======================

@action
def lock_screen(_: dict[str, Any]) -> dict[str, Any]:
    win.lock_workstation()
    return {"locked": True}


@action
def sleep_pc(_: dict[str, Any]) -> dict[str, Any]:
    win.powershell("Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false) | Out-Null")
    return {"sleeping": True}


@action
def power(args: dict[str, Any]) -> dict[str, Any]:
    op = _str(args, "action").lower()
    delay = max(10, min(int(args.get("delay_seconds") or 60), 3600))
    flags = {"restart": "/r", "shutdown": "/s"}
    if op == "cancel":
        r = subprocess.run(["shutdown", "/a"], capture_output=True, text=True, creationflags=win.CREATE_NO_WINDOW)
        if r.returncode != 0:
            raise ActionError("There was no pending shutdown or restart to cancel.")
        return {"cancelled": True}
    if op not in flags:
        raise ActionError(f"Unknown power action '{op}'. Use restart, shutdown or cancel.")
    r = subprocess.run(["shutdown", flags[op], "/t", str(delay), "/c", f"JARVIS: {op} requested"],
                       capture_output=True, text=True, creationflags=win.CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise ActionError((r.stderr or r.stdout).strip() or f"shutdown.exe failed ({r.returncode}).")
    return {"scheduled": op, "in_seconds": delay, "cancel_with": "power action=cancel"}


_SETTINGS_PAGES = {
    "display": "display", "sound": "sound", "bluetooth": "bluetooth", "wifi": "network-wifi", "network": "network",
    "battery": "batterysaver", "power": "powersleep", "storage": "storagesense", "apps": "appsfeatures",
    "default apps": "defaultapps", "notifications": "notifications", "update": "windowsupdate", "windows update": "windowsupdate",
    "privacy": "privacy", "personalization": "personalization", "background": "personalization-background",
    "themes": "themes", "mouse": "mousetouchpad", "keyboard": "typing", "time": "dateandtime", "language": "regionlanguage",
    "accounts": "yourinfo", "about": "about", "printers": "printers", "focus": "quiethours", "night light": "nightlight",
}


@action
def open_settings(args: dict[str, Any]) -> dict[str, Any]:
    page = (_str(args, "page", False) or "").lower()
    if page in ("task manager", "taskmgr"):
        subprocess.Popen(["taskmgr.exe"])
        return {"opened": "Task Manager"}
    if page in ("control panel", "control"):
        subprocess.Popen(["control.exe"])
        return {"opened": "Control Panel"}
    uri = "ms-settings:" + _SETTINGS_PAGES.get(page, "")
    if page and page not in _SETTINGS_PAGES:
        raise ActionError(f"Unknown settings page '{page}'. Known: {', '.join(sorted(_SETTINGS_PAGES))}, task manager, control panel.")
    os.startfile(uri)
    return {"opened": f"Settings{(' > ' + page) if page else ''}"}


@action
def notify(args: dict[str, Any]) -> dict[str, Any]:
    title = _str(args, "title")
    body = _str(args, "body", False)
    win.toast(title, body)
    return {"shown": True}
