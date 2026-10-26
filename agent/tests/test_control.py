"""Full control on the agent side: real command output, exact edits with backups, and commands that would wreck Windows refused."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_agent import actions  # noqa: E402


class Commands(unittest.TestCase):
    def test_output_and_exit_code(self) -> None:
        out = actions.run_command({"command": "Write-Output 'hi there'; exit 3"})
        self.assertEqual(out["exit_code"], 3)
        self.assertIn("hi there", out["output"])

    def test_wrecking_commands_refused(self) -> None:
        for cmd in ("format C: /q", "diskpart", "bcdedit /deletevalue", "vssadmin delete shadows /all", "Remove-Item -Recurse -Force C:\Windows",
                    "Set-MpPreference -DisableRealtimeMonitoring $true", "reg delete HKLM\SOFTWARE\X"):
            with self.assertRaises(actions.ActionError, msg=cmd):
                actions.run_command({"command": cmd})


class Files(unittest.TestCase):
    def test_write_read_edit_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LOCALAPPDATA": d}):
            f = str(Path(d) / "work" / "a.py")
            self.assertTrue(actions.write_file({"path": f, "content": "x = 1\ny = 2\n"})["created"])
            self.assertIn("2\ty = 2", actions.read_file({"path": f})["text"])
            out = actions.edit_file({"path": f, "old_text": "y = 2", "new_text": "y = 3"})
            self.assertEqual(Path(f).read_text(), "x = 1\ny = 3\n")
            self.assertEqual(Path(out["backup"]).read_text(), "x = 1\ny = 2\n")
            with self.assertRaises(actions.ActionError):
                actions.edit_file({"path": f, "old_text": "nope", "new_text": "z"})


if __name__ == "__main__":
    unittest.main()
