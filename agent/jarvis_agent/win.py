"""Thin ctypes wrappers over Windows APIs, so the agent needs no third-party packages."""
from __future__ import annotations

import ctypes
import subprocess
import uuid
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

CREATE_NO_WINDOW = 0x08000000


class WinError(Exception):
    """A Windows call failed; the message says which call and why."""


def powershell(script: str, timeout: float = 20.0) -> str:
    """Run a PowerShell snippet hidden. Raises WinError with PowerShell's own error text."""
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise WinError(msg[0] if msg else f"PowerShell exited with code {r.returncode}")
    return r.stdout.strip()


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
_CLSCTX_ALL = 23


def _method(obj: ctypes.c_void_p, index: int, restype, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise WinError(f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X}).")


class _Endpoint:
    """Default playback device's IAudioEndpointVolume, released on exit."""

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
            dev, ctypes.byref(_IID_IAudioEndpointVolume), _CLSCTX_ALL, None, ctypes.byref(vol)), "Opening the volume control")
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
