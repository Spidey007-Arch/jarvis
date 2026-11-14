"""Bugs the full test run on 14 November 2026 found (TEST-REPORT.md), each pinned down so it stays fixed."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "common")]

from jarvis_backend import research  # noqa: E402
from jarvis_backend.api import LOOK_LIMIT, call_key, repeat_of  # noqa: E402
from jarvis_backend.apps import Apps  # noqa: E402
from jarvis_backend.persona import _CHARACTER  # noqa: E402
from jarvis_backend.providers.base import ToolCall  # noqa: E402
from jarvis_backend.settings import SettingsStore  # noqa: E402
from jarvis_backend.tools import BY_NAME, describe_call  # noqa: E402


def call(name: str, **args) -> ToolCall:
    return ToolCall(id="x", name=name, args=args)


class FoundBugs(unittest.TestCase):
    def test_a_workflow_in_chat_no_longer_breaks_the_label(self) -> None:  # test 48: the reply crashed
        label = describe_call(BY_NAME["create_workflow"], {"name": "Test flow", "steps": [{"kind": "ask", "text": "time?"}, {"kind": "say"}]})
        self.assertEqual(label, "Save workflow “Test flow”")
        self.assertEqual(describe_call(BY_NAME["search_everything"], {"query": "x", "sources": ["memory", "calendar"]}), "Searching everything for x")

    def test_system_status_is_measured_once_whatever_the_arguments(self) -> None:  # test 3: 7 readings
        done = {call_key(call("system_status")): (1, 1, {"ok": True, "result": {"cpu": 28}})}
        again = repeat_of(call("system_status", detail=True), done, 2)
        self.assertTrue(again and again["repeated"])

    def test_the_same_look_stops_after_a_few_times(self) -> None:  # test 28: the reminder list 7 times
        key = call_key(call("list_reminders"))
        self.assertIsNone(repeat_of(call("list_reminders"), {key: (1, LOOK_LIMIT - 1, {"ok": True})}, 2))
        stop = repeat_of(call("list_reminders"), {key: (1, LOOK_LIMIT, {"ok": True})}, 3)
        self.assertIn("Don't look again", stop["note"])

    def test_typing_tells_the_ai_to_type_once_and_check(self) -> None:  # test 8: 12 x 7 = 588
        self.assertIn("'12*7=' once", BY_NAME["type_into"].description)
        self.assertIn("makes sense", BY_NAME["type_into"].description)

    def test_wikipedia_gets_a_reader_that_says_who_it_is(self) -> None:  # test 22: 403 from Wikipedia
        self.assertTrue("en.wikipedia.org".endswith(research.HONEST_HOSTS))
        self.assertIn("JARVIS", research.HONEST_UA)

    def test_obsidian_skips_program_folders(self) -> None:  # test 38: a README inside node_modules
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            for rel in ("Plans/roadmap.md", "node_modules/pkg/README.md", ".git/notes.md", "build/out.md"):
                (vault / rel).parent.mkdir(parents=True, exist_ok=True)
                (vault / rel).write_text("roadmap", encoding="utf-8")
            apps = Apps(SettingsStore(vault / "s.dat"))
            apps.save("obsidian", {"vault": str(vault)})
            found = asyncio.run(apps.list_items("obsidian", "roadmap"))
            self.assertEqual([x["id"] for x in found], ["Plans/roadmap.md"])

    def test_the_name_is_fixed(self) -> None:  # test 1: "I am KERNEL"
        self.assertIn("You are JARVIS (never any other name)", _CHARACTER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
