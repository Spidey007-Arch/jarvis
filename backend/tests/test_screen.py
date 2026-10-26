"""Clicking by sight: clicks that send, pay, delete or confirm need the user's Allow; ordinary clicks don't."""
import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend.api import LOCATE_PROMPT, needs_approval  # noqa: E402
from jarvis_backend.tools import BY_NAME  # noqa: E402

CLICK = BY_NAME["screen_click"]


class Approval(unittest.TestCase):
    def test_ordinary_clicks_just_happen(self) -> None:
        for target in ("the Search box in Spotify", "the chat named Mom", "File menu in Notepad", "the Play button", "the second video"):
            self.assertFalse(needs_approval(CLICK, {"target": target}), target)

    def test_consequential_clicks_ask_first(self) -> None:
        for target in ("the Send button", "Pay now", "Delete this email", "Place order", "Uninstall", "Confirm", "Sign out",
                       "Post the comment", "Empty trash", "Accept the terms"):
            self.assertTrue(needs_approval(CLICK, {"target": target}), target)

    def test_confirm_tools_and_dry_runs_unchanged(self) -> None:
        self.assertTrue(needs_approval(BY_NAME["delete_path"], {"path": "x"}))
        self.assertFalse(needs_approval(BY_NAME["organize_folder"], {"path": "x"}))
        self.assertTrue(needs_approval(BY_NAME["organize_folder"], {"path": "x", "dry_run": False}))
        self.assertFalse(needs_approval(BY_NAME["volume"], {"action": "up"}))

    def test_prompt_formats(self) -> None:
        self.assertIn('"the Send button"', LOCATE_PROMPT.format(target="the Send button"))


if __name__ == "__main__":
    unittest.main()
