"""Windows DPAPI: encrypt data so only the current Windows user on this machine can read it."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_ENTROPY = b"JARVIS-settings-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_P = ctypes.POINTER(_Blob)
_crypt32.CryptProtectData.argtypes = [_P, wintypes.LPCWSTR, _P, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, _P]
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = [_P, ctypes.c_void_p, _P, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, _P]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _take(out: _Blob) -> bytes:
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(data: bytes) -> bytes:
    inp, _b1 = _blob(data)
    ent, _b2 = _blob(_ENTROPY)
    out = _Blob()
    if not _crypt32.CryptProtectData(ctypes.byref(inp), "JARVIS", ctypes.byref(ent), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _take(out)


def unprotect(data: bytes) -> bytes:
    inp, _b1 = _blob(data)
    ent, _b2 = _blob(_ENTROPY)
    out = _Blob()
    if not _crypt32.CryptUnprotectData(ctypes.byref(inp), None, ctypes.byref(ent), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _take(out)
