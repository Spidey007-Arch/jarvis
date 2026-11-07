"""Handing coding jobs to a coding agent installed on this PC: Claude Code, OpenCode or OpenHands.

The job runs in the project folder the user approved, with a time limit, and only its report comes back. The task goes
in through stdin or a file, never on the command line: these tools are often .cmd files on Windows, where cmd.exe would
treat & or | in the text as commands. Each tool signs in on its own; JARVIS never sees its keys."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .actions import ActionError, _guard, _resolve, _str, action

log = logging.getLogger("jarvis.coding")

NO_WINDOW = 0x08000000
DEFAULT_MINUTES, MAX_MINUTES = 20, 60
REPORT_CHARS = 8000
LABELS = {"claude": "Claude Code", "opencode": "OpenCode", "openhands": "OpenHands"}


def command(tool: str, exe: str, task_file: str) -> tuple[list[str], bool]:
    """(the command, whether the task goes in on stdin)."""
    if tool == "claude":
        return [exe, "-p", "--permission-mode", "acceptEdits"], True
    if tool == "opencode":
        return [exe, "run"], True
    return [exe, "--headless", "-f", task_file], False


@action
def coding_tools(_: dict[str, Any]) -> dict[str, Any]:
    """Which coding agents can be used here."""
    return {"tools": [{"tool": t, "name": name, "installed": bool(shutil.which(t))} for t, name in LABELS.items()]}


@action
def hand_off_code(args: dict[str, Any]) -> dict[str, Any]:
    """Run one coding job and return the agent's report."""
    tool = {"claude code": "claude", "claudecode": "claude"}.get(_str(args, "tool").lower(), _str(args, "tool").lower())
    if tool not in LABELS:
        raise ActionError("tool must be claude, opencode or openhands.")
    exe = shutil.which(tool)
    if not exe:
        raise ActionError(f"{LABELS[tool]} isn't installed on this PC, or isn't on the PATH.")
    folder = _resolve(_str(args, "folder"))
    _guard(folder)
    if not Path(folder).is_dir():
        raise ActionError(f"{folder} isn't a folder.")
    task = _str(args, "task")
    minutes = max(1, min(int(args.get("minutes") or DEFAULT_MINUTES), MAX_MINUTES))
    with tempfile.TemporaryDirectory() as tmp:
        task_file = os.path.join(tmp, "task.txt")
        Path(task_file).write_text(task, encoding="utf-8")
        cmd, on_stdin = command(tool, exe, task_file)
        try:
            done = subprocess.run(cmd, cwd=folder, input=task if on_stdin else None, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=minutes * 60, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise ActionError(f"{LABELS[tool]} didn't finish in {minutes} minutes and was stopped. Any changes so far are still in the folder.") from e
    report = (done.stdout or "").strip() or (done.stderr or "").strip()
    log.info("coding job finished", extra={"fields": {"tool": tool, "code": done.returncode}})
    return {"tool": LABELS[tool], "folder": str(folder), "finished": done.returncode == 0, "report": report[-REPORT_CHARS:],
            "note": "Check what changed before calling it done (git diff, tests)."}
