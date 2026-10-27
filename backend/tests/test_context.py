"""Speed and cost: the changing context moves to the end so providers can cache the rest; long tasks shrink
old tool results; tool search sends only what a request needs; token use is counted per model."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend import toolsearch  # noqa: E402
from jarvis_backend.db import DB  # noqa: E402
from jarvis_backend.persona import build_system_prompt  # noqa: E402
from jarvis_backend.providers.anthropic import _AnthropicTools, _mark_last  # noqa: E402
from jarvis_backend.providers.base import DYNAMIC_MARK, ToolCall, split_system, stub, with_context  # noqa: E402
from jarvis_backend.usage import QuotaTracker  # noqa: E402


class Caching(unittest.TestCase):
    def test_changing_context_goes_last(self) -> None:
        p = build_system_prompt({"address": "sir"}, [], [], tools_enabled=True, context={"reminders": []})
        static, dynamic = split_system(p)
        self.assertIn("You are JARVIS", static)
        self.assertNotIn("Current local time", static)  # the time changes every minute: never in the cached part
        self.assertIn("Current local time", dynamic)
        turns = with_context([("user", "hi"), ("model", "hello"), ("user", "open spotify")], dynamic)
        self.assertEqual(turns[:2], [("user", "hi"), ("model", "hello")])  # earlier turns unchanged: cacheable
        self.assertTrue(turns[2][1].startswith("[Context right now") and turns[2][1].endswith("open spotify"))

    def test_claude_marks_a_cache_breakpoint_on_the_last_block(self) -> None:
        msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": [{"type": "text", "text": "b"}]}]
        marked = _mark_last(msgs)
        self.assertEqual(marked[-1]["content"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", msgs[-1]["content"][-1])  # the session's own history is left as it was


class LongTasks(unittest.TestCase):
    def test_old_tool_results_shrink_but_recent_ones_stay(self) -> None:
        s = _AnthropicTools(type("P", (), {"_messages": lambda self, h, d="": [{"role": "user", "content": "go"}]})(), "sys", [], [])
        big = {"ok": True, "result": "x" * 5000}
        for i in range(6):
            s.add_results([(ToolCall(id=f"c{i}", name="read_file", args={}), big)])
        self.assertEqual(s.trim_results(keep=4), 2)
        sizes = [len(b["content"]) for m in s.messages if isinstance(m["content"], list) for b in m["content"]]
        self.assertTrue(all(n < 400 for n in sizes[:2]) and all(n > 4000 for n in sizes[2:]))
        self.assertIn("cleared_to_save_space", stub(json.dumps(big)))


class ToolSearch(unittest.TestCase):
    def test_requests_get_core_plus_what_matches(self) -> None:
        names = [t["name"] for t in toolsearch.select("any important mail today?")]
        self.assertIn("gmail_list", names)
        self.assertIn("more_tools", names)
        self.assertNotIn("whatsapp_read", names)
        self.assertLess(len(names), 40)
        self.assertIn("set_alarm", [t["name"] for t in toolsearch.find("wake me up at 6")])


class Tokens(unittest.TestCase):
    def test_counted_per_model_with_cache_share(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d) / "t.db")
            q = QuotaTracker(db)
            q.record_tokens("gemini-x", {"input": 1000, "output": 50, "cached": 600})
            q.record_tokens("gemini-x", {"input": 1000, "output": 50, "cached": 900})
            snap = q.snapshot(["gemini-x"])
            self.assertEqual(snap["tokens"], {"input": 2000, "output": 100, "cached": 1500})
            self.assertEqual(snap["models"][0]["tokens_cached"], 1500)
            db.close()


if __name__ == "__main__":
    unittest.main()
