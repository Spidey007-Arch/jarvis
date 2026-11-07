"""Windows DPAPI: encrypt data so only the current Windows user on this machine can read it.

Elsewhere (Docker) there is no DPAPI: AES-GCM with a key in a file only this user can read, in the data folder."""
from __future__ import annotations

import os
import sys

if sys.platform != "win32":
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from .paths import data_dir

    def _key() -> bytes:
        path = data_dir() / ".settings.key"
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(32))
        return path.read_bytes()

    def protect(data: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(_key()).encrypt(nonce, data, None)

    def unprotect(data: bytes) -> bytes:
        return AESGCM(_key()).decrypt(data[:12], data[12:], None)
else:
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
