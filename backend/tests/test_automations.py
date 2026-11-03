"""Automations: when schedules fire, what a run records, and what happens to runs missed while JARVIS was off."""
import asyncio
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend.automations import AutomationError, Automations, describe, next_run  # noqa: E402
from jarvis_backend.db import DB  # noqa: E402


class Schedules(unittest.TestCase):
    def test_daily_on_chosen_weekdays(self) -> None:
        monday_8am = datetime(2026, 11, 2, 8, 0).timestamp()  # a Monday
        self.assertEqual(datetime.fromtimestamp(next_run("daily", "09:00", [], 0, monday_8am)), datetime(2026, 11, 2, 9, 0))
        self.assertEqual(datetime.fromtimestamp(next_run("daily", "07:00", [], 0, monday_8am)), datetime(2026, 11, 3, 7, 0))
        friday_10 = datetime(2026, 11, 6, 10, 0).timestamp()
        self.assertEqual(datetime.fromtimestamp(next_run("daily", "09:00", [0, 1, 2, 3, 4], 0, friday_10)), datetime(2026, 11, 9, 9, 0))

    def test_once_and_interval(self) -> None:
        now = time.time()
        later = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        self.assertGreater(next_run("once", later, [], 0, now), now)
        self.assertIsNone(next_run("once", "2020-01-01T10:00", [], 0, now))
        self.assertEqual(next_run("interval", "", [], 5, now), now + 15 * 60)  # never more often than 15 minutes
        with self.assertRaises(AutomationError):
            next_run("daily", "25:00", [], 0, now)

    def test_words(self) -> None:
        self.assertEqual(describe({"kind": "daily", "at": "09:00", "days": [0, 1, 2, 3, 4]}), "weekdays at 09:00")
        self.assertEqual(describe({"kind": "interval", "every_min": 120, "days": []}), "every 2 hours")


class Store(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dir.name) / "t.db")
        self.a = Automations(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def test_save_validates(self) -> None:
        with self.assertRaises(AutomationError):
            self.a.save("x", "do it", "interval", every_min=5)
        with self.assertRaises(AutomationError):
            self.a.save("x", "do it", "once", at="2020-01-01T10:00")
        with self.assertRaises(AutomationError):
            self.a.save("", "do it", "daily", at="09:00")
        s = self.a.save("Mail", "summarise unread mail", "daily", at="09:00", deliver=["speak", "phone"])
        self.assertEqual(s["deliver"], ["speak"])  # the phone link is gone
        self.assertEqual(s["schedule"], "every day at 09:00")

    def test_a_run_records_its_result_and_moves_the_schedule_on(self) -> None:
        s = self.a.save("Check", "check the disk", "interval", every_min=30)
        self.db.run("UPDATE automations SET next_run=? WHERE id=?", [time.time() - 5, s["id"]])
        seen = []

        async def runner(a):
            seen.append(a["name"])
            return {"status": "done", "result": "Disk is 40% full.", "conversation_id": "c1"}

        self.a.runner = runner
        due = self.a.due()
        self.assertEqual([d["id"] for d in due], [s["id"]])
        asyncio.run(self.a.run_one(due[0]))
        after = self.a.get(s["id"])
        self.assertEqual((seen, after["last_status"], after["last_result"], after["conv_id"], after["runs"]),
                         (["Check"], "done", "Disk is 40% full.", "c1", 1))
        self.assertGreater(after["next_run"], time.time() + 25 * 60)
        self.assertEqual(self.a.due(), [])

    def test_a_run_long_missed_while_off_is_skipped_not_run_late(self) -> None:
        s = self.a.save("Old", "x", "interval", every_min=60)
        self.db.run("UPDATE automations SET next_run=? WHERE id=?", [time.time() - 3 * 3600, s["id"]])
        self.assertEqual(self.a.due(), [])
        self.assertEqual(self.a.get(s["id"])["last_status"], "missed")
        self.assertGreater(self.a.get(s["id"])["next_run"], time.time())

    def test_a_failing_run_is_recorded_and_doesnt_raise(self) -> None:
        s = self.a.save("Bad", "x", "interval", every_min=15)

        async def runner(a):
            raise RuntimeError("model down")

        self.a.runner = runner
        asyncio.run(self.a.run_one(self.a.get(s["id"]), scheduled=False))
        self.assertEqual(self.a.get(s["id"])["last_status"], "failed")


if __name__ == "__main__":
    unittest.main()
