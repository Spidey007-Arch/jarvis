"""Full control: the owner's PC acts without asking except for destructive steps; anyone else's install asks for every change."""
import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend import public_apis  # noqa: E402
from jarvis_backend.api import SPOKEN_NO, SPOKEN_YES, needs_approval  # noqa: E402
from jarvis_backend.tools import BY_NAME  # noqa: E402

RUN = BY_NAME["run_command"]


class FullControl(unittest.TestCase):
    def test_without_full_control_every_change_asks(self) -> None:
        for name, args in (("run_command", {"command": "git status"}), ("write_file", {"path": "a.txt", "content": "x"}),
                           ("edit_file", {"path": "a.txt"}), ("kill_process", {"pid": 5}), ("delete_path", {"path": "x"})):
            self.assertTrue(needs_approval(BY_NAME[name], args), name)
        self.assertFalse(needs_approval(BY_NAME["read_file"], {"path": "a.txt"}))
        self.assertFalse(needs_approval(BY_NAME["list_processes"], {}))

    def test_full_control_runs_ordinary_work(self) -> None:
        for cmd in ("git status", "npm run build", "python -m pytest", "winget install Spotify.Spotify", r"Get-ChildItem C:\Users"):
            self.assertFalse(needs_approval(RUN, {"command": cmd}, full_control=True), cmd)
        self.assertFalse(needs_approval(BY_NAME["write_file"], {"path": "a.txt", "content": "x"}, full_control=True))
        self.assertFalse(needs_approval(BY_NAME["kill_process"], {"pid": 5}, full_control=True))

    def test_destructive_steps_always_ask(self) -> None:
        for cmd in ("Remove-Item -Recurse D:\old", "rm -r build", "del notes.txt", "winget uninstall Zoom", "git push --force",
                    "git reset --hard HEAD~3", "Stop-Process -Name chrome", "taskkill /IM chrome.exe", "reg delete HKCU\Software\X"):
            self.assertTrue(needs_approval(RUN, {"command": cmd}, full_control=True), cmd)
        self.assertTrue(needs_approval(BY_NAME["power"], {"action": "restart"}, full_control=True))
        self.assertTrue(needs_approval(BY_NAME["screen_click"], {"target": "the Send button"}, full_control=True))

    def test_spoken_answers(self) -> None:
        for yes in ("yes", "yeah go ahead", "haan do it", "okay"):
            self.assertTrue(SPOKEN_YES.search(yes) and not SPOKEN_NO.search(yes), yes)
        for no in ("no", "wait", "don't", "cancel that", "nahi"):
            self.assertTrue(SPOKEN_NO.search(no), no)


class ApiSource(unittest.TestCase):
    def test_label_names_the_api_not_the_link(self) -> None:
        self.assertIn("CoinGecko", public_apis.source_of("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin"))
        self.assertEqual(public_apis.source_of("https://nowhere.invalid/x"), "nowhere.invalid")


if __name__ == "__main__":
    unittest.main()
