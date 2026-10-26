"""Initiative: away and back from idle time and lock (a video playing is not away), one welcome after a real
absence, and follow-ups only with a reason, once per pause, capped per hour, never on a follow-up."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend import initiative as ini  # noqa: E402
from jarvis_backend.db import DB  # noqa: E402


class FakeLLM:
    def __init__(self, say: str) -> None:
        self.say, self.calls = say, 0

    async def generate_json(self, prompt, schema):
        self.calls += 1
        return {"say": self.say, "reason": "open question"}


class FakeBridge:
    connected = True
    stats: dict = {}

    def __init__(self) -> None:
        self.events = []

    def voice_event(self, e):
        self.events.append(e)


def make(db, llm, busy=False):
    said = []

    async def say(text):
        said.append(text)

    i = ini.Initiative(settings={"followups": True, "presence_greetings": True, "address": "sir"}, bridge=FakeBridge(), db=db,
                       llm=llm, say=say, busy=lambda: busy, happenings=lambda since: ["report.pdf finished downloading"])
    return i, said


class Presence(unittest.TestCase):
    def test_away_and_back(self) -> None:
        i, _ = make(None, None)
        t = 1000.0
        self.assertIsNone(i.observe({"idle_s": 400, "locked": False, "audio_playing": True}, t))  # watching a video
        self.assertNotEqual(i.state, "away")
        i.observe({"idle_s": 400, "locked": False, "audio_playing": False}, t)
        self.assertEqual(i.state, "away")
        self.assertEqual(i.observe({"idle_s": 1, "locked": False}, t + 900), "returned")  # gone ~22 min
        self.assertIn("report.pdf finished downloading", i.welcome(1300, t))

    def test_short_absence_is_not_greeted(self) -> None:
        i, _ = make(None, None)
        i.observe({"idle_s": 0, "locked": True}, 1000)
        self.assertIsNone(i.observe({"idle_s": 1, "locked": False}, 1100))


class FollowUps(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dir.name) / "t.db")
        now = time.time()
        self.db.run("INSERT INTO conversations (id, title, created_at, updated_at) VALUES ('c','t',?,?)", [now - 400, now - 300])
        self.db.run("INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES ('m1','c','user','which laptop should I buy?',?)", [now - 320])
        self.db.run("INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES ('m2','c','model','What is your budget?',?)", [now - 300])

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    async def test_follows_up_once_and_never_on_itself(self) -> None:
        llm = FakeLLM("Did you settle on a budget for the laptop, sir?")
        i, said = make(self.db, llm)
        self.assertEqual(await i.maybe_follow_up(), llm.say)
        self.assertEqual(said, [llm.say])
        self.db.run("UPDATE conversations SET updated_at=?", [time.time() - 300])  # quiet again after the follow-up
        self.assertIsNone(await i.maybe_follow_up())
        self.assertEqual(llm.calls, 1)

    async def test_nothing_worth_saying_stays_silent(self) -> None:
        i, said = make(self.db, FakeLLM(""))
        self.assertIsNone(await i.maybe_follow_up())
        self.assertEqual(said, [])

    async def test_not_while_busy_away_or_over_the_cap(self) -> None:
        llm = FakeLLM("x")
        i, _ = make(self.db, llm, busy=True)
        self.assertIsNone(await i.maybe_follow_up())
        i, _ = make(self.db, llm)
        i.state = "away"
        self.assertIsNone(await i.maybe_follow_up())
        i.state = "active"
        i._recent = [time.time()] * ini.FOLLOWUPS_PER_HOUR
        self.assertIsNone(await i.maybe_follow_up())
        self.assertEqual(llm.calls, 0)


if __name__ == "__main__":
    unittest.main()
