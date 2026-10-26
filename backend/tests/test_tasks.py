"""Task mode: pausing holds the next step, handing back makes JARVIS look again, stop ends it, and a task
cut short by a restart is offered once, never resumed on its own."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend.db import DB  # noqa: E402
from jarvis_backend.tasks import PLAN_ROUNDS, Tasks, TaskStopped  # noqa: E402


class TaskMode(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dir.name) / "t.db")
        self.tasks = Tasks(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    async def test_plan_makes_it_visible_and_raises_the_limit(self) -> None:
        t = self.tasks.begin("tidy my downloads", "c1", [])
        self.assertFalse(t.visible)
        self.tasks.set_plan(["List the folder", "Sort files", "Report"], 1)
        self.assertTrue(t.visible)
        self.assertEqual((t.current, t.max_rounds), (1, PLAN_ROUNDS))

    async def test_pause_holds_the_next_step_until_resumed(self) -> None:
        t = self.tasks.begin("x", "c1", [])
        self.tasks.control(t.id, "pause")
        step = asyncio.create_task(self.tasks.checkpoint(t))
        await asyncio.sleep(0.05)
        self.assertFalse(step.done())
        self.tasks.control(t.id, "resume")
        self.assertIsNone(await asyncio.wait_for(step, 1))

    async def test_take_over_then_hand_back_says_look_again_once(self) -> None:
        t = self.tasks.begin("x", "c1", [])
        self.tasks.control(t.id, "take_over")
        step = asyncio.create_task(self.tasks.checkpoint(t))
        await asyncio.sleep(0.05)
        self.tasks.control(t.id, "hand_back")
        self.assertEqual(await asyncio.wait_for(step, 1), "handed_back")
        self.assertIsNone(await self.tasks.checkpoint(t))

    async def test_stop_ends_a_paused_task(self) -> None:
        t = self.tasks.begin("x", "c1", [])
        self.tasks.control(t.id, "pause")
        step = asyncio.create_task(self.tasks.checkpoint(t))
        await asyncio.sleep(0.05)
        self.assertTrue(self.tasks.stop_all())
        with self.assertRaises(TaskStopped):
            await asyncio.wait_for(step, 1)

    async def test_cut_short_by_a_restart_is_offered_once(self) -> None:
        acts = [{"label": "Listed Downloads", "status": "done"}]
        t = self.tasks.begin("tidy my downloads", "c1", acts)
        self.tasks.set_plan(["List", "Sort", "Report"], 1)
        await self.tasks.checkpoint(t)  # saved while running; then JARVIS closes
        again = Tasks(self.db)
        offer = again.offer()
        self.assertEqual((offer["goal"], offer["current"], offer["done_steps"]), ("tidy my downloads", 1, ["Listed Downloads"]))
        prompt = again.continue_prompt(offer["id"])
        self.assertIn("check the current state first", prompt)
        self.assertIn("Listed Downloads", prompt)
        self.assertIsNone(again.offer())


if __name__ == "__main__":
    unittest.main()
