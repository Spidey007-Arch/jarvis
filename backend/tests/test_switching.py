"""Automatic model switching: a model out of quota stays skipped until the reset (even after a restart), a model the
provider no longer offers is skipped instead of failing the reply, and the local gateway answers when every model
of the chosen provider is out."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend.db import DB  # noqa: E402
from jarvis_backend.llm import LLM  # noqa: E402
from jarvis_backend.providers.base import ProviderError, Step, ToolSession  # noqa: E402
from jarvis_backend.usage import QuotaTracker  # noqa: E402

MODELS = ["gemini-4-argon", "gemini-3.6-flash", "gemini-3.5-flash-lite"]


class Switching(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dir.name) / "t.db")
        self.usage = QuotaTracker(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def make(self, answers: dict[str, object], backup: tuple[object, str] | None = None) -> tuple[LLM, list[str], list[str]]:
        seen: list[str] = []

        class Session(ToolSession):
            portable = False

            async def step(self, model: str) -> Step:
                seen.append(model)
                a = answers[model]
                if isinstance(a, Exception):
                    raise a
                return Step(text=str(a), calls=[])

            def add_results(self, results) -> None:
                pass

        class FakeLLM(LLM):
            chat_model = MODELS[0]
            provider_id = "gemini"

        llm = object.__new__(FakeLLM)
        llm._cooldown, llm._failures, llm.usage, llm._told = {}, {}, self.usage, {}
        llm._backup, llm._backup_retry_at = None, 0.0
        llm.provider = lambda: type("P", (), {"tool_session": lambda *a: Session()})()

        async def candidates():
            return llm._available(list(MODELS))

        async def no_backup():
            return backup

        llm._chat_candidates = candidates
        llm.backup = no_backup
        notices: list[str] = []
        llm._notices = notices
        return llm, seen, notices

    def run_agent(self, llm: LLM) -> str:
        async def execute(call):
            return {"ok": True}

        return asyncio.run(llm.run_agent("sys", [("user", "hi")], [], execute, llm._notices.append))

    def test_daily_quota_is_skipped_until_reset_even_after_restart(self) -> None:
        out = ProviderError("Quota reached (limit 20 requests per day). The daily free quota resets at midnight Pacific time.", 429)
        llm, seen, notices = self.make({"gemini-4-argon": out, "gemini-3.6-flash": "Hello, sir.", "gemini-3.5-flash-lite": "x"})
        self.assertEqual(self.run_agent(llm), "Hello, sir.")
        self.assertEqual(seen, ["gemini-4-argon", "gemini-3.6-flash"])
        self.assertIn("used up today's quota", notices[0])

        # a fresh start (no memory of cooldowns) still skips it, from what the quota tracker remembers
        again, seen2, notices2 = self.make({"gemini-4-argon": out, "gemini-3.6-flash": "Again, sir.", "gemini-3.5-flash-lite": "x"})
        self.assertEqual(self.run_agent(again), "Again, sir.")
        self.assertEqual(seen2, ["gemini-3.6-flash"])
        self.assertGreater(self.usage.blocked_until("gemini-4-argon"), time.time() + 60)

    def test_switch_notice_is_not_repeated_every_reply(self) -> None:
        out = ProviderError("Quota reached (limit 20 requests per day).", 429)
        llm, _seen, notices = self.make({"gemini-4-argon": out, "gemini-3.6-flash": "One.", "gemini-3.5-flash-lite": "x"})
        self.run_agent(llm)
        self.run_agent(llm)
        self.assertEqual(len(notices), 1)

    def test_retired_model_is_skipped_not_fatal(self) -> None:
        gone = ProviderError("models/gemini-4-argon is not found for API version v1beta", 404)
        llm, seen, _n = self.make({"gemini-4-argon": gone, "gemini-3.6-flash": "Still here, sir.", "gemini-3.5-flash-lite": "x"})
        self.assertEqual(self.run_agent(llm), "Still here, sir.")
        self.assertEqual(llm._available(list(MODELS))[0], "gemini-3.6-flash")

    def test_other_errors_still_stop_the_reply(self) -> None:
        from jarvis_backend.llm import LLMError

        bad = ProviderError("API key not valid", 400)
        llm, seen, _n = self.make({"gemini-4-argon": bad, "gemini-3.6-flash": "x", "gemini-3.5-flash-lite": "x"})
        with self.assertRaises(LLMError):
            self.run_agent(llm)
        self.assertEqual(seen, ["gemini-4-argon"])

    def test_gateway_answers_when_every_model_is_out(self) -> None:
        busy = ProviderError("overloaded", 503)

        class GatewaySession(ToolSession):
            portable = False

            async def step(self, model: str) -> Step:
                return Step(text=f"From {model}, sir.", calls=[])

            def add_results(self, results) -> None:
                pass

        gw = type("G", (), {"tool_session": lambda *a: GatewaySession()})()
        llm, seen, notices = self.make({m: busy for m in MODELS}, backup=(gw, "auto"))
        self.assertEqual(self.run_agent(llm), "From auto, sir.")
        self.assertEqual(seen, MODELS)
        self.assertTrue(any("local gateway" in n for n in notices))


class GatewayStart(unittest.TestCase):
    def test_not_installed_says_so(self) -> None:
        from unittest import mock

        from jarvis_backend import gateway

        with mock.patch.object(gateway, "installed", return_value=None):
            r = asyncio.run(gateway.ensure("http://127.0.0.1:1/v1"))
        self.assertFalse(r["running"])
        self.assertIn("isn't installed", r["error"])


class LocationPermission(unittest.TestCase):
    def test_near_me_needs_the_location_permission_a_named_place_does_not(self) -> None:
        from jarvis_backend import permissions
        from jarvis_backend.tools import TOOLS

        spec = {t.name: t for t in TOOLS}
        self.assertEqual(permissions.capability(spec["my_location"], {}), "location")
        self.assertEqual(permissions.capability(spec["maps_search"], {"query": "cafes"}), "location")
        self.assertIsNone(permissions.capability(spec["maps_search"], {"query": "cafes", "near": "Panaji"}))
        self.assertEqual(permissions.capability(spec["maps_directions"], {"destination": "Goa airport"}), "location")


class Globe(unittest.TestCase):
    def test_a_view_link_in_an_answer_is_found_on_jarvis_address(self) -> None:
        from jarvis_backend import globe

        text = 'Open 30 km around 19.070, 72.870 in the globe: http://localhost:4173/#v=2&lat=19.07&lon=72.87 {"url": "x"}'
        self.assertEqual(globe.view_link(text), "http://127.0.0.1:4173/?embed=1#v=2&lat=19.07&lon=72.87")
        self.assertIsNone(globe.view_link("no link here, or http://example.com:4173/#v=2"))

    def test_missing_pieces_are_named_not_started(self) -> None:
        from unittest import mock

        from jarvis_backend import globe

        with mock.patch.object(globe, "state", return_value={"app": True, "packages": False, "node": True, "url": globe.URL}):
            r = asyncio.run(globe.ensure())
        self.assertFalse(r["running"])
        self.assertIn("packages", r["error"])


if __name__ == "__main__":
    unittest.main()
