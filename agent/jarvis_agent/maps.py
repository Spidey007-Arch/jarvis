"""Places, near me: where this PC is, and Google Maps searches and directions read back as data.

Location comes from Windows location services when the user has turned them on (accurate to metres), else from
the internet connection's public address (approximate: usually the right city or area), and says which. Maps
work happens in one "maps" tab of JARVIS's own browser window (webdrive.py), reused for every search, with the
tab's location set to the user's so Google's "near me" means here. Results are read from the page: name,
rating, number of reviews and the line Google shows (kind, price, address, open now)."""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any

from . import webdrive, win
from .actions import ActionError, _str, action

_WIN_LOCATION = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Device
$w = New-Object System.Device.Location.GeoCoordinateWatcher([System.Device.Location.GeoPositionAccuracy]::High)
[void]$w.TryStart($false, [TimeSpan]::FromSeconds(3))
$t = 0
while ($w.Status -ne 'Ready' -and $w.Permission -ne 'Denied' -and $t -lt 40) { Start-Sleep -Milliseconds 250; $t++ }
$c = $w.Position.Location
$out = @{ permission = [string]$w.Permission; status = [string]$w.Status }
if (-not $c.IsUnknown) { $out.lat = $c.Latitude; $out.lon = $c.Longitude; $out.accuracy_m = [int]$c.HorizontalAccuracy }
$w.Stop()
$out | ConvertTo-Json -Compress
"""
TURN_ON = "Settings > Privacy & security > Location > turn on Location services and 'Let desktop apps access your location'"
_CACHE: dict[str, Any] = {}
CACHE_S = 600


def _windows_location() -> dict[str, Any] | None:
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WIN_LOCATION], capture_output=True,
                           text=True, timeout=25, creationflags=win.CREATE_NO_WINDOW)
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (subprocess.SubprocessError, ValueError, IndexError, OSError):
        return None
    if "lat" not in out:
        return None
    return {"lat": round(out["lat"], 6), "lon": round(out["lon"], 6), "accuracy_m": out.get("accuracy_m"), "source": "windows"}


def _get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS desktop assistant", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def _network_location() -> dict[str, Any] | None:
    """Approximate location from the connection's public address (city or area)."""
    for url, parse in (
        ("https://ipinfo.io/json", lambda d: (*map(float, d["loc"].split(",")), d.get("city"), d.get("region"), d.get("country"))),
        ("https://ipapi.co/json/", lambda d: (d["latitude"], d["longitude"], d.get("city"), d.get("region"), d.get("country_name"))),
    ):
        try:
            lat, lon, city, region, country = parse(_get_json(url))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        return {"lat": round(lat, 4), "lon": round(lon, 4), "area": ", ".join(x for x in (city, region, country) if x),
                "source": "network", "approximate": True}
    return None


def _place_name(lat: float, lon: float) -> str:
    """A readable address for coordinates, from OpenStreetMap (no key)."""
    try:
        d = _get_json(f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=16&lat={lat}&lon={lon}")
    except (OSError, ValueError):
        return ""
    return str(d.get("display_name") or "")[:160]


def locate() -> dict[str, Any]:
    if _CACHE.get("at", 0) > time.time() - CACHE_S:
        return dict(_CACHE["where"])
    where = _windows_location()
    if where:
        where["area"] = _place_name(where["lat"], where["lon"])
    else:
        where = _network_location()
        if where is None:
            raise ActionError("I couldn't work out where this PC is: Windows location is off and the network lookup failed. "
                              f"Turn it on in {TURN_ON}, or tell me the area.")
        where["note"] = (f"Approximate (from the internet connection, often only the right city or area). For your exact spot, "
                         f"turn on {TURN_ON}.")
    _CACHE.update(at=time.time(), where=where)
    return dict(where)


@action
def my_location(_: dict[str, Any]) -> dict[str, Any]:
    """Where this PC is: coordinates, the area's name, and how exact it is."""
    return locate()


# ---------------- Google Maps in JARVIS's browser ----------------

_PLACES = r"""
(() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/maps/place/"]')) {
    const name = (a.getAttribute('aria-label') || '').trim();
    if (!name || seen.has(name)) continue;
    seen.add(name);
    let card = a.parentElement;
    for (let i = 0; i < 4 && card && (card.innerText || '').length < 40; i++) card = card.parentElement;
    out.push({name, url: a.href.split('?')[0], text: ((card && card.innerText) || '').replace(/\s*\n+\s*/g, ' · ').slice(0, 320)});
  }
  const h1 = document.querySelector('h1');
  const main = document.querySelector('[role="main"]');
  return {url: location.href, places: out.slice(0, 25), heading: h1 ? h1.innerText.trim() : '',
          detail: main ? (main.innerText || '').replace(/\s*\n+\s*/g, ' · ').slice(0, 700) : ''};
})()
"""
_SCROLL = r"""
(() => { const f = document.querySelector('div[role="feed"]'); if (f) f.scrollBy(0, 2400); return !!f; })()
"""
_RATING = re.compile(r"(\d[.,]\d)\s*\(([\d,.]+\s*[KkLl]?)\)")


def _place(raw: dict[str, Any]) -> dict[str, Any]:
    text = raw.get("text") or ""
    m = _RATING.search(text)
    rest = _RATING.sub("", text.replace(raw["name"], "")).strip(" ·")  # the card repeats the name; the rating is its own field
    return {"name": raw["name"], "rating": float(m.group(1).replace(",", ".")) if m else None,
            "reviews": m.group(2).replace(" ", "") if m else None, "details": re.sub(r"(\s*·\s*)+", " · ", rest)[:220],
            "url": raw.get("url")}


def _maps_tab(url: str, where: dict[str, Any] | None) -> None:
    webdrive.ensure_browser()
    with webdrive._Tab("maps") as t:
        t.send("Page.bringToFront")
        if where:  # Google's own "near me" and distances then mean the user's real place
            t.send("Emulation.setGeolocationOverride", {"latitude": where["lat"], "longitude": where["lon"],
                                                        "accuracy": float(where.get("accuracy_m") or 2000)})
            try:
                t.send("Browser.grantPermissions", {"permissions": ["geolocation"], "origin": "https://www.google.com"})
            except webdrive.WebError:
                pass  # older browsers: Maps still centres on the coordinates in the address
        try:
            t.send("Page.navigate", {"url": url})
        except webdrive.WebError:
            pass  # the tab swapped processes while navigating; _settled reconnects
    webdrive._settled(45, "maps")


def _read_places(min_rating: float, limit: int) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    for attempt in range(8):  # results arrive a moment after the page settles
        with webdrive._Tab("maps") as t:
            raw = t.eval(_PLACES) or {}
            if len(raw.get("places") or []) < limit and attempt >= 2:
                t.eval(_SCROLL)  # load more of the list
        if raw.get("places") and (len(raw["places"]) >= limit or attempt >= 4) or raw.get("heading") and attempt >= 3:
            break
        time.sleep(1.0)
    places = [_place(p) for p in raw.get("places") or []]
    if min_rating:
        places = [p for p in places if (p["rating"] or 0) >= min_rating]
    return {"places": places[:limit], "heading": raw.get("heading"), "detail": raw.get("detail") if not places else "",
            "page": raw.get("url")}


@action
def maps_search(args: dict[str, Any]) -> dict[str, Any]:
    """Search Google Maps (near the user by default, or near a named place) and read the results back."""
    query = _str(args, "query").strip()
    near = (_str(args, "near", False) or "me").strip()
    limit = max(1, min(int(args.get("limit") or 10), 20))
    min_rating = float(args.get("min_rating") or 0)
    where = None
    if near.lower() in ("me", "here", "my location", "current location"):
        where = locate()
        url = f"https://www.google.com/maps/search/{urllib.parse.quote(query)}/@{where['lat']},{where['lon']},15z?hl=en"
    else:
        url = f"https://www.google.com/maps/search/{urllib.parse.quote(query + ' near ' + near)}?hl=en"
    _maps_tab(url, where)
    out = _read_places(min_rating, limit)
    if not out["places"] and not out["heading"]:
        raise ActionError("Google Maps showed no results I could read; the Maps tab is open for the user to look at.")
    out.update(searched=query, near=where.get("area") if where else near)
    if where:
        out["location"] = {"source": where["source"], **({"note": where["note"]} if where.get("note") else {})}
    return out


_ROUTE = r"""
(() => {
  const trips = [...document.querySelectorAll('[id^="section-directions-trip-"]')].slice(0, 3)
    .map(e => (e.innerText || '').replace(/\s*\n+\s*/g, ' · ').slice(0, 240));
  return {url: location.href, trips};
})()
"""


@action
def maps_directions(args: dict[str, Any]) -> dict[str, Any]:
    """Directions on Google Maps from the user's location (or `origin`) to `destination`: time and distance per route."""
    dest = _str(args, "destination").strip()
    origin = (_str(args, "origin", False) or "").strip()
    mode = (_str(args, "mode", False) or "driving").lower()
    if mode not in ("driving", "walking", "transit", "bicycling", "two-wheeler"):
        raise ActionError("mode must be driving, walking, transit, bicycling or two-wheeler.")
    where = None
    if not origin or origin.lower() in ("me", "here", "my location"):
        where = locate()
        origin = f"{where['lat']},{where['lon']}"
    url = "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(
        {"api": 1, "origin": origin, "destination": dest, "travelmode": "driving" if mode == "two-wheeler" else mode, "hl": "en"})
    _maps_tab(url, where)
    trips: list[str] = []
    for _ in range(10):
        with webdrive._Tab("maps") as t:
            trips = (t.eval(_ROUTE) or {}).get("trips") or []
        if trips:
            break
        time.sleep(1.0)
    if not trips:
        raise ActionError("Google Maps showed no route I could read; the directions are open in the Maps tab.")
    return {"from": where.get("area") if where else origin, "to": dest, "mode": mode, "routes": trips}
