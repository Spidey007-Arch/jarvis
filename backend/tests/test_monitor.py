"""Proactive monitor decisions and the persona's situational context."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "common")]

from jarvis_backend.bridge import AgentBridge  # noqa: E402
from jarvis_backend.monitor import SUSTAINED_CHECKS, Monitor  # noqa: E402
from jarvis_backend.persona import build_system_prompt  # noqa: E402
from jarvis_backend.settings import SettingsStore  # noqa: E402


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = SettingsStore(Path(self.tmp.name) / "s.dat")
        self.m = Monitor(AgentBridge(), self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def test_low_battery_only_when_unplugged_and_once_per_cooldown(self):
        self.assertEqual(self.m.evaluate({"battery_percent": 10, "battery_plugged": True}, now=1000), [])
        fired = self.m.evaluate({"battery_percent": 10, "battery_plugged": False}, now=1000)
        self.assertEqual([k for k, _ in fired], ["battery"])
        self.assertIn("10%", fired[0][1])
        self.assertIn("sir", fired[0][1])
        self.assertEqual(self.m.evaluate({"battery_percent": 9, "battery_plugged": False}, now=1600), [])
        self.assertEqual(len(self.m.evaluate({"battery_percent": 8, "battery_plugged": False}, now=1000 + 31 * 60)), 1)

    def test_cpu_needs_sustained_overload(self):
        for i in range(SUSTAINED_CHECKS - 1):
            self.assertEqual(self.m.evaluate({"cpu_percent": 99}, now=10 + i), [])
        self.assertEqual([k for k, _ in self.m.evaluate({"cpu_percent": 99}, now=100)], ["cpu"])
        self.m.evaluate({"cpu_percent": 20}, now=101)  # a calm reading resets the streak
        self.assertEqual(self.m._streak["cpu"], 0)

    def test_full_disk_and_address_from_settings(self):
        self.settings.update_profile({"address": "boss"})
        fired = self.m.evaluate({"disk_percent": 97.2}, now=5)
        self.assertEqual(fired[0][0], "disk")
        self.assertIn("97%", fired[0][1])
        self.assertIn("boss", fired[0][1])

    def test_proactive_alerts_default_on(self):
        self.assertTrue(self.settings.get("proactive_alerts"))
        self.assertTrue(self.settings.public()["proactive_alerts"])


class PersonaContextTests(unittest.TestCase):
    def test_situation_includes_pc_reminders_and_recent_threads(self):
        p = build_system_prompt(
            {"address": "sir", "user_name": "Tony"}, [], [], tools_enabled=True,
            context={"pc": {"cpu_percent": 91.0, "battery_percent": 12, "battery_plugged": False},
                     "reminders": [{"text": "Call Pepper", "due": "Fri 18:00"}],
                     "recent": [{"title": "Demo prep", "when": "yesterday 21:10", "last": "Good luck tomorrow, sir."}]},
        )
        for expected in ("CPU 91%", "battery 12% on battery", "Call Pepper (Fri 18:00)", '"Demo prep"', "their name is Tony", "Acting on the PC"):
            self.assertIn(expected, p)

    def test_offline_agent_is_stated(self):
        p = build_system_prompt({}, [], [], context={"pc": None})
        self.assertIn("Desktop agent: offline", p)
        self.assertIn("can only talk", p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
