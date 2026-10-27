"""Background jobs wait until the user isn't using the PC (and the battery can spare it), run one at a time, and
never wait for ever."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend.db import DB  # noqa: E402
from jarvis_backend.jobs import Jobs  # noqa: E402


class BackgroundJobs(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dir.name) / "t.db")
        self.stats = {"idle_s": 5, "battery_plugged": True}
        self.state = "active"
        self.ran: list[str] = []
        self.jobs = Jobs(self.db, stats=lambda: self.stats, presence=lambda: self.state)

        async def a():
            self.ran.append("a")
            return "done"

        async def b():
            self.ran.append("b")
            raise RuntimeError("boom")

        self.jobs.add("a", 3600, a)
        self.jobs.add("b", 3600, b)

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def test_waits_for_idle_then_runs_one_at_a_time(self) -> None:
        self.assertIsNone(asyncio.run(self.jobs.tick()))  # the user is busy
        self.stats["idle_s"] = 600
        self.assertEqual(asyncio.run(self.jobs.tick()), "a")
        self.assertEqual(asyncio.run(self.jobs.tick()), "b")  # a failure is recorded, not raised
        self.assertIsNone(asyncio.run(self.jobs.tick()))  # both done for this period
        self.assertEqual(self.ran, ["a", "b"])
        self.assertTrue(self.jobs.view()[1]["last_result"].startswith("failed"))
        again = Jobs(self.db, stats=lambda: self.stats, presence=lambda: self.state)
        again.add("a", 3600, lambda: None)
        self.assertGreater(again.jobs[0].last_run, 0)  # remembered across restarts

    def test_low_battery_waits_and_overdue_runs_anyway(self) -> None:
        self.stats.update(idle_s=600, battery_plugged=False, battery_percent=20)
        self.assertIsNone(asyncio.run(self.jobs.tick()))
        for j in self.jobs.jobs:
            j.last_run = time.time() - 3 * 3600  # waited a whole extra period
        self.assertEqual(asyncio.run(self.jobs.tick()), "a")


if __name__ == "__main__":
    unittest.main()
