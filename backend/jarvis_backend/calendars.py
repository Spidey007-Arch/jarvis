"""Calendar: read, plan and add events, with no API key and no sign-in of JARVIS's own.

Sources, merged into one view:
- JARVIS's own calendar, kept on this PC (SQLite). Always there.
- Calendars added by their private address (iCal / .ics link): Google Calendar's "Secret address in iCal format",
  Outlook.com's "Publish calendar" ICS link, iCloud, Proton... Read-only. The address works like a password, so it
  is kept with the other secrets (encrypted, scrubbed from anything the AI sees), never in plain settings.
- Classic Outlook on this PC, through the desktop agent (outlook.py), when the user turns it on.

Adding goes to JARVIS's calendar or Outlook directly; for Google or Outlook on the web it opens the calendar's own
"new event" page with everything filled in, and the user presses Save there."""
from __future__ import annotations

import logging
import re
import time
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from .db import DB

log = logging.getLogger("jarvis.calendar")

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_events (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    start      REAL NOT NULL,
    end        REAL NOT NULL,
    all_day    INTEGER NOT NULL DEFAULT 0,
    location   TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cal_start ON calendar_events(start);
"""
CACHE_S = 600  # a linked calendar is fetched at most this often
MAX_ICS_BYTES = 5_000_000
MAX_SPAN_DAYS = 400
MAX_LINKED = 10
TARGETS = ("jarvis", "outlook", "google", "outlook_web")
_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


class CalendarError(Exception):
    pass


# ---------------- iCalendar (.ics) ----------------

def _unescape(v: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def _zone(tzid: str | None):
    if not tzid:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tzid.strip('"'))
    except Exception:  # noqa: BLE001 - no tz database or a Windows zone name: read the time as local
        return None


def _when(value: str, params: dict[str, str]) -> tuple[datetime, bool]:
    """(local naive datetime, all-day) for a DTSTART/DTEND/EXDATE value."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d"), True
    utc = value.endswith("Z")
    dt = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None), False
    zone = _zone(params.get("TZID"))
    if zone is not None:
        return dt.replace(tzinfo=zone).astimezone().replace(tzinfo=None), False
    return dt, False  # floating time: the same wall-clock time everywhere


def _lines(text: str) -> list[tuple[str, dict[str, str], str]]:
    out = []
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    for line in unfolded.splitlines():
        head, sep, value = line.partition(":")
        if not sep:
            continue
        name, *raw = head.split(";")
        params = {}
        for p in raw:
            k, _, v = p.partition("=")
            params[k.upper()] = v
        out.append((name.upper(), params, value))
    return out


def parse_ics(text: str) -> list[dict[str, Any]]:
    """The VEVENTs of a calendar file: title, start/end (local naive datetimes), all_day, location, rrule, exdates."""
    events, cur = [], None
    for name, params, value in _lines(text):
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur = {"title": "", "location": "", "rrule": "", "exdates": set(), "status": ""}
        elif name == "END" and value.upper() == "VEVENT" and cur is not None:
            if "start" in cur and cur["status"] != "CANCELLED":
                if "end" not in cur:
                    cur["end"] = cur["start"] + (timedelta(days=1) if cur["all_day"] else cur.get("duration", timedelta(hours=1)))
                events.append(cur)
            cur = None
        elif cur is None:
            continue
        elif name == "SUMMARY":
            cur["title"] = _unescape(value)
        elif name == "LOCATION":
            cur["location"] = _unescape(value)
        elif name == "STATUS":
            cur["status"] = value.upper()
        elif name == "DTSTART":
            cur["start"], cur["all_day"] = _when(value, params)
        elif name == "DTEND":
            cur["end"], _ = _when(value, params)
        elif name == "DURATION":
            m = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
            if m:
                w, d, h, mi, s = (int(x or 0) for x in m.groups())
                cur["duration"] = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
        elif name == "RRULE":
            cur["rrule"] = value
        elif name == "EXDATE":
            for v in value.split(","):
                cur["exdates"].add(_when(v, params)[0])
    for e in events:
        if "duration" in e and e["end"] == e["start"]:
            e["end"] = e["start"] + e["duration"]
    return events


def _add_months(d: datetime, n: int) -> datetime | None:
    y, m = divmod(d.month - 1 + n, 12)
    try:
        return d.replace(year=d.year + y, month=m + 1)
    except ValueError:  # the 31st in a shorter month: that month has no occurrence
        return None


def occurrences(e: dict[str, Any], start: datetime, end: datetime, limit: int = 1000) -> list[datetime]:
    """Start times of an event (expanding a simple RRULE) that overlap [start, end)."""
    length = e["end"] - e["start"]
    if not e["rrule"]:
        return [e["start"]] if e["start"] < end and e["start"] + length > start else []
    rule = dict(p.split("=", 1) for p in e["rrule"].upper().split(";") if "=" in p)
    freq, step = rule.get("FREQ", ""), max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = _when(rule["UNTIL"], {})[0] if rule.get("UNTIL") else None
    bydays = [_DAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in _DAYS] if freq == "WEEKLY" else []
    out, n, i = [], 0, 0
    while i < limit * 4:
        if freq == "DAILY":
            batch = [e["start"] + timedelta(days=i * step)]
        elif freq == "WEEKLY":
            week = e["start"] + timedelta(weeks=i * step)
            monday = week - timedelta(days=week.weekday())
            batch = sorted(monday + timedelta(days=d) for d in bydays) if bydays else [week]
            batch = [b.replace(hour=e["start"].hour, minute=e["start"].minute, second=e["start"].second) for b in batch]
        elif freq == "MONTHLY":
            batch = [x for x in [_add_months(e["start"], i * step)] if x]
        elif freq == "YEARLY":
            batch = [x for x in [_add_months(e["start"], 12 * i * step)] if x]
        else:
            return [e["start"]] if e["start"] < end and e["start"] + length > start else []
        i += 1
        for s in batch:
            if s < e["start"]:
                continue
            if (until and s > until) or (count is not None and n >= count) or s >= end:
                return out
            n += 1
            if s + length > start and s not in e["exdates"]:
                out.append(s)
                if len(out) >= limit:
                    return out
    return out


# ---------------- links that open a calendar's own "new event" page ----------------

def _stamp(d: datetime, all_day: bool) -> str:
    return d.strftime("%Y%m%d") if all_day else d.strftime("%Y%m%dT%H%M%S")


def compose_link(target: str, title: str, start: datetime, end: datetime, all_day: bool, location: str, notes: str) -> str:
    if target == "google":
        q = {"action": "TEMPLATE", "text": title, "dates": f"{_stamp(start, all_day)}/{_stamp(end, all_day)}"}
        if location:
            q["location"] = location
        if notes:
            q["details"] = notes
        return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(q)
    q = {"path": "/calendar/action/compose", "rru": "addevent", "subject": title, "startdt": start.isoformat(timespec="seconds"),
         "enddt": end.isoformat(timespec="seconds"), "allday": "true" if all_day else "false"}
    if location:
        q["location"] = location
    if notes:
        q["body"] = notes
    return "https://outlook.live.com/calendar/0/deeplink/compose?" + urllib.parse.urlencode(q)


def parse_time(value: str, field: str = "time") -> datetime:
    try:
        d = datetime.fromisoformat(str(value).strip())
    except ValueError as e:
        raise CalendarError(f"'{value}' is not a {field} like 2026-11-05T15:30 or 2026-11-05.") from e
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def _fmt(d: datetime, all_day: bool) -> str:
    return d.strftime("%Y-%m-%d") if all_day else d.strftime("%Y-%m-%dT%H:%M")


def clean_link(url: str) -> str:
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    if not url.lower().startswith("https://"):
        raise CalendarError("A calendar address must start with https:// (or webcal://).")
    return url


class Calendar:
    def __init__(self, db: DB, settings: Any, bridge: Any) -> None:
        self.db, self.settings, self.bridge = db, settings, bridge
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.run(statement)
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    # ----- linked calendars (by private address) -----

    def linked(self) -> list[dict[str, Any]]:
        return [{"name": c["name"], "has_link": bool(self.settings.service_key(f"calendar:{c['name']}"))}
                for c in self.settings.get("calendars") or []]

    def link(self, name: str, url: str) -> list[dict[str, Any]]:
        name = re.sub(r"\s+", " ", name).strip()[:40]
        if not name:
            raise CalendarError("Give the calendar a name, e.g. Work or Family.")
        url = clean_link(url)
        items = [c for c in self.settings.get("calendars") or [] if c["name"].lower() != name.lower()]
        if len(items) >= MAX_LINKED:
            raise CalendarError(f"{MAX_LINKED} calendars are linked already; remove one first.")
        self.settings.set_service_key(f"calendar:{name}", url)
        self.settings.update_profile({"calendars": [*items, {"name": name}]})
        self._cache.pop(url, None)
        return self.linked()

    def unlink(self, name: str) -> bool:
        items = self.settings.get("calendars") or []
        keep = [c for c in items if c["name"].lower() != name.lower()]
        for c in items:
            if c not in keep:
                self.settings.set_service_key(f"calendar:{c['name']}", "")
        self.settings.update_profile({"calendars": keep})
        return len(keep) < len(items)

    async def _fetch(self, url: str) -> list[dict[str, Any]]:
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "JARVIS calendar"}) as client:
            async with client.stream("GET", url) as r:
                if r.status_code in (401, 403, 404):
                    raise CalendarError("the address was refused (it may have been reset: copy the new one)")
                r.raise_for_status()
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_ICS_BYTES:
                        raise CalendarError("the calendar is too large to read")
        if not str(r.url).lower().startswith("https://"):
            raise CalendarError("the address redirected away from https")
        text = body.decode("utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in text[:2000].upper():
            raise CalendarError("that address doesn't return a calendar (.ics)")
        events = parse_ics(text)
        self._cache[url] = (time.time(), events)
        return events

    # ----- JARVIS's own calendar -----

    def add_local(self, title: str, start: datetime, end: datetime, all_day: bool, location: str = "", notes: str = "") -> dict[str, Any]:
        eid, now = uuid.uuid4().hex[:10], time.time()
        self.db.run("INSERT INTO calendar_events (id, title, start, end, all_day, location, notes, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", [eid, title, start.timestamp(), end.timestamp(), int(all_day), location, notes, now, now])
        return {"id": eid, "title": title, "start": _fmt(start, all_day), "end": _fmt(end, all_day), "all_day": all_day, "calendar": "JARVIS"}

    def delete_local(self, eid: str) -> bool:
        return self.db.run("DELETE FROM calendar_events WHERE id=?", [eid]) > 0

    def all_local(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM calendar_events ORDER BY start")

    def put_local(self, rows: list[dict[str, Any]]) -> int:
        """Bring in events from a backup or another PC: new ones are added, newer edits win."""
        n = 0
        for e in rows:
            have = self.db.one("SELECT updated_at FROM calendar_events WHERE id=?", [e.get("id")])
            if have and have["updated_at"] >= float(e.get("updated_at") or 0):
                continue
            self.db.run("INSERT OR REPLACE INTO calendar_events (id, title, start, end, all_day, location, notes, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", [str(e["id"])[:20], str(e["title"])[:200], float(e["start"]), float(e["end"]),
                                                        int(bool(e.get("all_day"))), str(e.get("location") or "")[:200],
                                                        str(e.get("notes") or "")[:2000], float(e.get("created_at") or time.time()),
                                                        float(e.get("updated_at") or time.time())])
            n += 1
        return n

    # ----- everything together -----

    async def events(self, start: datetime, end: datetime, query: str = "") -> dict[str, Any]:
        if end <= start:
            raise CalendarError("The end of the range must be after its start.")
        if (end - start).days > MAX_SPAN_DAYS:
            raise CalendarError(f"Ask for at most {MAX_SPAN_DAYS} days at a time.")
        found: list[dict[str, Any]] = []
        problems: list[str] = []
        for r in self.db.all("SELECT * FROM calendar_events WHERE start < ? AND end > ? ORDER BY start", [end.timestamp(), start.timestamp()]):
            a, b = datetime.fromtimestamp(r["start"]), datetime.fromtimestamp(r["end"])
            found.append({"id": r["id"], "title": r["title"], "start": _fmt(a, bool(r["all_day"])), "end": _fmt(b, bool(r["all_day"])),
                          "all_day": bool(r["all_day"]), "location": r["location"], "calendar": "JARVIS", "_s": a})
        for c in self.settings.get("calendars") or []:
            url = self.settings.service_key(f"calendar:{c['name']}")
            if not url:
                continue
            try:
                for e in await self._fetch(url):
                    for s in occurrences(e, start, end):
                        f = s + (e["end"] - e["start"])
                        found.append({"title": e["title"] or "(no title)", "start": _fmt(s, e["all_day"]), "end": _fmt(f, e["all_day"]),
                                      "all_day": e["all_day"], "location": e["location"], "calendar": c["name"], "_s": s})
            except (CalendarError, httpx.HTTPError, ValueError) as err:
                problems.append(f"{c['name']}: {err}")
        if self.settings.get("calendar_outlook"):
            try:
                rows = (await self.bridge.call("outlook_events", {"from": start.isoformat(timespec="seconds"),
                                                                  "to": end.isoformat(timespec="seconds")}, timeout=70)).get("events", [])
                for e in rows:
                    s = datetime.fromisoformat(e["start"])
                    found.append({"title": e.get("title") or "(no title)", "start": _fmt(s, bool(e.get("all_day"))),
                                  "end": _fmt(datetime.fromisoformat(e["end"]), bool(e.get("all_day"))), "all_day": bool(e.get("all_day")),
                                  "location": e.get("location") or "", "calendar": "Outlook", "_s": s})
            except Exception as err:  # noqa: BLE001 - one source failing must not hide the others
                problems.append(f"Outlook: {err}")
        q = query.strip().lower()
        if q:
            found = [e for e in found if q in e["title"].lower() or q in (e["location"] or "").lower()]
        found.sort(key=lambda e: e.pop("_s"))
        return {"from": _fmt(start, False), "to": _fmt(end, False), "events": found[:300], "count": len(found), "problems": problems}

    async def free_time(self, day: date, start_hour: int = 9, end_hour: int = 18, min_minutes: int = 30) -> dict[str, Any]:
        """Gaps of at least min_minutes between start_hour and end_hour on a day, around timed events."""
        a = datetime.combine(day, datetime.min.time()).replace(hour=max(0, min(start_hour, 23)))
        b = datetime.combine(day, datetime.min.time()) + timedelta(hours=max(start_hour + 1, min(end_hour, 24)))
        listing = await self.events(a, b)
        busy = sorted((parse_time(e["start"]), parse_time(e["end"])) for e in listing["events"] if not e["all_day"])
        free, cursor = [], a
        for s, f in busy:
            if s > cursor and (s - cursor).total_seconds() >= min_minutes * 60:
                free.append({"from": cursor.strftime("%H:%M"), "to": s.strftime("%H:%M")})
            cursor = max(cursor, f)
        if b > cursor and (b - cursor).total_seconds() >= min_minutes * 60:
            free.append({"from": cursor.strftime("%H:%M"), "to": b.strftime("%H:%M")})
        return {"day": day.isoformat(), "free": free, "busy": [e for e in listing["events"] if not e["all_day"]],
                "all_day": [e["title"] for e in listing["events"] if e["all_day"]], "problems": listing["problems"]}
