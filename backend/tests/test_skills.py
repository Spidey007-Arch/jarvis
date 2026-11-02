"""Learned workflows: saved after a multi-step success, offered for a similar request, scored by how reuse
goes, and no longer offered once unreliable."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend.db import DB  # noqa: E402
from jarvis_backend.skills import Skills, guidance  # noqa: E402

STEPS = [
    {"tool": "open_app", "label": "Opening Spotify", "status": "done", "args": {"name": "spotify"}},
    {"tool": "click_text", "label": "Pressing \"Search\"", "status": "done", "args": {"text": "Search"}},
    {"tool": "type_text", "label": "Typing text", "status": "done", "args": {"text": "lofi"}},
    {"tool": "press_keys", "label": "Pressing enter", "status": "done", "args": {"keys": ["enter"]}},
]


class Workflows(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dir.name) / "t.db")
        self.skills = Skills(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def test_learned_and_offered_for_a_similar_request(self) -> None:
        sid = self.skills.learn("play lofi music on spotify", STEPS, None)
        self.assertIsNotNone(sid)
        hit = self.skills.match("play some lofi on spotify please")
        self.assertEqual(hit["id"], sid)
        self.assertIn("Pressing \"Search\"", guidance(hit))
        self.assertIsNone(self.skills.match("what's the weather in Pune"))

    def test_short_or_failed_requests_are_not_learned(self) -> None:
        self.assertIsNone(self.skills.learn("open spotify", STEPS[:1], None))
        bad = STEPS[:3] + [{"tool": "press_keys", "label": "x", "status": "failed", "args": {}}]
        self.assertIsNone(self.skills.learn("play lofi on spotify", bad, None))

    def test_unreliable_workflows_stop_being_offered(self) -> None:
        sid = self.skills.learn("play lofi music on spotify", STEPS, None)
        failed = [{"tool": "open_app", "label": "x", "status": "failed", "args": {}}]
        for _ in range(3):
            self.skills.learn("play lofi music on spotify", failed, sid)
        self.assertIsNone(self.skills.match("play lofi music on spotify"))


class Focus(unittest.TestCase):
    def test_what_is_in_focus_reaches_the_prompt(self) -> None:
        from jarvis_backend.persona import build_system_prompt

        ctx = {"focus": {"front": {"app": "AcroRd32", "title": "report.pdf - Adobe Reader"},
                         "files": [r"C:\Users\x\Downloads\report.pdf (open_path, 2 min ago)"], "downloads": []}}
        p = build_system_prompt({"address": "sir"}, [], [], tools_enabled=True, context=ctx)
        self.assertIn("report.pdf - Adobe Reader", p)
        self.assertIn("Files you worked with just now", p)
        self.assertIn("ask which one", p)



class MySkills(unittest.TestCase):
    """Customize > Skills: skills the user writes, followed when a request names or fits them."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dir.name) / "t.db")
        self.skills = Skills(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def test_write_match_edit_and_remove(self) -> None:
        s = self.skills.save_mine("Morning setup", "start my work day, open work apps", "1. Open Outlook\n2. Open VS Code")
        self.assertEqual([m["name"] for m in self.skills.match_mine("start my work day please")], ["Morning setup"])
        self.assertEqual(len(self.skills.match_mine("use morning setup")), 1)  # named
        self.assertEqual(self.skills.match_mine("play some music"), [])
        self.skills.save_mine("Morning setup", "", "x", enabled=False, sid=s["id"])
        self.assertEqual(self.skills.match_mine("morning setup"), [])  # turned off
        with self.assertRaises(ValueError):
            self.skills.save_mine("morning SETUP", "", "y")  # names are unique
        with self.assertRaises(ValueError):
            self.skills.save_mine("Empty", "", "  ")
        self.assertTrue(self.skills.remove_mine(s["id"]))
        self.assertEqual(self.skills.mine(), [])

    def test_instructions_go_into_the_prompt(self) -> None:
        from jarvis_backend.persona import build_system_prompt
        from jarvis_backend.providers.base import DYNAMIC_MARK

        p = build_system_prompt({"instructions": "Always answer in Hinglish."}, [], [])
        self.assertIn("Always answer in Hinglish.", p.split(DYNAMIC_MARK)[0])  # in the stable, cached part
        self.assertNotIn("own instructions", build_system_prompt({}, [], []))


if __name__ == "__main__":
    unittest.main()
