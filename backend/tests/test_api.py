"""API tests against the real FastAPI app (in-process). The invalid-key test calls Google for real."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "common")]

_tmp = tempfile.TemporaryDirectory()
os.environ["JARVIS_DATA_DIR"] = _tmp.name
os.environ.pop("JARVIS_PARENT_PID", None)

from fastapi.testclient import TestClient  # noqa: E402

from jarvis_backend.main import create_app  # noqa: E402

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(create_app(port=0, token=TOKEN)).__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_requires_token(self):
        self.assertEqual(self.client.get("/api/settings").status_code, 401)
        self.assertEqual(self.client.get("/api/settings", headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_settings_roundtrip_and_encrypted(self):
        r = self.client.put("/api/settings", headers=AUTH, json={"user_name": "Tester", "address": "sir"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["user_name"], "Tester")
        raw = (Path(_tmp.name) / "settings.dat").read_bytes()
        self.assertNotIn(b"Tester", raw)

    def test_chat_without_key_says_so(self):
        r = self.client.post("/api/chat", headers=AUTH, json={"message": "hello"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("API key", r.json()["message"])

    def test_invalid_key_rejected_with_real_reason(self):
        r = self.client.put("/api/settings", headers=AUTH, json={"provider": "gemini", "api_key": "AIza-definitely-not-valid-000000"})
        self.assertEqual(r.status_code, 400, r.text)
        print("\n    Google said:", r.json()["detail"][:160])
        self.assertFalse(self.client.get("/api/settings", headers=AUTH).json()["has_api_key"])

    def test_providers_listed(self):
        ids = [p["id"] for p in self.client.get("/api/providers", headers=AUTH).json()]
        self.assertEqual(ids, ["gateway", "gemini", "openai", "anthropic", "openrouter", "ollama", "custom"])

    def test_switch_provider_without_key_asks_for_key(self):
        r = self.client.put("/api/settings", headers=AUTH, json={"provider": "anthropic"})
        self.assertEqual(r.json()["provider"], "anthropic")
        self.assertFalse(r.json()["has_api_key"])
        r = self.client.post("/api/chat", headers=AUTH, json={"message": "hi"})
        self.assertEqual(r.status_code, 409)
        self.client.put("/api/settings", headers=AUTH, json={"provider": "gemini"})

    def test_unreachable_local_server_reports_real_error(self):
        r = self.client.put("/api/settings", headers=AUTH, json={"provider": "custom", "base_url": "http://127.0.0.1:9/v1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Could not", r.json()["detail"])

    def test_memory_import_list_edit_delete_export(self):
        r = self.client.post("/api/memories/import", headers=AUTH, json={"memories": [
            {"type": "preference", "subject": "theme", "content": "Prefers dark themes.", "importance": 0.4}]})
        self.assertEqual(r.json(), {"added": 1, "skipped": 0})
        [m] = self.client.get("/api/memories", headers=AUTH).json()
        r = self.client.patch(f"/api/memories/{m['id']}", headers=AUTH, json={"content": "Prefers dark blue themes."})
        self.assertEqual(r.json()["content"], "Prefers dark blue themes.")
        self.assertEqual(self.client.patch(f"/api/memories/{m['id']}", headers=AUTH, json={"type": "bogus"}).status_code, 400)
        self.assertEqual(self.client.get("/api/memories/stats", headers=AUTH).json()["by_type"]["preference"], 1)
        exported = self.client.get("/api/memories/export", headers=AUTH).json()
        self.assertEqual(exported["memories"][0]["content"], "Prefers dark blue themes.")
        self.assertEqual(self.client.delete(f"/api/memories/{m['id']}", headers=AUTH).status_code, 200)
        self.assertEqual(self.client.get("/api/memories", headers=AUTH).json(), [])

    def test_automations_endpoints_and_a_headless_run(self):
        r = self.client.put("/api/automations", headers=AUTH, json={"name": "Disk check", "prompt": "how full is my disk?",
                                                                     "kind": "daily", "at": "09:00", "days": [0, 1, 2, 3, 4]})
        self.assertEqual(r.status_code, 200, r.text)
        a = r.json()
        self.assertEqual(a["schedule"], "weekdays at 09:00")
        self.assertEqual(self.client.put("/api/automations", headers=AUTH, json={"name": "x", "prompt": "y", "kind": "interval",
                                                                                 "every_min": 1}).status_code, 400)
        self.assertFalse(self.client.post(f"/api/automations/{a['id']}/enabled", headers=AUTH, json={"enabled": False}).json()["enabled"])
        self.assertEqual(self.client.post(f"/api/automations/{a['id']}/run", headers=AUTH).status_code, 200)
        import time as _t
        for _ in range(50):  # no AI is connected here, so the run goes through the chat path and fails, saying why
            got = next(x for x in self.client.get("/api/automations", headers=AUTH).json() if x["id"] == a["id"])
            if got["last_status"]:
                break
            _t.sleep(0.1)
        self.assertEqual(got["last_status"], "failed")
        self.assertIn("API key", got["last_result"])
        self.assertEqual(self.client.delete(f"/api/automations/{a['id']}", headers=AUTH).status_code, 200)

    def test_calendar_endpoints(self):
        r = self.client.post("/api/calendar/events", headers=AUTH, json={"title": "Dentist", "start": "2030-01-05T16:00", "remind_minutes": 30})
        self.assertEqual(r.status_code, 200, r.text)
        ev = r.json()
        self.assertEqual((ev["start"], ev["end"], ev["reminder"]), ("2030-01-05T16:00", "2030-01-05T17:00", "30 minutes before"))
        view = self.client.get("/api/calendar?start=2030-01-01&end=2030-01-31", headers=AUTH).json()
        self.assertEqual([e["title"] for e in view["events"]], ["Dentist"])
        bad = self.client.post("/api/calendar/events", headers=AUTH, json={"title": "x", "start": "2030-01-05T16:00", "end": "2030-01-05T15:00"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("after it starts", bad.json()["detail"])
        self.assertEqual(self.client.put("/api/calendar/linked", headers=AUTH, json={"name": "Work", "url": "http://not-https.example/x.ics"}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/calendar/events/{ev['id']}", headers=AUTH).status_code, 200)
        self.assertEqual(self.client.get("/api/calendar?start=2030-01-01&end=2030-01-31", headers=AUTH).json()["events"], [])

    def test_heartbeat_endpoints(self):
        self.assertEqual(self.client.post("/api/heartbeat/run", headers=AUTH).status_code, 400)  # nothing to check yet
        r = self.client.put("/api/heartbeat", headers=AUTH, json={"enabled": True, "every_min": 30, "checks": ["battery", " "],
                                                                   "quiet_from": "22:30", "quiet_to": "06:00"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["checks"], r.json()["quiet_from"]), (["battery"], "22:30"))
        self.assertEqual(self.client.put("/api/heartbeat", headers=AUTH, json={"every_min": 5}).status_code, 422)
        self.client.put("/api/heartbeat", headers=AUTH, json={"enabled": False, "checks": []})

    def test_plugin_from_a_file(self):
        plugin = {"format": "jarvis-plugin", "id": "api-demo", "name": "API demo",
                  "skills": [{"name": "API demo skill", "when_to": "demo", "body": "1. Say demo"}],
                  "connectors": [{"name": "demo-notes", "kind": "local", "command": "npx", "args": ["-y", "demo-mcp"], "env_names": ["DEMO_TOKEN"]}]}
        import json as _json
        p = self.client.post("/api/plugins/preview", headers=AUTH, json={"text": _json.dumps(plugin)}).json()
        self.assertEqual(p["commands"][0]["runs"], "npx -y demo-mcp")
        self.assertEqual(self.client.post("/api/plugins/install", headers=AUTH, json={"text": _json.dumps(plugin)}).status_code, 200)
        conns = {c["name"]: c for c in self.client.get("/api/connectors", headers=AUTH).json()}
        self.assertEqual((conns["demo-notes"]["enabled"], conns["demo-notes"]["needs_env"]), (False, ["DEMO_TOKEN"]))
        self.assertEqual(self.client.delete("/api/plugins/api-demo", headers=AUTH).status_code, 200)
        self.assertNotIn("demo-notes", [c["name"] for c in self.client.get("/api/connectors", headers=AUTH).json()])
        bad = {**plugin, "connectors": [{"name": "x", "kind": "local", "command": "cmd", "args": ["/c", "del"]}]}
        self.assertEqual(self.client.post("/api/plugins/preview", headers=AUTH, json={"text": _json.dumps(bad)}).status_code, 400)

    def test_backup_restore_and_sync_settings(self):
        import base64 as _b64
        from jarvis_backend import backup as _backup

        blob = _backup.encrypt({"device": {"name": "Laptop"}, "made_at": 1, "profile": {"instructions": "From the laptop."},
                                "memories": [{"type": "preference", "subject": "tea", "content": "Likes masala chai"}]}, "laptop passphrase")
        data = _b64.b64encode(blob).decode()
        self.assertEqual(self.client.post("/api/backup/restore", headers=AUTH, json={"data_b64": data, "passphrase": "wrong one!"}).status_code, 400)
        r = self.client.post("/api/backup/restore", headers=AUTH, json={"data_b64": data, "passphrase": "laptop passphrase"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["restored"]["memories"], r.json()["from"]), (1, "Laptop"))
        self.assertEqual(self.client.get("/api/settings", headers=AUTH).json()["instructions"], "From the laptop.")
        self.client.put("/api/settings", headers=AUTH, json={"instructions": ""})
        for m in self.client.get("/api/memories", headers=AUTH).json():
            self.client.delete(f"/api/memories/{m['id']}", headers=AUTH)
        r = self.client.put("/api/sync", headers=AUTH, json={"enabled": True, "folder": str(Path(_tmp.name) / "sync")})
        self.assertEqual(r.status_code, 400)
        self.assertIn("passphrase", r.json()["detail"])
        self.assertEqual(self.client.put("/api/sync", headers=AUTH, json={"enabled": False, "passphrase": "short"}).status_code, 400)

    def test_conversations_empty_and_404(self):
        self.assertEqual(self.client.get("/api/conversations", headers=AUTH).json(), [])
        self.assertEqual(self.client.get("/api/conversations/nope/messages", headers=AUTH).status_code, 404)


class RepeatGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        from jarvis_backend.api import call_key, repeat_of
        from jarvis_backend.providers.base import ToolCall

        self.repeat_of, self.call_key, self.Call = repeat_of, call_key, ToolCall

    def record(self, done, call, step, out, runs=1):
        done[self.call_key(call)] = (step, runs, out)

    def test_identical_action_is_not_run_twice(self) -> None:
        done: dict = {}
        c = self.Call("1", "open_url", {"url": "https://mail.google.com"})
        self.record(done, c, 1, {"ok": True, "result": {"opened": "x"}})
        again = self.repeat_of(self.Call("2", "open_url", {"url": "https://mail.google.com"}), done, 3)
        self.assertTrue(again and again["repeated"] and again["ok"])
        self.assertIsNone(self.repeat_of(self.Call("3", "open_url", {"url": "https://example.com"}), done, 3))

    def test_one_status_reading_per_request(self) -> None:
        """A fresh reading answers the question; measuring again and again in one request was a loop with no reply."""
        done: dict = {}
        c = self.Call("1", "system_status", {})
        self.record(done, c, 1, {"ok": True, "result": {"cpu_percent": 12.0}})
        again = self.repeat_of(self.Call("2", "system_status", {}), done, 4)
        self.assertTrue(again and again["repeated"] and "Answer from it" in again["note"])

    def test_failed_action_gets_one_retry(self) -> None:
        done: dict = {}
        c = self.Call("1", "open_app", {"name": "outlook"})
        self.record(done, c, 1, {"ok": False, "error": "timeout"})
        self.assertIsNone(self.repeat_of(c, done, 1))
        self.record(done, c, 2, {"ok": False, "error": "timeout"}, runs=2)
        self.assertTrue(self.repeat_of(c, done, 2)["repeated"])

    def test_observation_repeats_only_after_something_changed(self) -> None:
        done: dict = {}
        c = self.Call("1", "look_at_screen", {"question": "what is open?"})
        self.record(done, c, 1, {"ok": True, "result": "a browser"})
        self.assertTrue(self.repeat_of(c, done, 1)["repeated"])  # nothing happened in between
        self.assertIsNone(self.repeat_of(c, done, 2))  # another tool ran since

    def test_keystrokes_always_run(self) -> None:
        done: dict = {}
        c = self.Call("1", "press_keys", {"keys": ["down"]})
        self.record(done, c, 1, {"ok": True})
        self.assertIsNone(self.repeat_of(c, done, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
