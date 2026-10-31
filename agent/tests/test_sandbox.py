"""The sandbox runs ordinary analysis on copies of the given files, keeps what it writes to out/, and refuses the
network, starting programs, and files outside its folder."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_agent import actions  # noqa: E402


class Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.home = mock.patch("pathlib.Path.home", lambda: Path(self.dir.name))
        self.home.start()
        self.csv = Path(self.dir.name) / "sales.csv"
        self.csv.write_text("region,amount\nPune,120\nMumbai,95\n")

    def tearDown(self) -> None:
        self.home.stop()
        self.dir.cleanup()

    def run_code(self, code: str, **kw) -> dict:
        return actions.run("run_sandboxed", {"code": code, "files": [str(self.csv)], "timeout": 60, **kw})

    def test_analysis_runs_on_a_copy_and_keeps_out_files(self) -> None:
        out = self.run_code("import csv\nrows = list(csv.DictReader(open('sales.csv')))\n"
                            "total = sum(int(r['amount']) for r in rows)\nprint('total', total)\n"
                            "open('out/total.txt', 'w').write(str(total))")
        self.assertEqual(out["exit_code"], 0, out["errors"])
        self.assertIn("total 215", out["output"])
        self.assertEqual(Path(out["files_made"][0]).read_text(), "215")

    def test_dangerous_things_are_refused(self) -> None:
        secret = Path(self.dir.name) / "secret.txt"
        secret.write_text("private")
        for code in ("import socket; socket.create_connection(('example.com', 80))",
                     "import urllib.request; urllib.request.urlopen('http://example.com')",
                     "import subprocess; subprocess.run(['cmd', '/c', 'echo hi'])",
                     "import os; os.system('echo hi')",
                     "import ctypes; ctypes.windll.user32.MessageBoxW(0, 'hi', 'hi', 0)",
                     f"print(open(r'{secret}').read())",
                     f"open(r'{Path(self.dir.name) / 'planted.txt'}', 'w').write('x')"):
            out = self.run_code(code)
            self.assertNotEqual(out["exit_code"], 0, code)
            self.assertIn("PermissionError", out["errors"], code)
        self.assertFalse((Path(self.dir.name) / "planted.txt").exists())

    def test_windows_api_and_planted_native_modules_are_refused(self) -> None:
        planted = Path(self.dir.name) / "via_winapi.txt"
        for code in (f"import _winapi; _winapi.CreateFile(r'{planted}', 0x40000000, 0, 0, 2, 0, 0)",  # GENERIC_WRITE, CREATE_ALWAYS
                     "import _winapi, os; _winapi.OpenProcess(0x1000, False, os.getppid())",
                     "import shutil, sys, _decimal; shutil.copy(_decimal.__file__, 'evil.pyd'); sys.path.insert(0, '.'); import evil"):
            out = self.run_code(code)
            self.assertNotEqual(out["exit_code"], 0, code)
            self.assertIn("PermissionError", out["errors"], code)
        self.assertFalse(planted.exists())

    def test_time_limit(self) -> None:
        out = self.run_code("while True: pass", timeout=5)
        self.assertTrue(out["timed_out"])


if __name__ == "__main__":
    unittest.main()
