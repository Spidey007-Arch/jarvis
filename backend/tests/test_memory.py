"""Offline tests for settings encryption and the memory store.

The embedder here is a deterministic bag-of-words stand-in so ranking and merge
logic can be tested without network access. Real Gemini behaviour is checked
separately by the live test once an API key is configured.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "common")]

from jarvis_backend import memory as mem  # noqa: E402
from jarvis_backend.db import DB  # noqa: E402
from jarvis_backend.settings import SettingsStore  # noqa: E402
from jarvis_common import dpapi  # noqa: E402


class FakeLLM:
    embed_model = "fake-embed"
    can_embed = True

    def __init__(self) -> None:
        self.json_replies: list[dict] = []

    async def embed(self, texts, task):
        out = []
        for t in texts:
            v = [0.0] * 64
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
            out.append(v)
        return out

    async def generate_json(self, prompt, schema):
        return self.json_replies.pop(0)


def run(coro):
    return asyncio.run(coro)


class DpapiTests(unittest.TestCase):
    def test_roundtrip(self):
        blob = dpapi.protect(b"secret-value")
        self.assertNotIn(b"secret-value", blob)
        self.assertEqual(dpapi.unprotect(blob), b"secret-value")

    def test_settings_encrypted_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.dat"
            s = SettingsStore(p)
            s.set_api_key("openai", "sk-TESTKEY123456")
            s.set_provider("openai")
            s.update_profile({"user_name": "Tester", "address": "sir"})
            raw = p.read_bytes()
            self.assertNotIn(b"sk-TESTKEY", raw)
            self.assertNotIn(b"Tester", raw)
            again = SettingsStore(p)
            self.assertEqual(again.api_key("openai"), "sk-TESTKEY123456")
            pub = again.public()
            self.assertEqual((pub["provider"], pub["user_name"], pub["api_key_hint"]), ("openai", "Tester", "…3456"))
            self.assertEqual(pub["base_url"], "https://api.openai.com/v1")
            self.assertNotIn("sk-TESTKEY123456", str(pub))

    def test_v1_settings_migrate_to_per_provider(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.dat"
            v1 = {"version": 1, "user_name": "", "address": "sir", "chat_model": "gemini-x-flash",
                  "embed_model": "gemini-embedding-2", "secrets": {"gemini_api_key": "AIzaOLDKEY99999"}}
            p.write_bytes(dpapi.protect(json.dumps(v1).encode()))
            s = SettingsStore(p)
            self.assertEqual(s.provider, "gemini")
            self.assertEqual(s.api_key("gemini"), "AIzaOLDKEY99999")
            self.assertEqual(s.provider_cfg()["chat_model"], "gemini-x-flash")
            self.assertEqual(s.provider_cfg()["embed_model"], "gemini-embedding-2")

    def test_corrupt_settings_reported_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.dat"
            p.write_bytes(b"not dpapi data")
            s = SettingsStore(p)
            self.assertIsNotNone(s.load_error)
            self.assertTrue((Path(d) / "settings.unreadable").exists())


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        self.llm = FakeLLM()
        self.store = mem.MemoryStore(self.db, self.llm)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def op(self, **kw):
        base = {"op": "create", "type": "preference", "subject": "music", "content": "", "importance": 0.5, "target_id": ""}
        base.update(kw)
        return base

    def test_create_update_delete(self):
        r = run(self.store.apply_ops([self.op(content="Likes jazz music while coding.")], "c1"))
        self.assertEqual(r["created"], 1)
        [m] = self.store.list()
        r = run(self.store.apply_ops([self.op(op="update", target_id=m["id"], content="Now prefers lo-fi beats while coding.")], "c1"))
        self.assertEqual(r["updated"], 1)
        active = self.store.list()
        self.assertEqual([a["content"] for a in active], ["Now prefers lo-fi beats while coding."])
        old = self.store.list(status="superseded")
        self.assertEqual(old[0]["superseded_by"], active[0]["id"])
        r = run(self.store.apply_ops([self.op(op="delete", target_id=active[0]["id"])], "c1"))
        self.assertEqual(r["deleted"], 1)
        self.assertEqual(self.store.list(), [])

    def test_near_duplicate_create_becomes_update(self):
        run(self.store.apply_ops([self.op(type="identity", subject="name", content="The user's name is Parth.")], "c1"))
        run(self.store.apply_ops([self.op(type="identity", subject="name", content="The user's name is Parth.", importance=0.9)], "c2"))
        active = self.store.list()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["importance"], 0.9)

    def test_invalid_ops_are_skipped(self):
        r = run(self.store.apply_ops(
            [self.op(type="nonsense", content="x"), self.op(content=""), self.op(op="delete", target_id="missing")], "c1"
        ))
        self.assertEqual(r["skipped"], 3)
        self.assertEqual(self.store.list(), [])

    def test_search_ranks_relevant_and_excludes_core(self):
        run(self.store.apply_ops([
            self.op(type="identity", subject="name", content="The user's name is Parth."),
            self.op(type="project", subject="project: jarvis", content="Building a desktop assistant called JARVIS in Python and Rust."),
            self.op(type="preference", subject="food", content="Loves spicy paneer tikka."),
        ], "c1"))
        hits = run(self.store.search("how is the JARVIS desktop assistant project in Rust going"))
        self.assertTrue(hits)
        self.assertEqual(hits[0]["subject"], "project: jarvis")
        self.assertNotIn("identity", {h["type"] for h in hits})
        self.assertNotIn("food", {h["subject"] for h in hits})
        self.assertEqual([c["subject"] for c in self.store.core()], ["name"])

    def test_summarize_consolidates_subject(self):
        ops = [self.op(type="project", subject="project: jarvis", content=f"Milestone note number {i} about distinct topic t{i}.")
               for i in range(mem.SUMMARIZE_AT)]
        run(self.store.apply_ops(ops, "c1"))
        self.llm.json_replies.append({"memories": [
            {"content": "JARVIS project has completed milestones 0 through 9.", "importance": 0.7}]})
        merged = run(self.store.maybe_summarize())
        self.assertEqual(merged, mem.SUMMARIZE_AT)
        active = self.store.list()
        self.assertEqual(len(active), 1)
        self.assertEqual(len(self.store.list(status="summarized")), mem.SUMMARIZE_AT)

    def test_maintenance_expires_and_purges(self):
        run(self.store.apply_ops([
            self.op(type="event", subject="trip", content="Went hiking once.", importance=0.1),
            self.op(type="event", subject="award", content="Won a national hackathon.", importance=0.9),
        ], "c1"))
        long_ago = time.time() - 400 * mem.DAY
        self.db.run("UPDATE memories SET updated_at=?, last_accessed_at=NULL", [long_ago])
        r = self.store.maintenance()
        self.assertEqual(r["expired"], 1)
        self.assertEqual({m["subject"] for m in self.store.list()}, {"award"})
        r = self.store.maintenance(now=time.time() + 100 * mem.DAY)
        self.assertEqual(r["purged"], 1)

    def test_a_correction_replaces_the_old_fact(self):
        run(self.store.apply_ops([self.op(type="identity", subject="city", content="Lives in Mumbai.")], "c1"))
        run(self.store.apply_ops([self.op(type="identity", subject="city", content="Lives in Pune.")], "c2"))
        self.assertEqual([m["content"] for m in self.store.list(type_="identity")], ["Lives in Pune."])
        self.assertEqual(len(self.store.list(status="superseded")), 1)

    def test_unused_facts_fade_and_then_expire(self):
        run(self.store.apply_ops([
            self.op(type="preference", subject="snacks", content="Likes masala chai.", importance=0.5),
            self.op(type="identity", subject="name", content="Is called Parth.", importance=0.95),
        ], "c1"))
        self.db.run("UPDATE memories SET updated_at=?, last_accessed_at=NULL", [time.time() - 200 * mem.DAY])
        self.assertGreaterEqual(self.store.maintenance()["faded"], 1)
        by = {m["subject"]: m["importance"] for m in self.store.list()}
        self.assertAlmostEqual(by["snacks"], 0.45, places=2)
        self.assertEqual(by["name"], 0.95)  # who the user is never fades
        self.db.run("UPDATE memories SET importance=0.1, updated_at=? WHERE subject='snacks'", [time.time() - 400 * mem.DAY])
        self.store.maintenance()
        self.assertEqual({m["subject"] for m in self.store.list()}, {"name"})

    def test_export_import_and_lazy_embedding(self):
        run(self.store.apply_ops([self.op(content="Prefers dark themes everywhere.")], "c1"))
        data = self.store.export()
        other = mem.MemoryStore(DB(Path(self.tmp.name) / "o.db"), self.llm)
        self.assertEqual(other.import_items(data + [{"type": "bad"}]), {"added": 1, "skipped": 1})
        self.assertEqual(other.import_items(data), {"added": 0, "skipped": 1})
        self.assertFalse(other.list()[0]["has_embedding"])
        self.assertEqual(run(other.embed_missing()), 1)
        self.assertTrue(other.list()[0]["has_embedding"])
        other.db.close()

    def test_keyword_search_without_embeddings(self):
        self.llm.can_embed = False
        run(self.store.apply_ops([
            self.op(type="project", subject="project: jarvis", content="Building a desktop assistant called JARVIS in Rust."),
            self.op(type="preference", subject="food", content="Loves spicy paneer tikka."),
            self.op(type="preference", subject="food", content="Loves spicy paneer tikka."),
        ], "c1"))
        self.assertEqual(len(self.store.list()), 2)  # duplicate merged without embeddings
        self.assertFalse(any(m["has_embedding"] for m in self.store.list()))
        hits = run(self.store.search("any progress on the JARVIS desktop assistant?"))
        self.assertEqual([h["subject"] for h in hits], ["project: jarvis"])

    def test_manual_delete_is_permanent(self):
        run(self.store.apply_ops([self.op(content="Likes tea.")], "c1"))
        [m] = self.store.list()
        run(self.store.apply_ops([self.op(op="update", target_id=m["id"], content="Likes green tea.")], "c1"))
        [cur] = self.store.list()
        self.assertTrue(self.store.delete(cur["id"]))
        self.assertEqual(self.db.all("SELECT * FROM memories"), [])

    def test_delete_removes_every_earlier_version_of_a_fact(self):
        run(self.store.apply_ops([self.op(content="Likes black coffee.")], "c1"))
        for content in ("Likes coffee with oat milk.", "Likes iced coffee with oat milk."):
            [cur] = self.store.list()
            run(self.store.apply_ops([self.op(op="update", target_id=cur["id"], content=content)], "c1"))
        [cur] = self.store.list()
        self.assertEqual(len(self.db.all("SELECT * FROM memories")), 3)  # A -> B -> C
        self.assertTrue(self.store.delete(cur["id"]))
        self.assertEqual(self.db.all("SELECT content FROM memories"), [])  # the first version is gone too
        self.assertFalse(self.store.delete(cur["id"]))

    def _summarize_two(self, with_sources: bool):
        ops = [self.op(type="project", subject="project: jarvis", content=f"Note {i}: detail about topic t{i}.")
               for i in range(mem.SUMMARIZE_AT)]
        run(self.store.apply_ops(ops, "c1"))
        half = mem.SUMMARIZE_AT // 2
        first = {"content": "JARVIS has a voice.", "importance": 0.7}
        second = {"content": "JARVIS has a memory.", "importance": 0.7}
        if with_sources:
            first["from"], second["from"] = list(range(1, half + 1)), list(range(half + 1, mem.SUMMARIZE_AT + 1))
        self.llm.json_replies.append({"memories": [first, second]})
        run(self.store.maybe_summarize())
        return {m["content"]: m["id"] for m in self.store.list()}, half

    def test_deleting_a_summary_fact_removes_only_its_own_sources(self):
        facts, half = self._summarize_two(with_sources=True)
        self.assertTrue(self.store.delete(facts["JARVIS has a memory."]))
        left = {r["content"] for r in self.db.all("SELECT content FROM memories")}
        self.assertIn("JARVIS has a voice.", left)
        self.assertEqual({c for c in left if c.startswith("Note")}, {f"Note {i}: detail about topic t{i}." for i in range(half)})
        self.assertTrue(self.store.delete(facts["JARVIS has a voice."]))
        self.assertEqual(self.db.all("SELECT * FROM memories"), [])
        self.assertEqual(self.db.all("SELECT * FROM memory_sources"), [])

    def test_a_summary_fact_without_sources_takes_them_all_when_deleted(self):
        facts, _half = self._summarize_two(with_sources=False)
        self.assertTrue(self.store.delete(facts["JARVIS has a memory."]))
        left = [r["content"] for r in self.db.all("SELECT content FROM memories")]
        self.assertEqual(left, ["JARVIS has a voice."])  # it may have been made from any of them


if __name__ == "__main__":
    unittest.main(verbosity=2)
