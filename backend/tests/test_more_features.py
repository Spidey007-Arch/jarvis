"""App connections, always-running agents, workflows, deep research, A2A, the MCP server and the SDK."""
import asyncio
import email.message
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "common"), str(ROOT / "sdk")]

from fastapi.testclient import TestClient  # noqa: E402

from jarvis_backend import a2a, apps, deep_research, permissions  # noqa: E402
from jarvis_backend.agents import AgentJobError, Agents, split_notes  # noqa: E402
from jarvis_backend.apps import AppError, Apps, find_vault, imap_quote, mail_text, parse_feed, words_match  # noqa: E402
from jarvis_backend.db import DB  # noqa: E402
from jarvis_backend.main import create_app  # noqa: E402
from jarvis_backend.settings import SettingsStore  # noqa: E402
from jarvis_backend.tools import BY_NAME  # noqa: E402
from jarvis_backend.workflows import WorkflowError, Workflows, following, validate  # noqa: E402
from jarvis_sdk import Jarvis, mcp_server  # noqa: E402

RSS = """<rss><channel><title>Feed</title>
<item><title>Rain &amp; storms in Mumbai</title><link>https://example.com/a</link><description><![CDATA[<p>Heavy rain</p>]]></description><pubDate>Sat, 07 Nov 2026</pubDate></item>
<item><title>Chess final</title><link>https://example.com/b</link></item></channel></rss>"""
ATOM = """<feed><entry><title>Release 2.0</title><link href="https://example.com/r"/><updated>2026-11-07T10:00:00Z</updated><summary>New version</summary></entry></feed>"""


class Temp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.settings = SettingsStore(self.dir / "settings.dat")
        self.db = DB(self.dir / "t.db")
        self.addCleanup(self.db.close)


class AppTests(Temp):
    def test_feeds_of_both_kinds_are_read(self) -> None:
        got = parse_feed(RSS)
        self.assertEqual([x["title"] for x in got], ["Rain & storms in Mumbai", "Chess final"])
        self.assertEqual(got[0]["snippet"], "Heavy rain")
        self.assertEqual(got[0]["url"], "https://example.com/a")
        self.assertEqual(parse_feed(ATOM)[0]["url"], "https://example.com/r")

    def test_what_is_on_from_the_start(self) -> None:
        a = Apps(self.settings)
        self.assertTrue(a.connected("rss"))
        self.assertTrue(a.connected("hackernews"))
        for name in ("github", "imap", "google", "oura"):
            self.assertFalse(a.connected(name), name)
        with self.assertRaisesRegex(AppError, "isn't connected"):
            asyncio.run(a.list_items("github"))
        with self.assertRaisesRegex(AppError, "no app called"):
            asyncio.run(a.list_items("myspace"))

    def test_tokens_stay_secret_and_disconnect_forgets_them(self) -> None:
        a = Apps(self.settings)
        a.save("github", {"token": "ghp_secret"})
        self.assertTrue(a.connected("github"))
        shown = json.dumps(a.overview())
        self.assertNotIn("ghp_secret", shown)
        self.assertIn("ghp_secret", self.settings.secret_values())  # so it is scrubbed from tool output too
        self.assertNotIn("app:github:token", self.settings.service_keys())  # not listed with the user's API keys
        a.disconnect("github")
        self.assertFalse(a.connected("github"))
        self.assertEqual(self.settings.service_key("app:github:token"), "")

    def test_a_ready_app_can_be_turned_off_and_on(self) -> None:
        a = Apps(self.settings)
        a.disconnect("rss")
        self.assertFalse(a.connected("rss"))
        a.save("rss", {})
        self.assertTrue(a.connected("rss"))

    def test_obsidian_finds_the_vault_searches_and_stays_inside(self) -> None:
        vault = self.dir / "Notes"
        (vault / "Work").mkdir(parents=True)
        (vault / ".obsidian").mkdir()
        (vault / ".obsidian" / "skip.md").write_text("dentist", encoding="utf-8")
        (vault / "Work" / "Plan.md").write_text("Call the dentist on Friday", encoding="utf-8")
        (vault / "Shopping.md").write_text("Milk", encoding="utf-8")
        (self.dir / "secret.md").write_text("private", encoding="utf-8")
        listing = self.dir / "obsidian.json"
        listing.write_text(json.dumps({"vaults": {"a": {"path": str(vault), "ts": 5}, "b": {"path": str(self.dir / "gone"), "ts": 9}}}), encoding="utf-8")
        self.assertEqual(find_vault(listing), str(vault))
        a = Apps(self.settings)
        a.save("obsidian", {"vault": str(vault)})
        found = asyncio.run(a.list_items("obsidian", "dentist"))
        self.assertEqual([x["id"] for x in found], ["Work/Plan.md"])
        self.assertIn("Friday", asyncio.run(a.read("obsidian", "Work/Plan.md"))["text"])
        with self.assertRaisesRegex(AppError, "no note"):
            asyncio.run(a.read("obsidian", "../secret.md"))

    def test_github_goes_only_to_github_with_its_token(self) -> None:
        a = Apps(self.settings)
        a.save("github", {"token": "t0ken"})
        seen = {}

        async def send(method, url, headers, params, body, as_text=False):
            seen.update(url=url, auth=headers["Authorization"])
            return [{"id": "1", "reason": "mention", "updated_at": "2026-11-07", "repository": {"full_name": "a/b"}, "subject": {"title": "Fix login", "type": "Issue"}},
                    {"id": "2", "reason": "subscribed", "updated_at": "2026-11-06", "repository": {"full_name": "c/d"}, "subject": {"title": "Docs", "type": "PullRequest"}}]

        a._send = send
        got = asyncio.run(a.list_items("github", "login"))
        self.assertEqual([x["title"] for x in got], ["a/b: Fix login"])
        self.assertEqual(seen, {"url": "https://api.github.com/notifications", "auth": "Bearer t0ken"})

    def test_one_failing_app_does_not_stop_the_search(self) -> None:
        a = Apps(self.settings)

        async def good(q, limit):
            return [apps.item("rss", 1, "Found it")]

        async def bad(q, limit):
            raise AppError("It's down.")

        a._rss, a._hackernews = good, bad
        out = asyncio.run(a.search_all("x", ["rss", "hackernews"]))
        self.assertEqual([x["title"] for x in out["results"]], ["Found it"])
        self.assertEqual(out["problems"], {"hackernews": "It's down."})

    def test_sign_in_needs_the_app_details_first(self) -> None:
        with self.assertRaisesRegex(AppError, "client ID"):
            Apps(self.settings).sign_in("google")
        with self.assertRaisesRegex(AppError, "doesn't use"):
            Apps(self.settings).sign_in("github")

    def test_a_sign_in_comes_back_with_the_right_state_only(self) -> None:
        async def go() -> None:
            a = Apps(self.settings)
            a.save("strava", {"client_id": "abc", "client_secret": "shh"})
            got = {}

            async def exchange(app, form):
                got.update(form)
                self.settings.set_service_key(f"app:{app}:refresh_token", "r1")
                return "access"

            a._exchange = exchange
            url = a.sign_in("strava")
            self.assertIn("code_challenge=", url)
            self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A53682%2F", url)
            state = url.split("state=")[1].split("&")[0]
            await asyncio.sleep(0.1)

            async def visit(path: str) -> None:
                reader, writer = await asyncio.open_connection("127.0.0.1", apps.REDIRECT_PORT)
                writer.write(f"GET {path} HTTP/1.1\r\n\r\n".encode())
                await writer.drain()
                await reader.read()
                writer.close()

            await visit("/?state=wrong&code=stolen")
            self.assertFalse(a.connected("strava"))
            await visit(f"/?state={state}&code=real")
            await asyncio.sleep(0.1)
            self.assertEqual(got["code"], "real")
            self.assertTrue(a.connected("strava"))
            self.assertEqual(a.waiting["strava"], "")

        asyncio.run(go())

    def test_helpers(self) -> None:
        self.assertTrue(words_match("Call the Dentist", "dentist call"))
        self.assertFalse(words_match("Call", "dentist"))
        self.assertEqual(imap_quote('a"b\r\nc\\'), '"ab  c"')
        msg = email.message.EmailMessage()
        msg.set_content("Hello there")
        self.assertEqual(mail_text(msg.as_bytes()), "Hello there")

    def test_everything_only_reads(self) -> None:
        for name in ("search_everything", "app_items", "app_read"):
            self.assertEqual(BY_NAME[name].risk, "auto")
            self.assertIn(name, permissions.PRIVATE_READS)  # sending it out afterwards asks first


class AgentTests(Temp):
    def jobs(self, replies: list[dict]) -> tuple[Agents, list]:
        said, asked = [], []
        a = Agents(self.db)

        async def runner(text, conv):
            asked.append(text)
            self.db.run("INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at) VALUES ('c1','x',?,?)", [time.time(), time.time()])
            return {"status": "done", "conversation_id": "c1", **replies.pop(0)}

        async def say(text):
            said.append(text)

        a.runner, a.say = runner, say
        a.asked = asked
        return a, said

    def test_notes_are_split_from_the_reply(self) -> None:
        self.assertEqual(split_notes("It dropped.\nNOTES: was 70000"), ("It dropped.", "was 70000"))
        self.assertEqual(split_notes("ALL_CLEAR\n**NOTES:** nothing new"), ("ALL_CLEAR", "nothing new"))
        self.assertEqual(split_notes("No notes here"), ("No notes here", None))

    def test_it_carries_on_from_its_notebook_and_speaks_only_when_needed(self) -> None:
        a, said = self.jobs([{"result": "ALL_CLEAR\nNOTES: price 70000"}, {"result": "It dropped to 59000!\nNOTES: told the user"}])
        aid = a.save("Laptop", "Watch the price", 60)["id"]
        self.assertEqual(asyncio.run(a.wake(aid))["status"], "quiet")
        self.assertEqual(said, [])
        self.assertEqual(a.get(aid)["notebook"], "price 70000")
        self.assertEqual(asyncio.run(a.wake(aid))["status"], "told")
        self.assertIn("price 70000", a.asked[1])
        self.assertIn("wake number 2", a.asked[1])
        self.assertEqual(said, ["Laptop: It dropped to 59000!"])
        self.assertEqual(a.get(aid)["notebook"], "told the user")

    def test_a_reply_without_notes_keeps_the_notebook(self) -> None:
        a, _ = self.jobs([{"result": "ALL_CLEAR\nNOTES: keep me"}, {"result": "ALL_CLEAR"}])
        aid = a.save("A", "G", 60)["id"]
        asyncio.run(a.wake(aid))
        asyncio.run(a.wake(aid))
        self.assertEqual(a.get(aid)["notebook"], "keep me")

    def test_only_due_and_enabled_agents_wake(self) -> None:
        a, _ = self.jobs([])
        one = a.save("One", "G", 60)["id"]
        two = a.save("Two", "G", 60)["id"]
        now = time.time()
        self.assertEqual(a.due(now), [])
        a.set_enabled(two, False)
        self.assertEqual([x["id"] for x in a.due(now + 3700)], [one])

    def test_it_pauses_after_failing_again_and_again(self) -> None:
        a, _ = self.jobs([{"status": "failed", "result": "no key"} for _ in range(5)])
        aid = a.save("A", "G", 60)["id"]
        for _ in range(5):
            asyncio.run(a.wake(aid))
        self.assertFalse(a.get(aid)["enabled"])

    def test_limits_and_survival(self) -> None:
        a, _ = self.jobs([])
        with self.assertRaisesRegex(AgentJobError, "every 15"):
            a.save("A", "G", 5)
        with self.assertRaisesRegex(AgentJobError, "name and a goal"):
            a.save("", "G", 60)
        aid = a.save("A", "G", 60)["id"]
        self.assertEqual(Agents(self.db).get(aid)["name"], "A")  # after a restart


class WorkflowTests(Temp):
    def flow(self, replies: list[str], answers: list[bool] | None = None) -> tuple[Workflows, list, list]:
        asked, said = [], []
        checks = list(answers or [])

        class LLM:
            async def generate_json(self, prompt, schema):
                return {"answer": checks.pop(0)}

        w = Workflows(self.db, LLM())

        async def runner(text, conv):
            asked.append(text)
            return {"status": "done", "result": replies.pop(0), "conversation_id": None}

        async def say(text):
            said.append(text)

        w.runner, w.say = runner, say
        return w, asked, said

    def test_steps_run_in_order_and_pass_results_on(self) -> None:
        w, asked, said = self.flow(["3 unread", "Summary"])
        w.save("Mail", [{"kind": "ask", "text": "Count my mail, {input}"}, {"kind": "ask", "text": "Summarise: {last}"}, {"kind": "say", "text": "Done: {last}"}])
        out = asyncio.run(w.run("mail", "please"))
        self.assertEqual(out["status"], "done")
        self.assertIn("Count my mail, please", asked[0])
        self.assertIn("Summarise: 3 unread", asked[1])
        self.assertEqual(said, ["Done: Summary"])

    def test_a_check_takes_the_right_branch(self) -> None:
        steps = [{"id": "get", "kind": "ask", "text": "Any errors?"}, {"id": "c", "kind": "check", "text": "Were there errors?", "yes": "fix", "no": "fine"},
                 {"id": "fix", "kind": "say", "text": "Fixing", "next": "end"}, {"id": "fine", "kind": "say", "text": "All good"}]
        w, _, said = self.flow(["none"], [False])
        w.save("Check", steps)
        asyncio.run(w.run("Check"))
        self.assertEqual(said, ["All good"])
        w2, _, said2 = self.flow(["3 errors"], [True])
        asyncio.run(w2.run("Check"))
        self.assertEqual(said2, ["Fixing"])

    def test_a_loop_runs_until_the_check_says_stop_and_a_runaway_is_stopped(self) -> None:
        steps = [{"id": "try", "kind": "ask", "text": "Try"}, {"id": "c", "kind": "check", "text": "Worked?", "yes": "end", "no": "try"}]
        w, asked, _ = self.flow(["no", "no", "yes"], [False, False, True])
        w.save("Loop", steps)
        self.assertEqual(asyncio.run(w.run("Loop"))["status"], "done")
        self.assertEqual(len(asked), 3)
        w2, _, _ = self.flow(["x"] * 200, [False] * 200)
        self.assertEqual(asyncio.run(w2.run("Loop"))["status"], "stopped")

    def test_a_failed_step_stops_the_run(self) -> None:
        w, _, said = self.flow([])

        async def runner(text, conv):
            return {"status": "failed", "result": "no AI"}

        w.runner = runner
        w.save("F", [{"kind": "ask", "text": "Go"}, {"kind": "say", "text": "Never"}])
        self.assertEqual(asyncio.run(w.run("F"))["status"], "failed")
        self.assertEqual(said, [])

    def test_bad_workflows_are_explained(self) -> None:
        for steps, why in (([], "at least one"), ([{"kind": "dance", "text": "x"}], "kind"), ([{"kind": "ask"}], "write what"),
                           ([{"id": "a", "kind": "say", "text": "x"}, {"id": "a", "kind": "say", "text": "y"}], "two steps"),
                           ([{"kind": "say", "text": "x", "next": "nowhere"}], "isn't a step"), ([{"kind": "wait", "seconds": 0}], "wait between")):
            with self.assertRaisesRegex(WorkflowError, why):
                validate(steps)
        self.assertIsNone(following([{"id": "a", "next": "end"}], 0))
        w, _, _ = self.flow([])
        w.save("Same", [{"kind": "stop"}])
        with self.assertRaisesRegex(WorkflowError, "already"):
            w.save("same", [{"kind": "stop"}])


class ResearchTests(unittest.TestCase):
    def llm(self, plans: list[dict]):
        class LLM:
            prompts: list[str] = []

            async def generate_json(self, prompt, schema):
                self.prompts.append(prompt)
                return plans.pop(0)

        return LLM()

    def test_rounds_number_sources_and_stop_when_enough(self) -> None:
        async def web(q):
            return [{"kind": "web", "title": f"Page on {q}", "where": f"https://x/{q}", "text": "Fact " + q},
                    {"kind": "web", "title": "Same page", "where": "https://x/same", "text": "Repeated"}]

        async def docs(q):
            return [{"kind": "documents", "title": "My report", "where": "report.pdf", "text": "Private figure 42"}]

        llm = self.llm([{"enough": False, "queries": ["follow up", "follow up"]}, {"enough": True, "queries": []}])
        with mock.patch.object(deep_research, "from_web", web):
            out = asyncio.run(deep_research.research("Big question", llm, 5, [docs]))
        self.assertEqual(out["rounds"], 2)
        self.assertEqual(out["searches"], ["Big question", "follow up"])
        self.assertEqual([s["n"] for s in out["sources"]], list(range(1, len(out["sources"]) + 1)))
        self.assertEqual(sum(s["where"] == "https://x/same" for s in out["sources"]), 1)
        self.assertIn("documents", {s["kind"] for s in out["sources"]})

    def test_private_documents_never_reach_the_planning_of_web_searches(self) -> None:
        async def web(q):
            return [{"kind": "web", "title": "Public", "where": "https://x", "text": "Public fact"}]

        async def docs(q):
            return [{"kind": "documents", "title": "Salary", "where": "pay.pdf", "text": "SECRET salary"}]

        llm = self.llm([{"enough": True, "queries": []}])
        with mock.patch.object(deep_research, "from_web", web):
            asyncio.run(deep_research.research("Q", llm, 3, [docs]))
        self.assertNotIn("SECRET", llm.prompts[0])
        self.assertIn("Public fact", llm.prompts[0])

    def test_a_search_that_fails_is_reported(self) -> None:
        async def web(q):
            raise deep_research.ResearchError("blocked")

        async def docs(q):
            return [{"kind": "documents", "title": "Mine", "where": "a", "text": "Something"}]

        with mock.patch.object(deep_research, "from_web", web):
            out = asyncio.run(deep_research.research("Q", self.llm([]), 1, [docs]))
            self.assertEqual(out["problems"], ["blocked"])
            with self.assertRaises(deep_research.ResearchError):
                asyncio.run(deep_research.research("Q", self.llm([]), 1, []))


class ToolPermissionTests(unittest.TestCase):
    def test_new_tools_ask_first_and_cannot_be_allowed_silently(self) -> None:
        for name, args, cap in (("ask_agent", {"url": "https://a"}, "other_agents"), ("hand_off_code", {"tool": "claude"}, "coding_agents"),
                                ("create_agent", {}, "automations"), ("delete_agent", {}, "automations"), ("create_workflow", {}, "automations")):
            self.assertEqual(permissions.capability(BY_NAME[name], args), cap, name)
            self.assertEqual(permissions.decide(BY_NAME[name], args, {cap: "allow"}, True)[0], "ask", name)


class ApiTests(unittest.TestCase):
    TOKEN = "test-token"
    AUTH = {"Authorization": f"Bearer {TOKEN}"}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"JARVIS_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("JARVIS_PARENT_PID", None)

    def client(self) -> TestClient:
        return TestClient(create_app(8799, self.TOKEN))

    def test_screens_get_what_they_need(self) -> None:
        with self.client() as c:
            listed = c.get("/api/apps", headers=self.AUTH).json()
            self.assertEqual({a["id"] for a in listed}, set(apps.APPS))
            self.assertEqual(c.get("/api/apps", headers={}).status_code, 401)
            saved = c.put("/api/apps/oura", json={"values": {"token": "abc"}}, headers=self.AUTH).json()
            self.assertTrue(next(a for a in saved if a["id"] == "oura")["connected"])
            self.assertNotIn("abc", json.dumps(saved))
            self.assertEqual(c.put("/api/apps/nope", json={"values": {}}, headers=self.AUTH).status_code, 400)
            c.delete("/api/apps/oura", headers=self.AUTH)

            a = c.post("/api/agents", json={"name": "A", "goal": "G", "every_min": 60}, headers=self.AUTH).json()
            self.assertEqual(c.post("/api/agents", json={"name": "A", "goal": "G", "every_min": 1}, headers=self.AUTH).status_code, 400)
            c.post(f"/api/agents/{a['id']}/enabled", json={"enabled": False}, headers=self.AUTH)
            self.assertFalse(c.get("/api/agents", headers=self.AUTH).json()[0]["enabled"])
            self.assertEqual(c.delete(f"/api/agents/{a['id']}", headers=self.AUTH).status_code, 200)

            w = c.post("/api/workflows", json={"name": "W", "steps": [{"kind": "say", "text": "Hi"}]}, headers=self.AUTH).json()
            self.assertEqual(c.post(f"/api/workflows/{w['id']}/run", json={}, headers=self.AUTH).json()["status"], "done")
            self.assertEqual(c.delete(f"/api/workflows/{w['id']}", headers=self.AUTH).status_code, 200)

    def test_search_covers_what_jarvis_keeps(self) -> None:
        with self.client() as c:
            c.post("/api/calendar/events", json={"title": "Dentist visit", "start": time.strftime("%Y-%m-%dT10:00", time.localtime(time.time() + 86400))}, headers=self.AUTH)
            out = c.get("/api/search?q=dentist", headers=self.AUTH).json()
            self.assertIn("calendar", out["searched"])
            self.assertIn("Dentist visit", [x["title"] for x in out["results"]])

    def test_a2a_is_off_until_turned_on_and_needs_its_own_key(self) -> None:
        call = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {"parts": [{"kind": "text", "text": "hello"}]}}}
        with self.client() as c:
            self.assertEqual(c.get("/.well-known/agent-card.json").status_code, 404)
            self.assertEqual(c.post("/a2a", json=call).status_code, 404)
            key = c.post("/api/developer/a2a", json={"enabled": True}, headers=self.AUTH).json()["key"]
            self.assertEqual(c.get("/.well-known/agent.json").json()["name"], "JARVIS")
            self.assertEqual(c.post("/a2a", json=call).status_code, 401)
            self.assertEqual(c.post("/a2a", json=call, headers=self.AUTH).status_code, 401)  # the backend's token isn't the key
            bad = c.post("/a2a", json={**call, "method": "tasks/cancel"}, headers={"Authorization": f"Bearer {key}"}).json()
            self.assertEqual(bad["error"]["code"], -32601)
            self.assertNotIn(key, json.dumps(c.get("/api/developer", headers=self.AUTH).json()))
            self.assertEqual(c.post("/api/developer/a2a", json={"enabled": False}, headers=self.AUTH).json(), {"on": False})
            self.assertEqual(c.post("/a2a", json=call, headers={"Authorization": f"Bearer {key}"}).status_code, 404)

    def test_mcp_setup_is_given_ready_to_paste(self) -> None:
        with self.client() as c:
            out = c.get("/api/developer", headers=self.AUTH).json()
            self.assertEqual(out["mcp"]["args"], ["-m", "jarvis_sdk.mcp_server"])
            self.assertTrue(out["sdk"].endswith("sdk"))


class FakeJarvis:
    def __init__(self) -> None:
        self.outside = None

    def ask_full(self, message, conversation_id=None, outside=False):
        self.outside = outside
        return {"status": "done", "result": f"Heard: {message}"}

    def search(self, query, limit=5):
        return {"results": [{"title": query}]}


class SdkTests(unittest.TestCase):
    def test_mcp_handshake_and_tools(self) -> None:
        out = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(out["result"]["protocolVersion"], "2025-03-26")
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["ask_jarvis", "search_jarvis", "jarvis_status"])
        self.assertEqual(mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)

    def test_what_another_app_asks_is_marked_as_outside(self) -> None:
        j = FakeJarvis()
        out = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ask_jarvis", "arguments": {"message": "hi"}}}, j)
        self.assertEqual(out["result"]["content"][0]["text"], "Heard: hi")
        self.assertTrue(j.outside)

    def test_finds_a_running_jarvis_and_says_when_there_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"JARVIS_DATA_DIR": d}):
            os.environ.pop("JARVIS_URL", None)
            os.environ.pop("JARVIS_TOKEN", None)
            with self.assertRaisesRegex(Exception, "isn't running"):
                Jarvis()
            (Path(d) / "runtime.json").write_text(json.dumps({"port": 1234, "token": "t", "pid": 1}), encoding="utf-8")
            self.assertEqual(Jarvis().url, "http://127.0.0.1:1234")


class A2AHelpers(unittest.TestCase):
    def test_answers_are_read_from_tasks_and_messages(self) -> None:
        self.assertEqual(a2a.answer_of(a2a.reply(1, "Done")["result"]), "Done")
        self.assertEqual(a2a.answer_of({"kind": "message", "parts": [{"kind": "text", "text": "Hi"}]}), "Hi")

    def test_addresses_must_be_safe(self) -> None:
        with self.assertRaisesRegex(a2a.A2AError, "https"):
            asyncio.run(a2a.ask("http://example.com", "hi"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
