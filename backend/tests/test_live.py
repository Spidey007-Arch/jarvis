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


def audio(n: int) -> types.LiveServerMessage:
    return content(model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=bytes(n), mime_type="audio/pcm"))]))


class SlowLine(unittest.IsolatedAsyncioTestCase):
    async def run_reply(self, chunk: int) -> tuple[list, int]:
        """A reply whose audio arrives in chunk-byte pieces every ~10 ms; returns on_slow calls and connections made."""
        script = [content(input_transcription=types.Transcription(text="what's the weather")),
                  *[audio(chunk) for _ in range(60)], content(turn_complete=True)]
        connect = FakeConnect([FakeSession(script, fail=None), FakeSession([], fail=None)])
        client = pytypes.SimpleNamespace(aio=pytypes.SimpleNamespace(live=pytypes.SimpleNamespace(connect=connect)),
                                         models=pytypes.SimpleNamespace(list=lambda: []))

        async def noop(*a, **k):
            return {}

        calls = []
        v = live.LiveVoice(api_key=lambda: "k", push_agent=noop, call_agent=noop, emit=lambda e: None,
                           system_prompt=lambda: "", tools=[], run_tool=None, save_turn=lambda c, u, r, label=None: "conv",
                           on_slow=lambda *a: calls.append(a))
        with mock.patch("google.genai.Client", return_value=client), mock.patch.object(live, "IDLE_S", 0.6), \
                mock.patch.object(live, "SLOW_CHECK_S", 0.2):
            await v.start("button")
            await asyncio.wait_for(v._task, 10)
        return calls, len(connect.configs)

    async def test_audio_slower_than_real_time_hands_over_to_offline(self) -> None:
        calls, connections = await self.run_reply(100)  # ~10 KB/s, playing needs 48
        self.assertEqual(len(calls), 1)
        reason, user, acted, origin = calls[0]
        self.assertEqual((user, acted, origin), ("what's the weather", False, "button"))
        self.assertIn("KB/s", reason)
        self.assertEqual(connections, 1)  # no point reconnecting on the same slow line

    async def test_a_fast_line_stays_live(self) -> None:
        calls, _ = await self.run_reply(4000)
        self.assertEqual(calls, [])


class Continuity(unittest.IsolatedAsyncioTestCase):
    async def run_session(self, script, label=None, recent="conv-earlier", during=None):
        session = FakeSession(script, fail=None)
        connect = FakeConnect([session])
        client = pytypes.SimpleNamespace(aio=pytypes.SimpleNamespace(live=pytypes.SimpleNamespace(connect=connect)),
                                         models=pytypes.SimpleNamespace(list=lambda: []))
        pushed, saved, asked = [], [], []

        async def push(m):
            pushed.append(m["type"])

        async def call(*a, **k):
            return {}

        def recent_conversation(age):
            asked.append(age)
            return recent

        v = live.LiveVoice(api_key=lambda: "k", push_agent=push, call_agent=call, emit=lambda e: None,
                           system_prompt=lambda: "", tools=[], run_tool=None,
                           save_turn=lambda c, u, r, label=None: saved.append((c, u, r)) or (c or "conv-new"),
                           recent_conversation=recent_conversation)
        with mock.patch("google.genai.Client", return_value=client), mock.patch.object(live, "IDLE_S", 0.6):
            await v.start("wake", label=label)
            if during:
                await during(v)
            await asyncio.wait_for(v._task, 10)
        return v, saved, pushed, connect

    async def test_a_new_session_continues_the_recent_conversation(self) -> None:
        v, saved, _, connect = await self.run_session([
            content(input_transcription=types.Transcription(text="And tomorrow?")),
            content(output_transcription=types.Transcription(text="Rain, sir.")),
            content(turn_complete=True),
        ])
        self.assertEqual(saved, [("conv-earlier", "And tomorrow?", "Rain, sir.")])
        self.assertEqual(v.conversation_id, "conv-earlier")
        cfg = connect.configs[0]
        self.assertEqual(cfg.realtime_input_config.activity_handling, types.ActivityHandling.NO_INTERRUPTION)

    async def test_the_briefing_starts_its_own(self) -> None:
        _, saved, _, _ = await self.run_session([content(output_transcription=types.Transcription(text="Good evening.")),
                                                 content(turn_complete=True)], label="Start-up briefing")
        self.assertEqual(saved[0][0], None)

    async def test_hey_jarvis_drops_the_rest_of_the_reply(self) -> None:
        audio = types.LiveServerMessage(server_content=types.LiveServerContent(
            model_turn=types.Content(parts=[types.Part(inline_data=types.Blob(data=b"\0" * 480, mime_type="audio/pcm"))])))

        async def barge_early(v):
            await asyncio.sleep(0.005)
            await v.barge()

        _, saved, pushed, _ = await self.run_session(
            [content(output_transcription=types.Transcription(text="Let me explain at length"))] + [audio] * 5 + [content(turn_complete=True)],
            during=barge_early)
        self.assertNotIn("speaker", pushed)
        self.assertIn("speaker_flush", pushed)
        self.assertEqual(saved[0][2], "Let me explain at length ...")


if __name__ == "__main__":
    unittest.main()
