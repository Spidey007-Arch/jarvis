"""Plugins: a bundle of skills and connectors in one file, so people can share what they built, plus the community
catalog to find them.

A plugin is a JSON file (format "jarvis-plugin"): a name, a description, and any of
- skills: {name, when_to, body}, added to Customize > Skills;
- connectors: {name, kind: local|web, command, args, url, env_names}, added to Customize > Connectors.

Installing always shows exactly what will be added first, including every command a connector would run. Connectors
from a plugin are added turned off: the user reads them and turns each on, and adds any token it needs, in Connectors.
Removing a plugin removes everything it added.

The catalog is community/catalog.json in the JARVIS repository: anyone can add a plugin with a pull request, and it is
reviewed before it is listed. JARVIS reads it from GitHub, or from the copy in its own folder when offline."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .db import DB

log = logging.getLogger("jarvis.plugins")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugins (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    version      TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    author       TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    skill_ids    TEXT NOT NULL DEFAULT '[]',
    connectors   TEXT NOT NULL DEFAULT '[]',
    manifest     TEXT NOT NULL,
    installed_at REAL NOT NULL
)
"""
FORMAT = "jarvis-plugin"
REPO_RAW = "https://raw.githubusercontent.com/Spidey007-Arch/jarvis/main/community/"
LOCAL = Path(__file__).resolve().parents[2] / "community"
CATALOG_CACHE_S = 3600
MAX_BYTES = 200_000
MAX_SKILLS = 20
MAX_CONNECTORS = 5
# What a plugin's local connector may start: package runners, nothing that is a shell.
RUNNERS = frozenset({"npx", "uvx", "docker", "node", "python", "py"})
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
_CONN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,59}$")
_SHELLISH = re.compile(r"[;&|`$<>]|\r|\n")


class PluginError(Exception):
    pass


def _text(v: Any, limit: int) -> str:
    return str(v or "").strip()[:limit]


def validate(m: Any) -> dict[str, Any]:
    """A clean copy of a plugin manifest, or PluginError saying what is wrong with it."""
    if not isinstance(m, dict) or m.get("format") != FORMAT:
        raise PluginError(f"That isn't a JARVIS plugin (its \"format\" must be \"{FORMAT}\").")
    pid = _text(m.get("id"), 40).lower()
    if not _ID.match(pid):
        raise PluginError("A plugin needs an id of 2-40 lowercase letters, digits and dashes, e.g. morning-routine.")
    name = _text(m.get("name"), 60)
    if not name:
        raise PluginError("A plugin needs a name.")
    homepage = _text(m.get("homepage"), 300)
    if homepage and not homepage.startswith("https://"):
        raise PluginError("A plugin's homepage must be an https:// address.")
    skills = []
    for s in (m.get("skills") or [])[:MAX_SKILLS + 1]:
        if not isinstance(s, dict) or not _text(s.get("name"), 80) or not _text(s.get("body"), 6000):
            raise PluginError("Each skill needs a name and its steps (body).")
        skills.append({"name": _text(s["name"], 80), "when_to": _text(s.get("when_to"), 300), "body": _text(s["body"], 6000)})
    if len(skills) > MAX_SKILLS:
        raise PluginError(f"A plugin can carry at most {MAX_SKILLS} skills.")
    connectors = []
    for c in (m.get("connectors") or [])[:MAX_CONNECTORS + 1]:
        if not isinstance(c, dict):
            raise PluginError("Each connector must be an object.")
        cname = _text(c.get("name"), 30).lower()
        if not _CONN.match(cname):
            raise PluginError("Each connector needs a short name of lowercase letters, digits, - and _.")
        kind = c.get("kind") or "local"
        env_names = [str(e) for e in c.get("env_names") or []]
        if any(not _ENV.match(e) for e in env_names) or len(env_names) > 10:
            raise PluginError(f"Connector '{cname}': env_names must be names like NOTION_TOKEN.")
        if kind == "web":
            url = _text(c.get("url"), 500)
            if not url.startswith("https://"):
                raise PluginError(f"Connector '{cname}': a web connector needs an https:// address.")
            connectors.append({"name": cname, "kind": "web", "command": "", "args": [], "url": url, "env_names": [],
                               "needs_token": bool(c.get("needs_token"))})
        elif kind == "local":
            command = _text(c.get("command"), 40)
            args = [str(a)[:200] for a in c.get("args") or []]
            if command.lower().removesuffix(".exe") not in RUNNERS:
                raise PluginError(f"Connector '{cname}': it may only start with {', '.join(sorted(RUNNERS))}, not '{command}'.")
            if len(args) > 20 or any(_SHELLISH.search(a) for a in [command, *args]):
                raise PluginError(f"Connector '{cname}': its command has characters a plugin may not use.")
            connectors.append({"name": cname, "kind": "local", "command": command, "args": args, "url": "", "env_names": env_names,
                               "needs_token": False})
        else:
            raise PluginError(f"Connector '{cname}': kind must be local or web.")
    if len(connectors) > MAX_CONNECTORS:
        raise PluginError(f"A plugin can carry at most {MAX_CONNECTORS} connectors.")
    if not skills and not connectors:
        raise PluginError("This plugin has no skills or connectors in it.")
    return {"format": FORMAT, "id": pid, "name": name, "version": _text(m.get("version"), 20), "description": _text(m.get("description"), 400),
            "author": _text(m.get("author"), 60), "homepage": homepage, "skills": skills, "connectors": connectors}


def parse(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise PluginError("That plugin file is too large.")
    try:
        return validate(json.loads(text))
    except ValueError as e:
        raise PluginError(f"That isn't valid JSON: {e}") from e


def command_line(c: dict[str, Any]) -> str:
    return " ".join([c["command"], *c["args"]]) if c["kind"] == "local" else c["url"]


class Plugins:
    def __init__(self, db: DB, skills: Any, settings: Any) -> None:
        self.db, self.skills, self.settings = db, skills, settings
        db.run(SCHEMA)
        self._catalog: tuple[float, list[dict[str, Any]], str] | None = None

    def installed(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.all("SELECT * FROM plugins ORDER BY name COLLATE NOCASE"):
            m = json.loads(r["manifest"])
            out.append({k: r[k] for k in ("id", "name", "version", "description", "author", "source", "installed_at")} |
                       {"skills": [s["name"] for s in m["skills"]], "connectors": json.loads(r["connectors"]),
                        "homepage": m.get("homepage", "")})
        return out

    def preview(self, m: dict[str, Any]) -> dict[str, Any]:
        """Exactly what installing would add, and what is in the way."""
        have_skills = {s["name"].lower() for s in self.skills.mine()}
        have_conns = {c["name"] for c in self.settings.get("connectors") or []}
        mine = self.db.one("SELECT skill_ids, connectors FROM plugins WHERE id=?", [m["id"]])
        own_conns = set(json.loads(mine["connectors"])) if mine else set()
        own_skills = {s["name"].lower() for s in self.skills.mine() if s["id"] in (json.loads(mine["skill_ids"]) if mine else [])}
        return {**m, "installed": bool(mine),
                "commands": [{"connector": c["name"], "runs": command_line(c), "env_names": c["env_names"], "needs_token": c["needs_token"]}
                             for c in m["connectors"]],
                "clashes": [f"skill “{s['name']}”" for s in m["skills"] if s["name"].lower() in have_skills - own_skills] +
                           [f"connector “{c['name']}”" for c in m["connectors"] if c["name"] in have_conns - own_conns]}

    def install(self, m: dict[str, Any], source: str) -> tuple[dict[str, Any], list[str]]:
        """Add a plugin's skills and (switched off) connectors. Returns the plugin and the connectors to drop and re-read."""
        p = self.preview(m)
        if p["clashes"]:
            raise PluginError("Already there and not from this plugin: " + ", ".join(p["clashes"]) + ". Rename or remove those first.")
        old = self.db.one("SELECT * FROM plugins WHERE id=?", [m["id"]])
        if old:  # an update: take out what the old version added, then add the new
            self._take_out(old)
        skill_ids = [self.skills.save_mine(s["name"], s["when_to"], s["body"], True)["id"] for s in m["skills"]]
        names = [c["name"] for c in m["connectors"]]
        conns = [c for c in self.settings.get("connectors") or [] if c.get("name") not in names]
        conns += [{"name": c["name"], "kind": c["kind"], "command": c["command"], "args": c["args"], "url": c["url"], "enabled": False,
                   "plugin": m["id"], "env_names": c["env_names"]} for c in m["connectors"]]
        self.settings.set_connectors(conns)
        self.db.run("INSERT OR REPLACE INTO plugins (id, name, version, description, author, source, skill_ids, connectors, manifest, installed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", [m["id"], m["name"], m["version"], m["description"], m["author"], source[:300],
                                                    json.dumps(skill_ids), json.dumps(names), json.dumps(m), time.time()])
        log.info("plugin installed", extra={"fields": {"id": m["id"], "skills": len(skill_ids), "connectors": len(names)}})
        return next(x for x in self.installed() if x["id"] == m["id"]), names

    def _take_out(self, row: dict[str, Any]) -> list[str]:
        for sid in json.loads(row["skill_ids"]):
            self.skills.remove_mine(sid)
        names = json.loads(row["connectors"])
        self.settings.set_connectors([c for c in self.settings.get("connectors") or [] if c.get("name") not in names])
        for n in names:
            self.settings.set_service_key(f"connector:{n}", "")
        return names

    def remove(self, pid: str) -> list[str] | None:
        """Remove a plugin and what it added; returns its connectors' names (to disconnect), None if not installed."""
        row = self.db.one("SELECT * FROM plugins WHERE id=?", [pid])
        if not row:
            return None
        names = self._take_out(row)
        self.db.run("DELETE FROM plugins WHERE id=?", [pid])
        return names

    def manifests(self) -> list[dict[str, Any]]:
        return [{"source": r["source"], **json.loads(r["manifest"])} for r in self.db.all("SELECT source, manifest FROM plugins")]

    # ----- the community catalog -----

    async def _get(self, path: str) -> str:
        if not re.fullmatch(r"(catalog|plugins/[a-z0-9-]+)\.json", path):
            raise PluginError("Not a catalog path.")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                r = await client.get(REPO_RAW + path)
                r.raise_for_status()
                if len(r.content) > MAX_BYTES:
                    raise PluginError("The catalog file is too large.")
                return r.text
        except (httpx.HTTPError, PluginError) as e:
            local = LOCAL / path
            if local.is_file():
                log.info("catalog offline, using the local copy: %s", e)
                return local.read_text(encoding="utf-8")
            raise PluginError(f"Couldn't reach the community catalog: {e}") from e

    async def catalog(self) -> list[dict[str, Any]]:
        if self._catalog and time.time() - self._catalog[0] < CATALOG_CACHE_S:
            items = self._catalog[1]
        else:
            try:
                data = json.loads(await self._get("catalog.json"))
            except ValueError as e:
                raise PluginError(f"The catalog isn't valid JSON: {e}") from e
            items = [{"id": str(p.get("id", "")), "name": _text(p.get("name"), 60), "description": _text(p.get("description"), 400),
                      "author": _text(p.get("author"), 60), "has": [str(x) for x in p.get("has") or []][:4]}
                     for p in data.get("plugins") or [] if _ID.match(str(p.get("id", "")))]
            self._catalog = (time.time(), items, "")
        installed = {r["id"]: r["version"] for r in self.db.all("SELECT id, version FROM plugins")}
        return [{**p, "installed": p["id"] in installed} for p in items]

    async def from_catalog(self, pid: str) -> dict[str, Any]:
        if not _ID.match(pid):
            raise PluginError("Not a plugin id.")
        m = parse(await self._get(f"plugins/{pid}.json"))
        if m["id"] != pid:
            raise PluginError("The catalog entry and its plugin file don't match.")
        return m


def export_skills(skills: list[dict[str, Any]], name: str, author: str) -> dict[str, Any]:
    """The user's own skills as a plugin file they can share."""
    pid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "my-skills"
    return validate({"format": FORMAT, "id": pid if len(pid) >= 2 else "my-skills", "name": name or "My skills", "version": "1.0",
                     "description": f"{len(skills)} skill{'s' if len(skills) != 1 else ''} shared from JARVIS.", "author": author,
                     "skills": [{"name": s["name"], "when_to": s["when_to"], "body": s["body"]} for s in skills]})
