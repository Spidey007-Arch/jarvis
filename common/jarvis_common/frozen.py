"""Packaged (PyInstaller) builds: where bundled files live, and letting the .exe stand in for python.exe.

The installer ships jarvis-backend.exe and jarvis-agent.exe with no separate Python. A few features start Python
themselves (the agent's sandbox runs `python -I _guard.py`; the MCP server is `python -m jarvis_sdk.mcp_server`),
so in a packaged build sys.executable is our own .exe, and act_as_python() runs those calls instead of JARVIS."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))


def bundle_root(source_file: str) -> Path:
    """The repository root in development; the folder of bundled files in a packaged build."""
    return Path(getattr(sys, "_MEIPASS")) if FROZEN else Path(source_file).resolve().parents[2]


def act_as_python() -> None:
    """In a packaged build, `jarvis-x.exe [-I] script.py ...`, `-m module ...` or `-c code` runs that like python.exe
    would, then exits. With no arguments (how the app starts it) this returns and JARVIS starts normally; anything
    else is refused, so a stray call can never start a second JARVIS."""
    if not FROZEN or len(sys.argv) <= 1:
        return
    args = sys.argv[1:]
    while args and args[0] in ("-I", "-E", "-s", "-S", "-u", "-B"):
        args = args[1:]
    if len(args) >= 2 and args[0] == "-m":
        sys.argv = [args[1], *args[2:]]
        runpy.run_module(args[1], run_name="__main__", alter_sys=True)
    elif len(args) >= 2 and args[0] == "-c":
        sys.argv = ["-c", *args[2:]]
        exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102 - python -c, same as python.exe
    elif args and args[0].lower().endswith(".py"):
        script = Path(args[0]).resolve()
        sys.argv = [str(script), *args[1:]]
        sys.path.insert(0, str(script.parent))
        runpy.run_path(str(script), run_name="__main__")
    else:
        sys.stderr.write(f"{Path(sys.executable).name}: unsupported arguments {sys.argv[1:]!r}; "
                         "expected none, script.py, -m module or -c code\n")
        sys.exit(2)
    sys.exit(0)
