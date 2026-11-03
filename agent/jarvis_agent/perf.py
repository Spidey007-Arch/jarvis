"""Task Manager's own numbers: Windows performance counters (pdh.dll), read through ctypes.

psutil's CPU figure is "% Processor Time", which on modern CPUs that boost above their base clock reads lower than
Task Manager, and psutil has no figure for how busy a disk is (Task Manager's "Disk" column is active time, not how
full the drive is). These counters are the ones Task Manager shows, so JARVIS's numbers match what the user sees.

A Meter keeps one query open; each read() gives the average since the previous read, so a reading covers a span
of time instead of a single instant and doesn't jump around. If the counters aren't available, read() returns None
for them and callers fall back to psutil."""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Any

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
COUNTERS = {
    "cpu": r"\Processor Information(_Total)\% Processor Utility",  # Task Manager's CPU %, can exceed 100 when boosting
    "disk_idle": r"\PhysicalDisk(_Total)\% Idle Time",  # Task Manager's Disk % is the time the disk was busy
}
# Every process at once, in one query (asking psutil process by process takes seconds): Task Manager's CPU and
# Memory columns. % Processor Time here is per core, so it is divided by the number of cores.
PROCESS_COUNTERS = {
    "proc_cpu": r"\Process(*)\% Processor Time",
    "proc_mem": r"\Process(*)\Working Set - Private",
}


class _Value(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _Item(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _Value)]


try:
    _pdh = ctypes.WinDLL("pdh")
    _pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
    _pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
    _pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
    _pdh.PdhGetFormattedCounterValue.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Value)]
    _pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
    _pdh.PdhGetFormattedCounterArrayW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _pdh.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD
    _pdh.PdhCollectQueryData.restype = wintypes.DWORD
    _pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
    _pdh.PdhOpenQueryW.restype = wintypes.DWORD
    _pdh.PdhGetFormattedCounterValue.restype = wintypes.DWORD
except (OSError, AttributeError):
    _pdh = None


class Meter:
    """One open counter query. Thread-safe; read() returns the averages since the last read()."""

    def __init__(self, processes: bool = False) -> None:
        self._lock = threading.Lock()
        self._query = wintypes.HANDLE()
        self._counters: dict[str, wintypes.HANDLE] = {}
        self._last = 0.0
        if _pdh is None or _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
            self._query = None
            return
        for name, path in {**COUNTERS, **(PROCESS_COUNTERS if processes else {})}.items():
            h = wintypes.HANDLE()
            if _pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(h)) == 0:
                self._counters[name] = h
        self._collect()

    def _collect(self) -> bool:
        ok = self._query is not None and _pdh.PdhCollectQueryData(self._query) == 0
        self._last = time.monotonic()
        return ok

    def _value(self, name: str) -> float | None:
        h = self._counters.get(name)
        if h is None:
            return None
        v = _Value()
        if _pdh.PdhGetFormattedCounterValue(h, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, None, ctypes.byref(v)) != 0 or v.CStatus not in (0, 1):
            return None
        return v.doubleValue

    @property
    def has_processes(self) -> bool:
        return "proc_cpu" in self._counters

    def _array(self, name: str) -> dict[str, float]:
        """Every instance of a wildcard counter, summed by program name (chrome#3 counts as chrome)."""
        h = self._counters.get(name)
        if h is None:
            return {}
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        if _pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, ctypes.byref(size), ctypes.byref(count), None) != PDH_MORE_DATA:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if _pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return {}
        out: dict[str, float] = {}
        for item in ctypes.cast(buf, ctypes.POINTER(_Item))[:count.value]:
            label = (item.szName or "").split("#")[0]
            if label in ("_Total", "Idle", "") or item.FmtValue.CStatus not in (0, 1):
                continue
            out[label] = out.get(label, 0.0) + item.FmtValue.doubleValue
        return out

    def processes(self) -> dict[str, list[dict[str, Any]]] | None:
        """Top programs by CPU and by memory over the last read's span, or None if the counters aren't there.
        Call right after read() or fresh()."""
        with self._lock:
            if "proc_cpu" not in self._counters:
                return None
            cpu, mem = self._array("proc_cpu"), self._array("proc_mem")
        if not cpu and not mem:
            return None
        cores = os.cpu_count() or 1
        return {"top_cpu": [{"name": n, "cpu_percent": round(v / cores, 1)} for n, v in sorted(cpu.items(), key=lambda x: -x[1])[:5]],
                "top_memory": [{"name": n, "memory_mb": round(v / 1024**2)} for n, v in sorted(mem.items(), key=lambda x: -x[1])[:5]]}

    def read(self, min_span: float = 0.0) -> dict[str, Any]:
        """{"cpu_percent", "disk_active_percent", "span_s"}: averages over the time since the last read, which is
        made at least min_span seconds long (waiting if needed) so a fresh reading is never a single instant."""
        with self._lock:
            if self._query is None or not self._counters:
                return {"cpu_percent": None, "disk_active_percent": None, "span_s": 0.0}
            wait = min_span - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            span = time.monotonic() - self._last
            if not self._collect():
                return {"cpu_percent": None, "disk_active_percent": None, "span_s": 0.0}
            cpu, idle = self._value("cpu"), self._value("disk_idle")
        return {"cpu_percent": None if cpu is None else round(min(max(cpu, 0.0), 100.0), 1),  # shown like Task Manager: 0-100
                "disk_active_percent": None if idle is None else round(min(max(100.0 - idle, 0.0), 100.0), 1),
                "span_s": round(span, 1)}

    def fresh(self, seconds: float = 1.5) -> dict[str, Any]:
        """A reading of right now: starts a new span, waits `seconds`, and reads it (not the time since an old read)."""
        with self._lock:
            self._collect()
        return self.read(min_span=seconds)

    def close(self) -> None:
        with self._lock:
            if self._query is not None:
                _pdh.PdhCloseQuery(self._query)
                self._query = None
