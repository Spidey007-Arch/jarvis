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
        self.assertEqual([r["id"] for r in self.r.take_missed()], ["a"])
        self.assertEqual(self.r.take_missed(), [])
        self.assertEqual([p["id"] for p in self.r.pending()], ["b"])  # just due: rung now, not missed


if __name__ == "__main__":
    unittest.main()
