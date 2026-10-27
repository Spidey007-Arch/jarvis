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


class AgentLoop(unittest.TestCase):
    """The loop's own pieces: more_tools loads tools, effort is set per step, the advisor's note reaches the model."""

    def test_more_tools_effort_and_advisor(self) -> None:
        import asyncio

        from jarvis_backend.llm import LLM
        from jarvis_backend.providers.base import Step, ToolSession

        class Session(ToolSession):
            def __init__(self) -> None:
                self.n, self.added, self.efforts, self.results = 0, [], [], []

            async def step(self, model: str) -> Step:
                self.efforts.append(self.effort)
                self.n += 1
                if self.n == 1:
                    return Step(calls=[ToolCall(id="a", name="more_tools", args={"need": "gmail"})], thought="I need mail tools")
                if self.n == 2:
                    return Step(calls=[ToolCall(id="b", name="gmail_list", args={})])
                return Step(text="Two new mails, sir.")

            def add_results(self, results) -> None:
                self.results.append(results)

            def add_tools(self, tools) -> None:
                self.added += [t["name"] for t in tools]

        class FakeLLM(LLM):
            chat_model = "m"
            provider_id = "gemini"

        llm = object.__new__(FakeLLM)
        llm._cooldown, llm._failures, llm.usage = {}, {}, None
        session = Session()
        llm.provider = lambda: type("P", (), {"tool_session": lambda *a: session})()

        async def candidates():
            return ["m"]

        llm._chat_candidates = candidates
        thoughts: list[str] = []

        async def execute(call):
            return {"ok": False, "error": "Gmail is not signed in"}

        async def advise(rounds, results):
            return "Ask the user to sign in to Gmail in the JARVIS browser window." if not results[-1][1].get("ok", True) else None

        reply = asyncio.run(llm.run_agent("sys", [("user", "any mail?")], [], execute, effort=lambda: "high", on_thought=thoughts.append,
                                          advise=advise, find_tools=toolsearch.find))
        self.assertEqual(reply, "Two new mails, sir.")
        self.assertIn("gmail_list", session.added)
        self.assertEqual(set(session.efforts), {"high"})
        self.assertEqual(thoughts, ["I need mail tools"])
        self.assertIn("sign in to Gmail", session.results[1][-1][1]["advisor"])


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
