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


class SpotifySearch(unittest.TestCase):
    def test_song_rows_come_from_title_and_song_lines(self) -> None:
        from jarvis_agent import uictl

        # what Windows OCR read on Spotify's search page for "alfaaz"
        lines = [{"x": 371, "y": 73, "w": 32, "h": 11, "text": "Songs"},
                 {"x": 414, "y": 135, "w": 49, "h": 12, "text": "Alfaaz"},
                 {"x": 380, "y": 220, "w": 87, "h": 10, "text": "Alfaaz - Vocals"},
                 {"x": 381, "y": 239, "w": 168, "h": 11, "text": "Song  Hamza Mark, Zain Zohaib"},
                 {"x": 380, "y": 273, "w": 35, "h": 10, "text": "Aarzu"},
                 {"x": 381, "y": 292, "w": 151, "h": 11, "text": "Song  Noor, Khan, Madhurxc"},
                 {"x": 380, "y": 786, "w": 87, "h": 10, "text": "Alfaaz -Vocals"},
                 {"x": 381, "y": 805, "w": 174, "h": 11, "text": "Single  Hamza Malik, Zain Zohaib"}]
        rows = uictl._song_rows(lines)
        self.assertEqual([r["text"] for r in rows], ["Alfaaz - Vocals", "Aarzu"])  # singles and headings aren't tracks
        self.assertEqual(rows[0]["artist"], "Hamza Mark, Zain Zohaib")


class Guard(unittest.TestCase):
    def test_a_whole_drive_or_home_is_refused(self) -> None:
        """Issue #2: a Path never equals the str anchor, so drive roots used to get through."""
        for p in (Path("C:\\"), Path("D:\\"), Path.home()):
            with self.assertRaises(actions.ActionError):
                actions._guard(p)
        actions._guard(Path.home() / "Documents" / "notes")  # an ordinary folder is fine


class JarvisSecrets(unittest.TestCase):
    def test_no_tool_reaches_the_saved_keys_or_the_token(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"JARVIS_DATA_DIR": d}):
            for name in ("settings.dat", "runtime.json", "Settings.DAT"):
                (Path(d) / name.lower()).write_text("secret")
                with self.assertRaises(actions.ActionError, msg=name):
                    actions.read_file({"path": str(Path(d) / name)})
            with self.assertRaises(actions.ActionError):  # copying it elsewhere first doesn't help
                actions.run("copy_path", {"source": str(Path(d) / "settings.dat"), "destination": str(Path(d).parent / "x.dat")})
            with self.assertRaises(actions.ActionError):  # nor changing anything in JARVIS's own folder
                actions.write_file({"path": str(Path(d) / "jarvis.db"), "content": "x"})
            (Path(d) / "logs").mkdir()
            (Path(d) / "logs" / "agent.log").write_text("ok")
            self.assertIn("ok", actions.read_file({"path": str(Path(d) / "logs" / "agent.log")})["text"])  # logs stay readable

    def test_commands_that_reach_for_secrets_are_refused(self) -> None:
        for cmd in ("Get-Content $env:LOCALAPPDATA\\JARVIS\\runtime.json", "echo $env:JARVIS_TOKEN",
                    "[Security.Cryptography.ProtectedData]::Unprotect($b, $null, 'CurrentUser')", "copy settings.dat x"):
            with self.assertRaises(actions.ActionError, msg=cmd):
                actions.run_command({"command": cmd})


class Readings(unittest.TestCase):
    """CPU, disk and per-program numbers come from Task Manager's own counters, measured fresh each time."""

    def test_a_fresh_reading_covers_the_last_moment_not_one_instant(self) -> None:
        import time

        from jarvis_agent.perf import Meter

        m = Meter(processes=True)
        time.sleep(2)  # an old span that a fresh reading must not include
        r = m.fresh(0.5)
        self.assertLess(r["span_s"], 1.0)
        self.assertTrue(0 <= r["cpu_percent"] <= 100 and 0 <= r["disk_active_percent"] <= 100, r)
        top = m.processes()
        self.assertTrue(top["top_memory"] and all(p["name"] not in ("_Total", "Idle") for p in top["top_cpu"]), top)

    def test_system_status_says_when_it_measured(self) -> None:
        s = actions.system_status({})
        self.assertIn("measured_at", s)
        self.assertIsNotNone(s["disk_active_percent"])
        self.assertNotIn("System Idle Process", [p["name"] for p in s["top_cpu"]])


class GoogleMaps(unittest.TestCase):
    def test_a_result_card_becomes_name_rating_reviews_and_details(self) -> None:
        from jarvis_agent import maps

        card = {"name": "Mind Space", "url": "https://www.google.com/maps/place/Mind+Space",
                "text": "Mind Space · 4.3(233) · Mind Space · Cafe · ₹200–400 · Closed · Opens 8 am Wed"}
        p = maps._place(card)
        self.assertEqual((p["rating"], p["reviews"]), (4.3, "233"))
        self.assertNotIn("Mind Space", p["details"])
        self.assertTrue(p["details"].startswith("Cafe"))
        self.assertIsNone(maps._place({"name": "New place", "text": "New place · Cafe"})["rating"])


if __name__ == "__main__":
    unittest.main()
