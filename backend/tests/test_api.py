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
        r = self.client.put("/api/settings", headers=AUTH, json={"gemini_api_key": "AIza-definitely-not-valid-000000"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("rejected", r.json()["detail"])
        print("\n    Google said:", r.json()["detail"][:160])
        self.assertFalse(self.client.get("/api/settings", headers=AUTH).json()["has_gemini_key"])

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

    def test_conversations_empty_and_404(self):
        self.assertEqual(self.client.get("/api/conversations", headers=AUTH).json(), [])
        self.assertEqual(self.client.get("/api/conversations/nope/messages", headers=AUTH).status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
