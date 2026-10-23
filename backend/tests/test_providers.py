"""Protocol tests for the OpenAI-compatible and Anthropic providers.

A local HTTP server speaks each API's wire format (models list, SSE streaming,
structured output, embeddings, errors). This checks our request/response
handling; it does not prove compatibility with every hosted service.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "common")]

from jarvis_backend.providers import anthropic as anth  # noqa: E402
from jarvis_backend.providers.base import ProviderError  # noqa: E402
from jarvis_backend.providers.openai_compat import OpenAICompatProvider  # noqa: E402

SEEN: list[dict] = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep test output clean
        pass

    def _json(self, code: int, obj, headers: dict | None = None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, events: list[str]):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for e in events:
            self.wfile.write(e.encode())
            self.wfile.flush()

    def do_GET(self):
        if self.path.startswith("/oa/models"):
            return self._json(200, {"data": [{"id": "gpt-4.1-mini"}, {"id": "gpt-5-mini"}, {"id": "text-embedding-3-small"}, {"id": "whisper-1"}]})
        if self.path.startswith("/an/models"):
            return self._json(200, {"data": [{"id": "claude-new-sonnet", "display_name": "Claude Sonnet"}, {"id": "claude-new-haiku"}]})
        self._json(404, {"error": {"message": "nope"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        if body.get("model") == "hangs-up":
            self.close_connection = True
            self.connection.close()  # disconnect without sending any response
            return
        if self.path == "/oa/chat/completions" and body.get("tools"):
            if not any(m["role"] == "tool" for m in body["messages"]):
                return self._json(200, {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "volume", "arguments": '{"action": "set", "level": 30}'}}]}}]})
            tool_msg = next(m for m in body["messages"] if m["role"] == "tool")
            return self._json(200, {"choices": [{"message": {"role": "assistant", "content": f"Volume set. ({tool_msg['tool_call_id']})"}}]})
        if self.path == "/an/messages" and body.get("tools"):
            last = body["messages"][-1]
            if isinstance(last["content"], list) and last["content"][0].get("type") == "tool_result":
                return self._json(200, {"content": [{"type": "text", "text": f"Done ({last['content'][0]['tool_use_id']})."}]})
            return self._json(200, {"content": [{"type": "text", "text": "Checking."},
                                                {"type": "tool_use", "id": "toolu_1", "name": "system_status", "input": {}}]})
        if self.path == "/oa/chat/completions":
            if body["model"] == "busy-model":
                return self._json(429, {"error": {"message": "Rate limit reached"}}, {"retry-after": "7"})
            if body.get("stream"):
                chunks = [{"choices": [{"delta": {"content": t}}]} for t in ["Good ", "evening", ", sir."]]
                return self._sse([f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"])
            rf = body.get("response_format", {})
            if rf.get("type") == "json_schema":
                return self._json(400, {"error": {"message": "response_format json_schema is not supported by this model"}})
            return self._json(200, {"choices": [{"message": {"content": '```json\n{"operations": []}\n```'}}]})
        if self.path == "/oa/embeddings":
            data = [{"index": i, "embedding": [float(i), 1.0]} for i in range(len(body["input"]))]
            return self._json(200, {"data": list(reversed(data))})
        if self.path == "/an/messages":
            if self.headers.get("x-api-key") != "test-key":
                return self._json(401, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}})
            if body["model"] == "busy":
                return self._sse(['event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'])
            if body.get("stream"):
                ev = [{"type": "message_start"}] + [
                    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}} for t in ["At your ", "service."]
                ] + [{"type": "message_stop"}]
                return self._sse([f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in ev])
            return self._json(200, {"content": [{"type": "text", "text": 'Here you go: {"operations": [{"op": "create"}]}'}]})
        self._json(404, {"error": {"message": "nope"}})


class ProviderProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        SEEN.clear()

    def run_async(self, coro):
        return asyncio.run(coro)

    # ---------- OpenAI-compatible ----------

    def oa(self, key="sk-test"):
        return OpenAICompatProvider("openai", f"{self.base}/oa", key)

    def test_openai_models_classified_and_defaults(self):
        async def go():
            p = self.oa()
            try:
                return await p.list_models()
            finally:
                await p.aclose()
        models = self.run_async(go())
        kinds = {m["name"]: m["kind"] for m in models}
        self.assertEqual(kinds, {"gpt-4.1-mini": "chat", "gpt-5-mini": "chat", "text-embedding-3-small": "embed"})
        self.assertEqual(OpenAICompatProvider.pick_chat(models), "gpt-5-mini")
        self.assertEqual(OpenAICompatProvider.pick_embed(models), "text-embedding-3-small")

    def test_gateway_auto_model_preferred(self):
        models = [{"name": n, "kind": "chat"} for n in ("gpt-5-mini", "auto", "claude-x")]
        self.assertEqual(OpenAICompatProvider.pick_chat(models), "auto")
        omni = [{"name": n, "kind": "chat"} for n in ("auto/best-chaos", "auto/best-chat", "auto/fast", "ddgw/gpt-5.4-mini")]
        self.assertEqual(OpenAICompatProvider.pick_chat(omni), "auto/best-chat")

    def test_omniroute_preset_uses_local_gateway(self):
        from jarvis_backend.providers import PRESETS, make_provider
        p = make_provider("omniroute", "", "")
        try:
            self.assertEqual(p.base, "http://127.0.0.1:20128/v1")
            self.assertFalse(PRESETS["omniroute"]["needs_key"])
        finally:
            asyncio.run(p.aclose())

    def test_openai_stream_maps_roles_and_auth(self):
        async def go():
            p = self.oa()
            try:
                return [t async for t in p.stream_chat("gpt-5-mini", "be JARVIS", [("user", "a"), ("user", "b"), ("model", "c"), ("user", "d")])]
            finally:
                await p.aclose()
        parts = self.run_async(go())
        self.assertEqual("".join(parts), "Good evening, sir.")
        sent = SEEN[0]
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual([m["role"] for m in sent["body"]["messages"]], ["system", "user", "assistant", "user"])
        self.assertEqual(sent["body"]["messages"][1]["content"], "a\n\nb")

    def test_openai_json_falls_back_when_schema_unsupported(self):
        async def go():
            p = self.oa()
            try:
                return await p.generate_json("gpt-5-mini", "extract", {"type": "object"})
            finally:
                await p.aclose()
        self.assertEqual(self.run_async(go()), {"operations": []})
        self.assertEqual([s["body"].get("response_format", {}).get("type") for s in SEEN], ["json_schema", "json_object"])

    def test_openai_rate_limit_is_transient_with_retry_after(self):
        async def go():
            p = self.oa()
            try:
                return [t async for t in p.stream_chat("busy-model", "", [("user", "hi")])]
            finally:
                await p.aclose()
        with self.assertRaises(ProviderError) as cm:
            self.run_async(go())
        self.assertEqual((cm.exception.status, cm.exception.retry_after, cm.exception.transient), (429, 7.0, True))
        self.assertIn("Rate limit", str(cm.exception))

    def test_openai_embeddings_reordered_by_index(self):
        async def go():
            p = self.oa()
            try:
                return await p.embed("text-embedding-3-small", ["x", "y", "z"], "RETRIEVAL_DOCUMENT")
            finally:
                await p.aclose()
        self.assertEqual(self.run_async(go()), [[0.0, 1.0], [1.0, 1.0], [2.0, 1.0]])

    def test_local_server_without_key_sends_no_auth(self):
        async def go():
            p = OpenAICompatProvider("ollama", f"{self.base}/oa", "")
            try:
                return [t async for t in p.stream_chat("llama", "", [("user", "hi")])]
            finally:
                await p.aclose()
        self.run_async(go())
        self.assertNotIn("Authorization", SEEN[0]["headers"])

    def test_dropped_connection_is_retryable(self):
        async def go():
            p = self.oa()
            try:
                return [t async for t in p.stream_chat("hangs-up", "", [("user", "hi")])]
            finally:
                await p.aclose()
        with self.assertRaises(ProviderError) as cm:
            self.run_async(go())
        self.assertTrue(cm.exception.transient, str(cm.exception))

    def test_gemini_network_error_is_retryable(self):
        import httpcore
        import httpx
        from jarvis_backend.providers.gemini import _err
        # The SDK raises either the httpx or the raw httpcore flavour of this error.
        for exc in (httpx.RemoteProtocolError("Server disconnected without sending a response."),
                    httpcore.RemoteProtocolError("Server disconnected without sending a response.")):
            e = _err(exc)
            self.assertTrue(e.transient, type(exc))
            self.assertIn("connection problem", str(e))

    def test_unreachable_server_error_is_clear(self):
        async def go():
            p = OpenAICompatProvider("custom", "http://127.0.0.1:9/v1", "")
            try:
                return await p.list_models()
            finally:
                await p.aclose()
        with self.assertRaises(ProviderError) as cm:
            self.run_async(go())
        self.assertIn("Could not reach", str(cm.exception))

    # ---------- Anthropic ----------

    def an(self, key="test-key"):
        anth.API = f"{self.base}/an"
        return anth.AnthropicProvider(key)

    def test_anthropic_models_and_picks(self):
        async def go():
            p = self.an()
            try:
                return await p.list_models()
            finally:
                await p.aclose()
        models = self.run_async(go())
        self.assertEqual(anth.AnthropicProvider.pick_chat(models), "claude-new-sonnet")
        self.assertEqual(anth.AnthropicProvider.pick_utility(models, "claude-new-sonnet"), "claude-new-haiku")
        self.assertEqual(anth.AnthropicProvider.pick_embed(models), "")

    def test_anthropic_stream_system_and_alternation(self):
        async def go():
            p = self.an()
            try:
                return [t async for t in p.stream_chat("claude-new-sonnet", "be JARVIS", [("model", "orphan"), ("user", "hello")])]
            finally:
                await p.aclose()
        self.assertEqual("".join(self.run_async(go())), "At your service.")
        sent = SEEN[0]
        self.assertEqual(sent["body"]["system"], "be JARVIS")
        self.assertEqual(sent["body"]["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(sent["headers"]["anthropic-version"], anth.VERSION)

    def test_anthropic_overload_in_stream_is_transient(self):
        async def go():
            p = self.an()
            try:
                return [t async for t in p.stream_chat("busy", "", [("user", "hi")])]
            finally:
                await p.aclose()
        with self.assertRaises(ProviderError) as cm:
            self.run_async(go())
        self.assertTrue(cm.exception.transient)

    def test_anthropic_bad_key_reports_reason(self):
        async def go():
            p = self.an("wrong")
            try:
                return [t async for t in p.stream_chat("claude-new-sonnet", "", [("user", "hi")])]
            finally:
                await p.aclose()
        with self.assertRaises(ProviderError) as cm:
            self.run_async(go())
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("invalid x-api-key", str(cm.exception))

    def test_anthropic_json_from_prose(self):
        async def go():
            p = self.an()
            try:
                return await p.generate_json("claude-new-haiku", "extract", {"type": "object"})
            finally:
                await p.aclose()
        self.assertEqual(self.run_async(go()), {"operations": [{"op": "create"}]})


    # ---------- tool calling ----------

    TOOLS = [{"name": "volume", "description": "v", "parameters": {"type": "object", "properties": {}}},
             {"name": "system_status", "description": "s", "parameters": {"type": "object", "properties": {}}}]

    def test_openai_tool_round_trip(self):
        async def go():
            p = self.oa()
            try:
                s = p.tool_session("sys", [("user", "set volume to 30")], self.TOOLS)
                first = await s.step("gpt-5-mini")
                s.add_results([(first.calls[0], {"ok": True, "result": {"volume_now": 30}})])
                return first, await s.step("gpt-5-mini")
            finally:
                await p.aclose()
        first, final = self.run_async(go())
        self.assertEqual((first.calls[0].name, first.calls[0].args), ("volume", {"action": "set", "level": 30}))
        self.assertEqual(final.text, "Volume set. (call_1)")
        self.assertEqual(SEEN[0]["body"]["tools"][0]["type"], "function")
        self.assertEqual([m["role"] for m in SEEN[1]["body"]["messages"]], ["system", "user", "assistant", "tool"])

    def test_anthropic_tool_round_trip(self):
        async def go():
            p = self.an()
            try:
                s = p.tool_session("sys", [("user", "how's the system?")], self.TOOLS)
                first = await s.step("claude-new-sonnet")
                s.add_results([(first.calls[0], {"ok": False, "error": "agent offline"})])
                return first, await s.step("claude-new-sonnet")
            finally:
                await p.aclose()
        first, final = self.run_async(go())
        self.assertEqual((first.calls[0].id, first.calls[0].name), ("toolu_1", "system_status"))
        self.assertEqual(final.text, "Done (toolu_1).")
        result_block = SEEN[1]["body"]["messages"][-1]["content"][0]
        self.assertTrue(result_block["is_error"])
        self.assertIn("input_schema", SEEN[0]["body"]["tools"][0])


class ModelChoiceTests(unittest.TestCase):
    def test_gemini_fallbacks_end_on_a_lite_model(self) -> None:
        from jarvis_backend.providers.gemini import GeminiProvider

        names = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
        models = [{"name": n, "kind": "chat"} for n in names]
        self.assertEqual(GeminiProvider.fallbacks(models, "gemini-3.8-flash"),
                         ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"])

    def test_gateway_uses_gemini_rules_with_prefix(self) -> None:
        from jarvis_backend.providers.openai_compat import OpenAICompatProvider as P

        names = ["auto/best-chat", "gemini/gemini-3.8-flash", "gemini/gemini-3.7-flash", "gemini/gemini-3.5-flash-lite", "gemini/gemini-embedding-2"]
        models = [{"name": n, "kind": "embed" if "embed" in n else "chat"} for n in names]
        self.assertEqual(P.pick_chat(models), "gemini/gemini-3.8-flash")
        self.assertEqual(P.fallbacks(models, "gemini/gemini-3.8-flash"), ["gemini/gemini-3.7-flash", "gemini/gemini-3.5-flash-lite"])
        self.assertEqual(P.pick_utility(models, "gemini/gemini-3.8-flash"), "gemini/gemini-3.5-flash-lite")
        self.assertEqual(P.pick_embed(models), "gemini/gemini-embedding-2")
        # Without Gemini models a gateway still prefers its own router.
        self.assertEqual(P.pick_chat([{"name": "auto/best-chat", "kind": "chat"}, {"name": "x/y", "kind": "chat"}]), "auto/best-chat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
