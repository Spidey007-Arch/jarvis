"""Sandboxed Python, for data someone else made (a CSV that was sent, a workbook to analyse) or code JARVIS writes to
answer a question, where running it like a normal program would be too trusting.

The code runs in a separate Python process, in its own empty folder, with only copies of the files it was given.
Before the code starts, an audit hook (Python's own security hook) refuses:
  - any network connection or name lookup,
  - starting programs (subprocess, os.system, os.startfile, exec/spawn) and reaching Windows through ctypes,
  - writing, deleting or renaming anything outside the sandbox folder, and reading outside it and Python's libraries,
  - the registry.
Windows limits the process too (a job object): memory, no child processes, and it dies with JARVIS. It has a time
limit. This is a strong guard against accidents and casual harm, not a virtual machine: don't use it for code that
is actively trying to break out. Files the code writes to out/ are kept in Documents\\JARVIS\\Sandbox."""
from __future__ import annotations

import ctypes
import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .actions import ActionError, _resolve, _str, action

MEMORY_MB = 1024
MAX_SECONDS = 120
MAX_OUTPUT = 12000

GUARD = r'''
import json, os, sys
sys.path[1:1] = json.load(open("_paths.json"))  # JARVIS's libraries (numpy, openpyxl, ...), given before anything else runs
ROOT = os.path.realpath(os.getcwd())
READ_OK = [ROOT] + [os.path.realpath(p) for p in {sys.prefix, sys.base_prefix, sys.exec_prefix, *sys.path} if p and os.path.isdir(p)]
_W = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

def _inside(path, roots):
    try:
        p = os.path.realpath(os.fsdecode(path))
    except Exception:
        return False
    return any(p == r or p.startswith(r + os.sep) for r in roots)

def _hook(event, args):
    # ctypes was set up before this hook (numpy needs the module); from here on no native library or function may be
    # looked up, because calling Windows directly is how code would get round every other rule here.
    # _winapi reaches Windows directly (CreateFile writes anywhere, OpenProcess/TerminateProcess touch other programs).
    if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn", "os.posix_spawn", "ctypes.dlopen", "ctypes.dlsym",
                         "ctypes.call_function", "_winapi.", "msvcrt.", "_wmi.",
                         "winreg.", "webbrowser.")) or event in ("os.system", "os.startfile", "os.kill"):
        raise PermissionError(f"Not allowed in the sandbox: {event}")
    # Libraries such as numpy register their own DLL folder; only folders of the installed libraries, never the sandbox's.
    if event == "os.add_dll_directory" and args and (_inside(args[0], [ROOT]) or not _inside(args[0], READ_OK)):
        raise PermissionError(f"Not allowed in the sandbox: {event} {args[0]}")
    # A native module (.pyd/.dll) is machine code: only the ones that come with Python and JARVIS's libraries may load,
    # never one written into the sandbox folder.
    if event == "import" and len(args) > 1 and args[1]:
        found = os.fsdecode(args[1])
        if _inside(found, [ROOT]) and not found.endswith(".py") or found.lower().endswith((".pyd", ".dll")) and not _inside(found, READ_OK):
            raise PermissionError(f"Not allowed in the sandbox: loading {found}")
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path, mode, flags = args[0], args[1] or "r", args[2] or 0
        writing = any(c in str(mode) for c in "wax+") or bool(flags & _W)
        if not _inside(path, [ROOT] if writing else READ_OK):
            raise PermissionError(f"The sandbox can only {'write' if writing else 'read'} files inside its folder: {path}")
    if event in ("os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.link", "os.symlink", "os.truncate", "shutil.rmtree",
                 "shutil.move", "shutil.copyfile", "os.utime"):
        for a in args:
            if isinstance(a, (str, bytes, os.PathLike)) and not _inside(a, [ROOT]):
                raise PermissionError(f"Not allowed outside the sandbox folder: {event} {a}")

code = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
import ctypes  # noqa: E402,F401 - its own Windows setup happens now, before the hook; later lookups are refused
sys.addaudithook(_hook)
sys.argv = ["main.py"]
exec(compile(code, "main.py", "exec"), {"__name__": "__main__"})
'''


def _job(proc: subprocess.Popen) -> Any:
    """Windows job object: a memory ceiling, no child processes, and killed when JARVIS closes."""
    k = ctypes.windll.kernel32

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.OpenProcess.restype = wintypes.HANDLE
    job = k.CreateJobObjectW(None, None)
    info = EXT()
    # 0x100 process memory, 0x8 active process limit, 0x2000 kill on close
    info.Basic.LimitFlags = 0x100 | 0x8 | 0x2000
    info.Basic.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = MEMORY_MB * 2**20
    k.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    handle = k.OpenProcess(0x1F0FFF, False, proc.pid)
    k.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(handle))
    k.CloseHandle(wintypes.HANDLE(handle))
    return job


def _tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else "…(earlier output cut)…\n" + text[-MAX_OUTPUT:]


@action
def run_sandboxed(args: dict[str, Any]) -> dict[str, Any]:
    """Run Python code in the sandbox with copies of the given files; returns its output and any files it made."""
    code = str(args.get("code") or "")
    if not code.strip():
        raise ActionError("No code to run.")
    files = args.get("files") or []
    if isinstance(files, str):
        files = [files]
    timeout = max(5, min(int(args.get("timeout") or 60), MAX_SECONDS))
    box = Path(tempfile.mkdtemp(prefix="jarvis-sandbox-"))
    try:
        copied = []
        for f in files[:20]:
            src = _resolve(str(f))
            if not src.is_file():
                raise ActionError(f"{src} is not a file.")
            if src.stat().st_size > 200 * 2**20:
                raise ActionError(f"{src.name} is over 200 MB.")
            shutil.copy2(src, box / src.name)
            copied.append(src.name)
        (box / "main.py").write_text(code, encoding="utf-8")
        (box / "_guard.py").write_text(GUARD, encoding="utf-8")
        import json
        import site

        (box / "_paths.json").write_text(json.dumps([p for p in site.getsitepackages() if os.path.isdir(p)]), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "NUMBER_OF_PROCESSORS")}
        env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", MPLBACKEND="Agg")
        # The real interpreter, not the venv's python.exe: that one is a launcher that starts a second process, which the
        # job object's one-process limit refuses.
        python = getattr(sys, "_base_executable", "") or sys.executable
        proc = subprocess.Popen([python, "-I", "_guard.py"], cwd=str(box), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, creationflags=0x08000000 | 0x00000004)  # no window, created suspended
        job = None
        try:
            job = _job(proc)
        finally:
            # resume the main thread now that the limits apply
            import psutil

            psutil.Process(proc.pid).resume()
        try:
            out, err = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            timed_out = True
        finally:
            if job:
                ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(job))
        made = [p for p in (box / "out").rglob("*") if p.is_file()] if (box / "out").is_dir() else []
        kept = []
        if made:
            dest = Path.home() / "Documents" / "JARVIS" / "Sandbox" / dt.datetime.now().strftime("%Y-%m-%d %H%M%S")
            dest.mkdir(parents=True, exist_ok=True)
            for p in made[:50]:
                target = dest / p.relative_to(box / "out")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
                kept.append(str(target))
        return {"exit_code": None if timed_out else proc.returncode, "timed_out": timed_out, "seconds_limit": timeout,
                "output": _tail(out.decode("utf-8", "replace").strip()), "errors": _tail(err.decode("utf-8", "replace").strip()),
                "input_files": copied, "files_made": kept,
                "note": "Sandboxed: no network, no programs, files only inside its folder. Save results to out/ to keep them."}
    finally:
        shutil.rmtree(box, ignore_errors=True)
