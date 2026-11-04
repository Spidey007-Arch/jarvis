"""Calendar (ICS reading, repeats, free time, links), heartbeat, plugins and encrypted backup and sync."""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "common")]

from jarvis_backend import backup, heartbeat, permissions  # noqa: E402
from jarvis_backend.automations import Automations  # noqa: E402
from jarvis_backend.calendars import Calendar, CalendarError, clean_link, compose_link, occurrences, parse_ics  # noqa: E402
from jarvis_backend.db import DB  # noqa: E402
from jarvis_backend.memory import MemoryStore  # noqa: E402
from jarvis_backend.persona import build_system_prompt  # noqa: E402
from jarvis_backend.plugins import PluginError, Plugins, export_skills, parse, validate  # noqa: E402
from jarvis_backend.settings import SettingsStore  # noqa: E402
from jarvis_backend.skills import Skills  # noqa: E402
from jarvis_backend.tools import BY_NAME  # noqa: E402

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Standup
DTSTART:20261102T093000
DTEND:20261102T094500
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=6
EXDATE:20261104T093000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Mum's birthday
DTSTART;VALUE=DATE:20261107
END:VEVENT
BEGIN:VEVENT
SUMMARY:Dentist\\, Dr Rao
LOCATION:MG Road
DTSTART:20261105T160000
DURATION:PT30M
END:VEVENT
BEGIN:VEVENT
SUMMARY:Cancelled thing
STATUS:CANCELLED
DTSTART:20261105T100000
END:VEVENT
END:VCALENDAR
"""


class FakeSettings:
    """The SettingsStore interface the features use, in memory."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.secrets: dict = {}

    def get(self, k):
        return json.loads(json.dumps(self.data.get(k)))

    def update_profile(self, values):
        self.data.update(values)

    def service_key(self, name):
        return self.secrets.get(f"service_key:{name.lower()}", "")

    def set_service_key(self, name, value):
        if value:
            self.secrets[f"service_key:{name.lower()}"] = value
        else:
            self.secrets.pop(f"service_key:{name.lower()}", None)

    def set_connectors(self, items):
        self.data["connectors"] = items

    def all_secrets(self):
        return dict(self.secrets)

    def merge_secrets(self, items):
        new = {k: v for k, v in items.items() if self.secrets.get(k) != v}
        self.secrets.update(new)
        return len(new)


class Ics(unittest.TestCase):
    def test_reads_events_and_skips_cancelled(self) -> None:
        events = parse_ics(ICS)
        self.assertEqual([e["title"] for e in events], ["Standup", "Mum's birthday", "Dentist, Dr Rao"])
        dentist = events[2]
        self.assertEqual(dentist["end"] - dentist["start"], timedelta(minutes=30))
        self.assertEqual(dentist["location"], "MG Road")
        self.assertTrue(events[1]["all_day"])
        self.assertEqual(events[1]["end"], datetime(2026, 11, 8))

    def test_weekly_repeats_with_count_and_exception(self) -> None:
        standup = parse_ics(ICS)[0]
        got = occurrences(standup, datetime(2026, 11, 1), datetime(2026, 12, 1))
        # Mon 2, (Wed 4 skipped), Fri 6, Mon 9, Wed 11, Fri 13: six counted, five happen
        self.assertEqual([d.day for d in got], [2, 6, 9, 11, 13])
        self.assertEqual(occurrences(standup, datetime(2026, 11, 7), datetime(2026, 11, 10)), [datetime(2026, 11, 9, 9, 30)])

    def test_utc_times_become_local(self) -> None:
        e = parse_ics("BEGIN:VEVENT\nSUMMARY:x\nDTSTART:20261105T120000Z\nEND:VEVENT\n")[0]
        expected = datetime(2026, 11, 5, 12, 0).replace(tzinfo=__import__("datetime").timezone.utc).astimezone().replace(tzinfo=None)
        self.assertEqual(e["start"], expected)

    def test_links_must_be_https(self) -> None:
        self.assertEqual(clean_link("webcal://p01-calendars.icloud.com/x.ics"), "https://p01-calendars.icloud.com/x.ics")
        with self.assertRaises(CalendarError):
            clean_link("http://example.com/cal.ics")
        with self.assertRaises(CalendarError):
            clean_link("file:///C:/secrets.ics")

    def test_new_event_pages(self) -> None:
        g = urlparse(compose_link("google", "Lunch & chat", datetime(2026, 11, 5, 13), datetime(2026, 11, 5, 14), False, "Cafe", ""))
        self.assertEqual(g.netloc, "calendar.google.com")
        q = parse_qs(g.query)
        self.assertEqual(q["text"], ["Lunch & chat"])
        self.assertEqual(q["dates"], ["20261105T130000/20261105T140000"])
        o = parse_qs(urlparse(compose_link("outlook_web", "Trip", datetime(2026, 11, 7), datetime(2026, 11, 8), True, "", "")).query)
        self.assertEqual(o["allday"], ["true"])


class CalendarStore(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dir.name) / "t.db")
        self.settings = FakeSettings()
        self.cal = Calendar(self.db, self.settings, bridge=None)

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def test_own_events_free_time_and_linked(self) -> None:
        day = date.today() + timedelta(days=3)
        at = lambda h, m=0: datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m)  # noqa: E731
        self.cal.add_local("Review", at(10), at(11), False)
        self.cal.add_local("Gym", at(13), at(14, 30), False)
        self.cal.add_local("Holiday", at(0), at(0) + timedelta(days=1), True)
        url = "https://calendar.example/private-abc/basic.ics"
        self.cal.link("Work", url)
        self.assertEqual(self.settings.service_key("calendar:Work"), url)  # a secret, not a setting
        self.assertNotIn(url, json.dumps(self.settings.data))
        self.cal._cache[url] = (time.time(), parse_ics(f"BEGIN:VEVENT\nSUMMARY:Call\nDTSTART:{day:%Y%m%d}T150000\nDTEND:{day:%Y%m%d}T153000\nEND:VEVENT\n"))

        listing = asyncio.run(self.cal.events(at(0), at(0) + timedelta(days=1)))
        self.assertEqual([(e["title"], e["calendar"]) for e in listing["events"]],
                         [("Holiday", "JARVIS"), ("Review", "JARVIS"), ("Gym", "JARVIS"), ("Call", "Work")])
        free = asyncio.run(self.cal.free_time(day, 9, 18, 30))
        self.assertEqual(free["free"], [{"from": "09:00", "to": "10:00"}, {"from": "11:00", "to": "13:00"},
                                        {"from": "14:30", "to": "15:00"}, {"from": "15:30", "to": "18:00"}])
        self.assertEqual(free["all_day"], ["Holiday"])
        self.assertTrue(self.cal.unlink("work"))
        self.assertEqual(self.settings.service_key("calendar:Work"), "")

    def test_permissions(self) -> None:
        spec = BY_NAME["calendar_add"]
        self.assertEqual(permissions.capability(spec, {"calendar": "jarvis"}), "calendar")
        self.assertIsNone(permissions.capability(spec, {"calendar": "google"}))  # only opens a page the user saves
        self.assertEqual(permissions.decide(spec, {"calendar": "google"}, read_private=True)[1], "send_data_out")
        self.assertIsNone(permissions.capability(BY_NAME["calendar_events"], {}))


class Beats(unittest.TestCase):
    def test_config_quiet_hours_and_all_clear(self) -> None:
        c = heartbeat.config({"every_min": 5, "checks": ["  mail ", "", "disk"]})
        self.assertEqual((c["every_min"], c["checks"]), (15, ["mail", "disk"]))
        self.assertTrue(heartbeat.quiet(c, datetime(2026, 11, 5, 23, 30)))
        self.assertTrue(heartbeat.quiet(c, datetime(2026, 11, 5, 6, 59)))
        self.assertFalse(heartbeat.quiet(c, datetime(2026, 11, 5, 7, 0)))
        self.assertTrue(heartbeat.is_clear("ALL_CLEAR"))
        self.assertTrue(heartbeat.is_clear("all_clear."))
        self.assertFalse(heartbeat.is_clear("Your battery is at 12% and not charging."))

    def test_speaks_only_when_needed_and_not_twice(self) -> None:
        d = tempfile.TemporaryDirectory()
        db = DB(Path(d.name) / "t.db")
        settings = FakeSettings()
        settings.data["heartbeat"] = {"checks": ["battery"]}
        hb = heartbeat.Heartbeat(settings, db, away=lambda: False, busy=lambda: False)
        replies, prompts, said = ["ALL_CLEAR", "Battery is at 12%."], [], []

        async def runner(text, conv):
            prompts.append(text)
            return {"status": "done", "result": replies.pop(0), "conversation_id": None}

        async def say(text):
            said.append(text)

        hb.runner, hb.say = runner, say
        self.assertEqual(asyncio.run(hb.beat())["status"], "clear")
        self.assertEqual(said, [])
        self.assertEqual(asyncio.run(hb.beat())["status"], "alert")
        self.assertEqual(said, ["Battery is at 12%."])
        self.assertIn("Battery is at 12%", hb.prompt(hb.cfg(), time.time()))  # told: not repeated unless it changed
        self.assertEqual(hb.why_not(), "not yet")
        settings.data["heartbeat"]["enabled"] = False
        self.assertEqual(hb.why_not(), "off")
        db.close()
        d.cleanup()


class PluginFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.dir.name) / "t.db")
        self.settings = FakeSettings()
        self.skills = Skills(self.db)
        self.plugins = Plugins(self.db, self.skills, self.settings)

    def tearDown(self) -> None:
        self.db.close()
        self.dir.cleanup()

    def test_starter_plugins_are_valid(self) -> None:
        catalog = json.loads((ROOT / "community" / "catalog.json").read_text(encoding="utf-8"))
        for entry in catalog["plugins"]:
            m = parse((ROOT / "community" / "plugins" / f"{entry['id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(m["id"], entry["id"])

    def test_refuses_unsafe_connectors(self) -> None:
        base = {"format": "jarvis-plugin", "id": "x-plugin", "name": "X"}
        for bad in ({"name": "a", "kind": "local", "command": "powershell", "args": ["-c", "whoami"]},
                    {"name": "a", "kind": "local", "command": "npx", "args": ["pkg", "&&", "del"]},
                    {"name": "a", "kind": "local", "command": "npx", "args": ["$(evil)"]},
                    {"name": "a", "kind": "web", "url": "http://plain.example/mcp"}):
            with self.assertRaises(PluginError):
                validate({**base, "connectors": [bad]})
        with self.assertRaises(PluginError):
            validate({**base})  # nothing in it
        with self.assertRaises(PluginError):
            validate({"format": "other", "id": "x", "name": "X"})

    def test_install_shows_first_adds_off_and_removes_everything(self) -> None:
        m = validate({"format": "jarvis-plugin", "id": "demo", "name": "Demo",
                      "skills": [{"name": "Say hi", "when_to": "greet", "body": "1. Say hi"}],
                      "connectors": [{"name": "notes", "kind": "local", "command": "npx", "args": ["-y", "notes-mcp"], "env_names": ["NOTES_TOKEN"]}]})
        p = self.plugins.preview(m)
        self.assertEqual(p["commands"][0]["runs"], "npx -y notes-mcp")
        self.assertEqual(p["clashes"], [])
        plugin, names = self.plugins.install(m, "file")
        self.assertEqual(names, ["notes"])
        self.assertEqual([s["name"] for s in self.skills.mine()], ["Say hi"])
        self.assertFalse(self.settings.data["connectors"][0]["enabled"])  # the user turns it on after reading it
        self.plugins.install(m, "file")  # installing again updates in place
        self.assertEqual(len(self.skills.mine()), 1)
        self.assertEqual(self.plugins.remove("demo"), ["notes"])
        self.assertEqual(self.skills.mine(), [])
        self.assertEqual(self.settings.data["connectors"], [])

    def test_clash_with_own_skill(self) -> None:
        self.skills.save_mine("Say hi", "", "my own")
        m = validate({"format": "jarvis-plugin", "id": "demo", "name": "Demo", "skills": [{"name": "Say hi", "body": "x"}]})
        with self.assertRaises(PluginError):
            self.plugins.install(m, "file")
        self.assertEqual(self.skills.mine()[0]["body"], "my own")

    def test_share_my_skills(self) -> None:
        self.skills.save_mine("Morning setup", "start my day", "1. Open Outlook")
        m = export_skills(self.skills.mine(), "Parth's routines", "Parth")
        self.assertEqual(m["id"], "parth-s-routines")
        self.assertEqual(parse(json.dumps(m))["skills"][0]["name"], "Morning setup")


class Fixture:
    """One PC's worth of stores, for backup and sync tests."""

    def __init__(self, root: Path, name: str) -> None:
        self.dir = root / name
        self.dir.mkdir()
        self.settings = SettingsStore(self.dir / "settings.dat")
        self.db = DB(self.dir / "jarvis.db")
        self.memory = MemoryStore(self.db, llm=None)
        self.skills = Skills(self.db)
        self.automations = Automations(self.db)
        self.calendar = Calendar(self.db, self.settings, bridge=None)
        self.plugins = Plugins(self.db, self.skills, self.settings)
        self.backup = backup.Backup(settings=self.settings, db=self.db, memory=self.memory, skills=self.skills, automations=self.automations,
                                    calendar=self.calendar, plugins=self.plugins)
        self.db.run("INSERT OR REPLACE INTO meta (key, value) VALUES ('device', ?)", [json.dumps({"id": name, "name": name})])

    def close(self) -> None:
        self.db.close()


class Backups(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # runs last, after each PC's database is closed
        self.root = Path(self.tmp.name)

    def test_encryption(self) -> None:
        blob = backup.encrypt({"device": {"name": "PC"}, "made_at": 1, "x": "secret value"}, "correct horse")
        self.assertNotIn(b"secret value", blob)
        self.assertEqual(backup.decrypt(blob, "correct horse")["x"], "secret value")
        with self.assertRaises(backup.BackupError):
            backup.decrypt(blob, "wrong horse!")
        f = json.loads(blob)
        f["made_at"] = 2  # the readable header is protected too
        with self.assertRaises(backup.BackupError):
            backup.decrypt(json.dumps(f).encode(), "correct horse")
        with self.assertRaises(backup.BackupError):
            backup.encrypt({}, "short")

    def test_backup_and_restore_with_and_without_keys(self) -> None:
        a, b = Fixture(self.root, "a"), Fixture(self.root, "b")
        self.addCleanup(b.close)
        self.addCleanup(a.close)
        a.settings.update_profile({"instructions": "Keep it short.", "language": "hi"})
        a.settings.set_api_key("gemini", "AIza-real-looking-key-123456")
        a.memory.import_items([{"type": "preference", "subject": "music", "content": "Likes lo-fi while working"}])
        a.skills.save_mine("Morning setup", "start my day", "1. Open Outlook")
        a.automations.save("Disk watch", "Check the drives", "interval", every_min=60)
        a.calendar.add_local("Dentist", datetime(2027, 1, 5, 16), datetime(2027, 1, 5, 17), False)

        path = self.root / "x.jarvisbackup"
        a.backup.write(path, "passphrase-1", include_secrets=False)
        self.assertNotIn(b"lo-fi", path.read_bytes())
        got = b.backup.read(path, "passphrase-1")
        self.assertEqual((got["memories"], got["skills"], got["automations"], got["calendar"], got["secrets"]), (1, 1, 1, 1, 0))
        self.assertEqual(b.settings.get("instructions"), "Keep it short.")
        self.assertEqual(b.settings.get("language"), "hi")
        self.assertEqual(b.settings.api_key("gemini"), "")
        a.backup.write(path, "passphrase-1", include_secrets=True)
        self.assertEqual(b.backup.read(path, "passphrase-1")["secrets"], 1)
        self.assertEqual(b.settings.api_key("gemini"), "AIza-real-looking-key-123456")
        self.assertEqual(b.backup.read(path, "passphrase-1")["memories"], 0)  # bringing it in twice adds nothing

    def test_sync_through_a_folder(self) -> None:
        folder = self.root / "OneDrive" / "JARVIS"
        folder.mkdir(parents=True)  # Settings creates it when sync is turned on
        a, b = Fixture(self.root, "a"), Fixture(self.root, "b")
        self.addCleanup(b.close)
        self.addCleanup(a.close)
        for pc in (a, b):
            pc.settings.set_service_key(backup.PASS_KEY, "same passphrase")
            pc.settings.update_profile({"sync": {"enabled": True, "folder": str(folder)}})
        a.memory.import_items([{"type": "preference", "subject": "tea", "content": "Prefers green tea"}])
        self.assertTrue(a.backup.sync_once()["wrote"])
        self.assertEqual(b.backup.sync_once()["brought"].get("memories"), 1)
        self.assertEqual([m["content"] for m in b.memory.export()], ["Prefers green tea"])
        self.assertEqual(len(list(folder.glob("*.jarvisbackup"))), 2)  # one file per PC
        self.assertNotIn("same passphrase", json.dumps(b.backup.snapshot(True)["secrets"]))

        # deleted on b: not brought back from a's (older) file
        mid = b.db.one("SELECT id FROM memories")["id"]
        b.memory.delete(mid)
        b.backup.sync_once()
        a.skills.save_mine("New skill", "", "1. x")  # a changes something, so it writes a newer file
        a.backup.sync_once()
        b.backup.sync_once()
        self.assertEqual(b.memory.export(), [])
        self.assertEqual([s["name"] for s in b.skills.mine()], ["New skill"])


class Hindi(unittest.TestCase):
    def test_persona_answers_in_hindi_when_chosen(self) -> None:
        self.assertIn("Hindi", build_system_prompt({"language": "hi"}, [], []))
        self.assertNotIn("chose Hindi", build_system_prompt({"language": "en"}, [], []))


if __name__ == "__main__":
    unittest.main()
