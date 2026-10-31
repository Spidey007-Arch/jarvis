"""Reminders, alarms and timers: when they ring, what JARVIS says, and what counts as missed."""
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend.db import DB  # noqa: E402
from jarvis_backend.reminders import ReminderError, Reminders, spoken  # noqa: E402


class Alarms(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dir.name) / "t.db")
        self.r = Reminders(self.db, bridge=None)

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def test_alarm_time_already_past_today_rings_tomorrow(self) -> None:
        past = datetime.fromtimestamp(time.time() - 3600).strftime("%H:%M")
        out = self.r.add_alarm(past, "gym")
        row = self.db.one("SELECT * FROM reminders WHERE id=?", [out["id"]])
        self.assertGreater(row["due_at"], time.time() + 22 * 3600)
        self.assertEqual(row["kind"], "alarm")

    def test_timer_and_its_words(self) -> None:
        out = self.r.add_timer(90, "")
        self.assertEqual(out["label"], "1 minute 30 seconds")
        row = self.db.one("SELECT * FROM reminders WHERE id=?", [out["id"]])
        self.assertEqual(spoken(row), "Your 1 minute 30 seconds timer is done.")
        self.assertEqual(self.r.pending()[0]["kind"], "timer")
        with self.assertRaises(ReminderError):
            self.r.add_timer(0)

    def test_spoken_lines(self) -> None:
        self.assertEqual(spoken({"kind": "reminder", "text": "call Papa", "due_at": time.time()}), "Just a reminder: call Papa")
        alarm = spoken({"kind": "alarm", "text": "gym", "due_at": datetime(2026, 1, 1, 7, 0).timestamp()})
        self.assertTrue(alarm.startswith("Alarm. It's 07:00. gym.") and alarm.count("gym") == 2)

    def test_due_while_off_is_missed_once(self) -> None:
        self.db.run("INSERT INTO reminders (id, text, due_at, created_at, status, kind) VALUES ('a','old',?,?,'pending','reminder')",
                    [time.time() - 3600, time.time() - 7200])
        self.db.run("INSERT INTO reminders (id, text, due_at, created_at, status, kind) VALUES ('b','now',?,?,'pending','reminder')",
                    [time.time() - 5, time.time() - 60])
        missed = self.r.overdue()
        self.assertEqual([r["id"] for r in missed], ["a"])  # just due ("b") is rung now, not missed
        self.r.mark_missed(missed)
        self.assertEqual(self.r.overdue(), [])
        self.assertEqual([p["id"] for p in self.r.pending()], ["b"])

    def test_missed_line_survives_a_voice_that_isnt_ready(self) -> None:
        """Issue #1: on a cold start the first attempt fails; the missed reminder must still be said once it can be."""
        import asyncio

        from jarvis_backend.bridge import AgentUnavailable

        class Bridge:
            def __init__(self) -> None:
                self.connected, self.said, self.tries = True, [], 0

            async def call(self, action, args, timeout=0):
                self.tries += 1
                if self.tries == 1:
                    raise AgentUnavailable("not connected yet")
                self.said.append(args["text"])

        self.db.run("INSERT INTO reminders (id, text, due_at, created_at, status, kind) VALUES ('a','call Papa',?,?,'pending','reminder')",
                    [time.time() - 3600, time.time() - 7200])
        bridge = Bridge()
        r = Reminders(self.db, bridge=bridge)

        async def two_passes() -> None:
            task = asyncio.create_task(r.run())
            while not bridge.said:
                await asyncio.sleep(0.05)
            task.cancel()

        import jarvis_backend.reminders as rem
        real_sleep = rem.asyncio.sleep
        rem.asyncio.sleep = lambda s: real_sleep(0.01)  # don't wait 10 s between passes in a test
        try:
            asyncio.run(asyncio.wait_for(two_passes(), 5))
        finally:
            rem.asyncio.sleep = real_sleep
        self.assertIn("call Papa", bridge.said[0])
        self.assertEqual(self.db.one("SELECT status FROM reminders WHERE id='a'")["status"], "missed")

    def test_nothing_is_marked_while_the_voice_is_offline(self) -> None:
        import asyncio

        class Offline:
            connected = False

            async def call(self, *a, **k):
                raise AssertionError("must not try to speak while offline")

        self.db.run("INSERT INTO reminders (id, text, due_at, created_at, status, kind) VALUES ('a','x',?,?,'pending','reminder')",
                    [time.time() - 3600, time.time() - 7200])
        r = Reminders(self.db, bridge=Offline())

        async def a_while() -> None:
            task = asyncio.create_task(r.run())
            await asyncio.sleep(0.2)
            task.cancel()

        asyncio.run(a_while())
        self.assertEqual(self.db.one("SELECT status FROM reminders WHERE id='a'")["status"], "pending")


if __name__ == "__main__":
    unittest.main()
