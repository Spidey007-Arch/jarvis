import unittest
from datetime import datetime

from jarvis_backend import briefing

STATUS = {
    "cpu_percent": 23.4, "cpu_cores": 8, "process_count": 341,
    "memory": {"used_percent": 71.0, "used_gb": 11.2, "total_gb": 15.8},
    "disks": [{"drive": "C:\\", "used_percent": 82.0, "free_gb": 85.1}],
    "battery": {"percent": 64, "charging": False, "minutes_left": 190},
    "network_kb_per_s": {"down": 12.0, "up": 3.1},
    "uptime_hours": 5.2,
    "open_apps": [{"name": "WhatsApp.Root", "windows": 1, "memory_mb": 417}, {"name": "brave", "windows": 2, "memory_mb": 410}],
    "top_cpu": [{"name": "brave.exe", "cpu_percent": 6.2}, {"name": "idle.exe", "cpu_percent": 0.2}],
    "top_memory": [{"name": "MsMpEng.exe", "memory_mb": 600}],
    "flags": [],
}
NOW = datetime(2026, 10, 25, 9, 30)


class Facts(unittest.TestCase):
    def test_covers_system_and_processes(self):
        text = briefing.facts(STATUS, [{"text": "Call mum", "due": "Sun 25 Oct 18:00"}], [], None, "today at 08:10", NOW)
        for want in ("CPU 23%", "memory 71%", "Drive C:\\ 82% full", "Battery 64%, on battery, about 190 minutes left",
                     "341 processes", "WhatsApp (417 MB)", "brave (410 MB)", "brave.exe 6.2%", "MsMpEng.exe 600 MB",
                     "Call mum", "last used today at 08:10"):
            self.assertIn(want, text)
        self.assertNotIn("idle.exe", text)  # under 1% is not worth mentioning

    def test_agent_offline(self):
        text = briefing.facts(None, [], [], None, None, NOW)
        self.assertIn("agent is not connected", text)
        self.assertIn("No pending reminders", text)

    def test_quota_out(self):
        quota = {"models": [{"model": "gemini-3.8-flash", "used": 40, "state": "out"}, {"model": "lite", "used": 2, "state": "ready"}]}
        self.assertIn("AI requests made today: 42. Out of quota: gemini-3.8-flash.", briefing.facts(STATUS, [], [], quota, None, NOW))


class Spoken(unittest.TestCase):
    def test_greeting_and_apps(self):
        text = briefing.spoken(STATUS, [], "sir", NOW)
        self.assertTrue(text.startswith("Good morning, sir. It's 09:30."))
        self.assertIn("WhatsApp, brave open", text)
        self.assertIn("MsMpEng.exe is using the most memory", text)
        self.assertTrue(text.endswith("What do you need?"))

    def test_part_of_day(self):
        self.assertEqual(briefing.part_of_day(datetime(2026, 1, 1, 23)), "night")
        self.assertEqual(briefing.part_of_day(datetime(2026, 1, 1, 18)), "evening")

    def test_opening_prompt_speaks_first(self):
        p = briefing.opening_prompt("REPORT", "sir", NOW)
        self.assertIn("greet sir for the morning", p)
        self.assertTrue(p.endswith("REPORT"))


if __name__ == "__main__":
    unittest.main()
