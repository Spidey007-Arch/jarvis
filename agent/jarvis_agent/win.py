"""Thin ctypes wrappers over Windows APIs, so the agent needs no third-party packages."""
from __future__ import annotations

import ctypes
import subprocess
import time
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from typing import Iterator

user32 = ctypes.WinDLL("user32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

CREATE_NO_WINDOW = 0x08000000
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000


class WinError(Exception):
    """A Windows call failed; the message says which call and why."""


def boost_thread() -> None:
    """Run the calling thread above normal priority, so audio keeps flowing when the CPU is busy."""
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentThread.restype = wintypes.HANDLE
        k.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        k.SetThreadPriority(k.GetCurrentThread(), 2)  # THREAD_PRIORITY_HIGHEST
    except (AttributeError, OSError):
        pass


def powershell(script: str, timeout: float = 20.0) -> str:
    """Run a PowerShell snippet hidden. Raises WinError with PowerShell's own error text."""
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        # Above normal so lookups still finish when something else pins the CPU.
        capture_output=True, text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW | ABOVE_NORMAL_PRIORITY_CLASS,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise WinError(msg[0] if msg else f"PowerShell exited with code {r.returncode}")
    return r.stdout.strip()


# ---------------- windows ----------------

SW_HIDE = 0
SW_SHOWNA = 8
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


def visible_windows_of(pid: int) -> list[int]:
    """Visible top-level windows owned by a process."""
    found: list[int] = []

    def cb(hwnd: int, _: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return found


user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80


def titled_windows() -> list[tuple[int, str]]:
    """(pid, title) of every visible top-level window with a title: the apps the user has open."""
    found: list[tuple[int, str]] = []

    def cb(hwnd: int, _: int) -> bool:
        rect = wintypes.RECT()
        if user32.IsWindowVisible(hwnd) and not user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW                 and (user32.IsIconic(hwnd) or user32.GetWindowRect(hwnd, ctypes.byref(rect)) and rect.right - rect.left > 100):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                found.append((owner.value, buf.value))
        return True

    user32.EnumWindows(_WNDENUMPROC(cb), 0)
    return found


@contextmanager
def windows_hidden(pid: int, settle: float = 0.25) -> Iterator[None]:
    """Hide a process's windows (e.g. JARVIS itself) for a moment, then show them again unchanged."""
    hidden = visible_windows_of(pid) if pid else []
    for h in hidden:
        user32.ShowWindow(h, SW_HIDE)
    if hidden:
        time.sleep(settle)  # let the compositor draw what was behind them
    try:
        yield
    finally:
        for h in hidden:
            user32.ShowWindow(h, SW_SHOWNA)  # same size and maximized state, without stealing focus


user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p


@contextmanager
def physical_pixels() -> Iterator[None]:
    """Measure the screen in real pixels (per-monitor DPI aware), the same units as a screenshot."""
    old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        yield
    finally:
        if old:
            user32.SetThreadDpiAwarenessContext(old)


def windows_at(pid: int, x: int, y: int) -> list[int]:
    """Which of a process's visible windows cover screen point (x, y), in physical pixels."""
    hits = []
    with physical_pixels():
        for h in visible_windows_of(pid) if pid else []:
            r = wintypes.RECT()
            if user32.GetWindowRect(h, ctypes.byref(r)) and r.left <= x < r.right and r.top <= y < r.bottom:
                hits.append(h)
    return hits


# ---------------- mouse ----------------

MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x0001, 0x8000, 0x4000
_BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
MOUSEEVENTF_WHEEL = 0x0800
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]


def virtual_screen() -> tuple[int, int, int, int]:
    """Left, top, width, height of the whole desktop across all monitors, in physical pixels."""
    with physical_pixels():
        return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))  # type: ignore[return-value]


def mouse_move(fx: float, fy: float) -> None:
    """Move to a point given as fractions (0..1) of the whole desktop. Absolute input is normalized to
    0..65535 across the virtual desktop, so it lands on the same pixel whatever the display scaling."""
    nx = int(round(max(0.0, min(1.0, fx)) * 65535))
    ny = int(round(max(0.0, min(1.0, fy)) * 65535))
    user32.mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny, 0, 0)


def mouse_click(fx: float, fy: float, button: str = "left", double: bool = False) -> None:
    down, up = _BUTTONS.get(button, _BUTTONS["left"])
    mouse_move(fx, fy)
    time.sleep(0.08)  # let hover effects and focus catch up, as a hand would
    for _ in range(2 if double else 1):
        user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.06)


def mouse_scroll(clicks: int, fx: float | None = None, fy: float | None = None) -> None:
    """Scroll by wheel clicks (positive is up), over a point if given."""
    if fx is not None and fy is not None:
        mouse_move(fx, fy)
        time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, ctypes.c_uint32(int(clicks) * 120).value, 0)


# ---------------- keyboard ----------------

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "printscreen": 0x2C, "capslock": 0x14,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "media_next": 0xB0, "media_previous": 0xB1, "media_stop": 0xB2, "media_play_pause": 0xB3,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
_EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}


def vk_of(key: str) -> int:
    k = key.strip().lower()
    if k in VK:
        return VK[k]
    if len(k) == 1 and (k.isalnum()):
        return ord(k.upper())
    raise WinError(f"Unknown key '{key}'.")


def press(*keys: str) -> None:
    """Press keys together (e.g. press('ctrl', 'shift', 'esc')) and release in reverse order."""
    codes = [vk_of(k) for k in keys]
    for c in codes:
        user32.keybd_event(c, 0, KEYEVENTF_EXTENDEDKEY if c in _EXTENDED else 0, 0)
    for c in reversed(codes):
        user32.keybd_event(c, 0, (KEYEVENTF_EXTENDEDKEY if c in _EXTENDED else 0) | KEYEVENTF_KEYUP, 0)


def lock_workstation() -> None:
    if not user32.LockWorkStation():
        raise WinError(f"LockWorkStation failed: {ctypes.WinError(ctypes.get_last_error())}")


# ---------------- Core Audio (master volume) ----------------

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, s: str) -> "GUID":
        u = uuid.UUID(s)
        g = cls(u.time_low, u.time_mid, u.time_hi_version)
        g.Data4[:] = list(u.bytes[8:])
        return g


_CLSID_MMDeviceEnumerator = GUID.of("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_IMMDeviceEnumerator = GUID.of("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_IAudioEndpointVolume = GUID.of("5CDF2C82-841E-4546-9722-0CF74078229A")
_IID_IAudioMeterInformation = GUID.of("C02216F6-8C67-4B5B-9D00-D008E73E0064")
_CLSCTX_ALL = 23


def _method(obj: ctypes.c_void_p, index: int, restype, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise WinError(f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X}).")


class _Endpoint:
    """An interface of the default playback device (IAudioEndpointVolume unless told otherwise), released on exit."""

    def __init__(self, iid: GUID = _IID_IAudioEndpointVolume, what: str = "Opening the volume control") -> None:
        self.iid, self.what = iid, what

    def __enter__(self) -> ctypes.c_void_p:
        hr = ole32.CoInitializeEx(None, 0x2)  # apartment-threaded; S_FALSE / RPC_E_CHANGED_MODE are fine
        self._uninit = hr in (0, 1)
        self._objs: list[ctypes.c_void_p] = []
        enum = ctypes.c_void_p()
        _check(ole32.CoCreateInstance(ctypes.byref(_CLSID_MMDeviceEnumerator), None, _CLSCTX_ALL,
                                      ctypes.byref(_IID_IMMDeviceEnumerator), ctypes.byref(enum)), "Creating the audio device enumerator")
        self._objs.append(enum)
        dev = ctypes.c_void_p()
        hr = _method(enum, 4, ctypes.HRESULT, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(enum, 0, 1, ctypes.byref(dev))
        if hr < 0:
            raise WinError("No default audio output device is available.")
        self._objs.append(dev)
        vol = ctypes.c_void_p()
        _check(_method(dev, 3, ctypes.HRESULT, ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            dev, ctypes.byref(self.iid), _CLSCTX_ALL, None, ctypes.byref(vol)), self.what)
        self._objs.append(vol)
        return vol

    def __exit__(self, *exc) -> None:
        for o in reversed(self._objs):
            _method(o, 2, wintypes.ULONG)(o)
        if self._uninit:
            ole32.CoUninitialize()


def get_volume() -> tuple[int, bool]:
    with _Endpoint() as v:
        level = ctypes.c_float()
        _check(_method(v, 9, ctypes.HRESULT, ctypes.POINTER(ctypes.c_float))(v, ctypes.byref(level)), "Reading the volume")
        muted = wintypes.BOOL()
        _check(_method(v, 15, ctypes.HRESULT, ctypes.POINTER(wintypes.BOOL))(v, ctypes.byref(muted)), "Reading mute state")
        return round(level.value * 100), bool(muted.value)


def set_volume(percent: int) -> None:
    with _Endpoint() as v:
        _check(_method(v, 7, ctypes.HRESULT, ctypes.c_float, ctypes.c_void_p)(v, max(0, min(100, percent)) / 100.0, None), "Setting the volume")


def set_mute(mute: bool) -> None:
    with _Endpoint() as v:
        _check(_method(v, 14, ctypes.HRESULT, wintypes.BOOL, ctypes.c_void_p)(v, bool(mute), None), "Changing mute")


class PlaybackMeter:
    """How loud the PC itself is playing right now (0..1), e.g. a video or music. Kept open while
    listening, so each reading is one cheap call; reopened if the output device changes."""

    def __init__(self) -> None:
        self._ep: _Endpoint | None = None
        self._meter: ctypes.c_void_p | None = None

    def peak(self) -> float:
        try:
            if self._meter is None:
                self._ep = _Endpoint(_IID_IAudioMeterInformation, "Opening the output meter")
                self._meter = self._ep.__enter__()
            v = ctypes.c_float()
            _check(_method(self._meter, 3, ctypes.HRESULT, ctypes.POINTER(ctypes.c_float))(self._meter, ctypes.byref(v)), "Reading the output meter")
            return float(v.value)
        except (WinError, OSError):
            self.close()
            return 0.0

    def close(self) -> None:
        if self._ep is not None:
            try:
                self._ep.__exit__(None, None, None)
            except OSError:
                pass
        self._ep = self._meter = None


# ---------------- Recycle Bin ----------------

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle(path: str) -> None:
    """Move a file or folder to the Recycle Bin (recoverable), without any UI."""
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = _SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0", None, FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise WinError(f"Windows could not move it to the Recycle Bin (code {rc}).")


# ---------------- notifications ----------------

_PS_APPID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def toast(title: str, body: str) -> None:
    """Native Windows toast notification."""
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")).replace("'", "''")

    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{esc(title)}</text><text>{esc(body)}</text></binding></visual></toast>')
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_PS_APPID}').Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""
    powershell(script)
