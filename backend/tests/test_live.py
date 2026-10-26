"""Live voice: a connection Google drops mid-reply is reopened and the conversation carries on."""
from __future__ import annotations

import asyncio
import sys
import types as pytypes
import unittest
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from google.genai import types  # noqa: E402

from jarvis_backend import live  # noqa: E402


def content(**kw) -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(**kw))


class FakeSession:
    def __init__(self, script: list, fail: Exception | None) -> None:
        self.script, self.fail, self.texts = script, fail, []

    async def send_realtime_input(self, audio=None, text=None) -> None:
        if text:
            self.texts.append(text)

    async def send_tool_response(self, **_) -> None:
        pass

    async def receive(self):
        for m in self.script:
            await asyncio.sleep(0.01)
            yield m
        if self.fail:
            raise self.fail
        await asyncio.sleep(3600)


class FakeConnect:
    def __init__(self, sessions: list[FakeSession]) -> None:
        self.sessions, self.configs = sessions, []

    def __call__(self, model, config):
        self.configs.append(config)
        session = self.sessions.pop(0)

        class CM:
            async def __aenter__(self_inner):
                return session

            async def __aexit__(self_inner, *a):
                return None

        return CM()


class Reconnect(unittest.IsolatedAsyncioTestCase):
    async def test_drop_mid_reply_reconnects_and_finishes(self) -> None:
        first = FakeSession([
            types.LiveServerMessage(session_resumption_update=types.LiveServerSessionResumptionUpdate(new_handle="h1", resumable=True)),
            content(output_transcription=types.Transcription(text="Good evening, sir. The system")),
        ], fail=RuntimeError("1006 None. abnormal closure"))
        second = FakeSession([
            content(output_transcription=types.Transcription(text=" is healthy.")),
            content(turn_complete=True),
        ], fail=None)
        connect = FakeConnect([first, second])
        client = pytypes.SimpleNamespace(aio=pytypes.SimpleNamespace(live=pytypes.SimpleNamespace(connect=connect)),
                                         models=pytypes.SimpleNamespace(list=lambda: []))
        events, saved = [], []

        async def push(_):
            pass

        async def call(*a, **k):
            return {}

        v = live.LiveVoice(api_key=lambda: "k", push_agent=push, call_agent=call, emit=events.append,
                           system_prompt=lambda: "", tools=[], run_tool=None,
                           save_turn=lambda c, u, r, label=None: saved.append(r) or "conv")
        with mock.patch("google.genai.Client", return_value=client), mock.patch.object(live, "IDLE_S", 0.6):
            await v.start("button")
            await asyncio.wait_for(v._task, 10)

        self.assertEqual(saved, ["Good evening, sir. The system ...", "is healthy."])
        self.assertEqual(connect.configs[1].session_resumption.handle, "h1")  # resumed, not a fresh conversation
        self.assertIn("Finish what you were saying", second.texts[0])
        names = [e["event"] for e in events]
        self.assertEqual(names.count("live_connecting"), 2)
        self.assertNotIn("error", names)

    async def test_stall_mid_reply_reconnects_without_waiting_for_google(self) -> None:
        stalled = FakeSession([content(output_transcription=types.Transcription(text="Your memory is"))], fail=None)  # then silence
        second = FakeSession([content(turn_complete=True)], fail=None)
        connect = FakeConnect([stalled, second])
        client = pytypes.SimpleNamespace(aio=pytypes.SimpleNamespace(live=pytypes.SimpleNamespace(connect=connect)),
                                         models=pytypes.SimpleNamespace(list=lambda: []))

        async def noop(*a, **k):
            return {}

        saved = []
        v = live.LiveVoice(api_key=lambda: "k", push_agent=noop, call_agent=noop, emit=lambda e: None,
                           system_prompt=lambda: "", tools=[], run_tool=None,
                           save_turn=lambda c, u, r, label=None: saved.append(r) or "conv")
        with mock.patch("google.genai.Client", return_value=client), mock.patch.object(live, "IDLE_S", 0.6), \
                mock.patch.object(live, "STALL_S", 0.5):
            await v.start("button")
            await asyncio.wait_for(v._task, 10)
        self.assertEqual(len(connect.configs), 2)
        self.assertEqual(saved, ["Your memory is ..."])


if __name__ == "__main__":
    unittest.main()
