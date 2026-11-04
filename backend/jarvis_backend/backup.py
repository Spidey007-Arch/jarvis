"""Encrypted backup and sync of memory and settings between the user's PCs.

A backup holds the profile and preferences, Customize > Instructions, memories, the user's skills, automations,
JARVIS's own calendar, installed plugins and the heartbeat list; keys and tokens only when the user asks for them.
It is compressed and encrypted with a passphrase only the user knows (scrypt, then AES-256-GCM): without it the file
is unreadable, and any change to the file is detected.

Sync needs no server or account: the user picks a folder that their own cloud drive already syncs (OneDrive, Google
Drive, Dropbox) or a USB stick. Each PC writes its own file there and reads the others', so two PCs never write the
same file. Bringing changes in adds and updates; something deleted on this PC isn't brought back from another, and
the newest settings win."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
import zlib
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.backup")

FORMAT = "jarvis-backup"
SUFFIX = ".jarvisbackup"
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSPHRASE = 8
SYNC_EVERY_S = 600
PASS_KEY = "sync:passphrase"  # kept with the other secrets (service_key:sync:passphrase), never written to a backup
PROFILE = ("user_name", "address", "proactive_alerts", "startup_briefing", "presence_greetings", "followups", "instructions",
           "language", "heartbeat", "permissions", "calendar_outlook")


class BackupError(Exception):
    pass


def _key(passphrase: str, salt: bytes, kdf: dict[str, int]) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=kdf["n"], r=kdf["r"], p=kdf["p"], dklen=32, maxmem=128 * 1024 * 1024)


def encrypt(data: dict[str, Any], passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    head = {"format": FORMAT, "version": 1, "kdf": {"name": "scrypt", **SCRYPT, "salt": base64.b64encode(salt).decode()},
            "nonce": base64.b64encode(nonce).decode(), "device": data.get("device", {}), "made_at": data.get("made_at")}
    aad = json.dumps(head, sort_keys=True).encode()  # the readable header can't be changed either
    sealed = AESGCM(_key(passphrase, salt, SCRYPT)).encrypt(nonce, zlib.compress(json.dumps(data).encode("utf-8"), 9), aad)
    return json.dumps({**head, "data": base64.b64encode(sealed).decode()}).encode("utf-8")


def peek(blob: bytes) -> dict[str, Any]:
    """The readable header: which PC made it and when (nothing private)."""
    try:
        f = json.loads(blob.decode("utf-8"))
    except ValueError as e:
        raise BackupError("That isn't a JARVIS backup file.") from e
    if not isinstance(f, dict) or f.get("format") != FORMAT:
        raise BackupError("That isn't a JARVIS backup file.")
    if f.get("version") != 1:
        raise BackupError("That backup was made by a newer JARVIS; update this one first.")
    return f


def decrypt(blob: bytes, passphrase: str) -> dict[str, Any]:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    f = peek(blob)
    head = {k: v for k, v in f.items() if k != "data"}
    kdf = f["kdf"]
    params = {"n": int(kdf["n"]), "r": int(kdf["r"]), "p": int(kdf["p"])}
    if params["n"] > 2 ** 20 or params["r"] > 16 or params["p"] > 4:
        raise BackupError("That backup asks for unreasonable settings; it wasn't made by JARVIS.")
    try:
        plain = AESGCM(_key(passphrase, base64.b64decode(kdf["salt"]), params)).decrypt(
            base64.b64decode(f["nonce"]), base64.b64decode(f["data"]), json.dumps(head, sort_keys=True).encode())
    except InvalidTag as e:
        raise BackupError("Wrong passphrase, or the file was changed.") from e
    return json.loads(zlib.decompress(plain).decode("utf-8"))


def _h(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


class Backup:
    def __init__(self, *, settings: Any, db: Any, memory: Any, skills: Any, automations: Any, calendar: Any, plugins: Any) -> None:
        self.settings, self.db, self.memory, self.skills = settings, db, memory, skills
        self.automations, self.calendar, self.plugins = automations, calendar, plugins
        self.last: dict[str, Any] = {}

    # ----- this PC -----

    def _meta(self, key: str, default: Any = None) -> Any:
        row = self.db.one("SELECT value FROM meta WHERE key=?", [key])
        return json.loads(row["value"]) if row else default

    def _set_meta(self, key: str, value: Any) -> None:
        self.db.run("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [key, json.dumps(value)])

    def device(self) -> dict[str, str]:
        d = self._meta("device")
        if not d:
            d = {"id": uuid.uuid4().hex[:12], "name": os.environ.get("COMPUTERNAME", "This PC")[:40]}
            self._set_meta("device", d)
        return d

    def _keys(self, snap: dict[str, Any]) -> dict[str, list[str]]:
        """A short fingerprint of every item, per kind: what this PC has."""
        return {"memories": [_h(m["type"], m["subject"], m["content"]) for m in snap["memories"]],
                "skills": [s["name"].lower() for s in snap["skills"]],
                "automations": [a["name"].lower() for a in snap["automations"]],
                "calendar": [e["id"] for e in snap["calendar"]],
                "plugins": [p["id"] for p in snap["plugins"]]}

    def _track(self, now: dict[str, list[str]]) -> dict[str, set[str]]:
        """Note what was deleted on this PC since the last look (it was here then and isn't now), and remember it, so a
        sync never brings it back. Something made again here is no longer counted as deleted."""
        had, gone = self._meta("had", {}), self._meta("gone", {})
        out: dict[str, set[str]] = {}
        for kind, keys in now.items():
            here = set(keys)
            out[kind] = (set(gone.get(kind, [])) | (set(had.get(kind, [])) - here)) - here
        self._set_meta("had", now)
        self._set_meta("gone", {k: sorted(v)[-2000:] for k, v in out.items()})
        return out

    def _profile_stamp(self, profile: dict[str, Any]) -> float:
        """When the synced settings last changed on this PC (noticed at each backup)."""
        seen = self._meta("profile_seen") or {}
        h = _h(profile)
        if seen.get("hash") != h:
            seen = {"hash": h, "at": time.time() if seen else 0.0}  # the first look is not a change
            self._set_meta("profile_seen", seen)
        return float(seen["at"])

    def snapshot(self, include_secrets: bool) -> dict[str, Any]:
        profile = {k: self.settings.get(k) for k in PROFILE}
        snap = {"device": self.device(), "made_at": time.time(), "profile": profile, "profile_at": self._profile_stamp(profile),
                "calendars": [c["name"] for c in self.settings.get("calendars") or []],
                "memories": self.memory.export(),
                "skills": [{k: s[k] for k in ("name", "when_to", "body", "enabled")} for s in self.skills.mine()],
                "automations": [{k: a[k] for k in ("name", "prompt", "kind", "at", "days", "every_min", "deliver", "enabled")}
                                for a in self.automations.list()],
                "calendar": self.calendar.all_local(),
                "plugins": self.plugins.manifests(),
                "secrets": {}}
        if include_secrets:
            snap["secrets"] = {k: v for k, v in self.settings.all_secrets().items() if not k.endswith(PASS_KEY)}
        return snap

    def restore(self, snap: dict[str, Any], settings_too: bool = False) -> dict[str, int]:
        """Bring a backup's contents in: add what is new, update what changed, keep what was deleted here deleted.
        settings_too: take the backup's settings even if this PC's are newer (restoring by hand)."""
        mine = self.snapshot(False)
        gone = self._track(self._keys(mine))
        counts = {"memories": 0, "skills": 0, "automations": 0, "calendar": 0, "plugins": 0, "secrets": 0, "settings": 0}

        mems = [m for m in snap.get("memories") or [] if _h(m.get("type"), m.get("subject"), m.get("content")) not in gone["memories"]]
        counts["memories"] = self.memory.import_items(mems)["added"]

        have = {s["name"].lower(): s for s in self.skills.mine()}
        for s in snap.get("skills") or []:
            name = str(s.get("name", "")).lower()
            if name in gone["skills"]:
                continue
            cur = have.get(name)
            if cur and (cur["when_to"], cur["body"], bool(cur["enabled"])) == (s.get("when_to", ""), s.get("body", ""), bool(s.get("enabled", True))):
                continue
            try:
                self.skills.save_mine(s["name"], s.get("when_to", ""), s["body"], bool(s.get("enabled", True)), cur["id"] if cur else None)
                counts["skills"] += 1
            except (ValueError, KeyError) as e:
                log.info("skill not restored: %s", e)

        have_a = {a["name"].lower(): a for a in self.automations.list()}
        for a in snap.get("automations") or []:
            name = str(a.get("name", "")).lower()
            cur = have_a.get(name)
            if name in gone["automations"] or cur and all(cur[k] == a.get(k) for k in ("prompt", "kind", "at", "days", "every_min", "deliver")):
                continue
            try:
                self.automations.save(a["name"], a["prompt"], a["kind"], a.get("at", ""), a.get("days") or [], int(a.get("every_min") or 0),
                                      a.get("deliver") or [], bool(a.get("enabled", True)), cur["id"] if cur else None)
                counts["automations"] += 1
            except Exception as e:  # noqa: BLE001 - e.g. a one-off whose time has passed
                log.info("automation not restored: %s", e)

        counts["calendar"] = self.calendar.put_local([e for e in snap.get("calendar") or [] if e.get("id") not in gone["calendar"]])

        installed = {p["id"] for p in self.plugins.installed()}
        for m in snap.get("plugins") or []:
            if m.get("id") in installed or m.get("id") in gone["plugins"]:
                continue
            try:
                from .plugins import validate

                self.plugins.install(validate(m), str(m.get("source") or "backup"))
                counts["plugins"] += 1
            except Exception as e:  # noqa: BLE001 - a clash with something made here: leave it
                log.info("plugin not restored: %s", e)

        if snap.get("secrets"):
            counts["secrets"] = self.settings.merge_secrets(snap["secrets"])
        if settings_too or float(snap.get("profile_at") or 0) > float(mine["profile_at"]):
            profile = {k: v for k, v in (snap.get("profile") or {}).items() if k in PROFILE}
            linked = [{"name": n} for n in snap.get("calendars") or [] if self.settings.service_key(f"calendar:{n}")]
            if linked:
                profile["calendars"] = linked + [c for c in self.settings.get("calendars") or [] if c["name"] not in snap["calendars"]]
            self.settings.update_profile(profile)
            self._profile_stamp({k: self.settings.get(k) for k in PROFILE})
            counts["settings"] = 1
        self._track(self._keys(self.snapshot(False)))
        return counts

    # ----- files -----

    def write(self, path: Path, passphrase: str, include_secrets: bool) -> dict[str, Any]:
        snap = self.snapshot(include_secrets)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(encrypt(snap, passphrase))
        os.replace(tmp, path)
        self._track(self._keys(snap))
        return {"path": str(path), "memories": len(snap["memories"]), "skills": len(snap["skills"]),
                "automations": len(snap["automations"]), "events": len(snap["calendar"]), "plugins": len(snap["plugins"]),
                "with_keys": include_secrets}

    def read(self, path: Path, passphrase: str) -> dict[str, int]:
        return self.restore(decrypt(path.read_bytes(), passphrase), settings_too=True)

    # ----- sync through a folder -----

    def sync_cfg(self) -> dict[str, Any]:
        raw = self.settings.get("sync") or {}
        return {"enabled": bool(raw.get("enabled")), "folder": str(raw.get("folder") or ""), "include_secrets": bool(raw.get("include_secrets")),
                "has_passphrase": bool(self.settings.service_key(PASS_KEY))}

    def own_file(self, folder: Path) -> Path:
        d = self.device()
        name = re.sub(r"[^\w -]", "", d["name"]).strip() or "PC"
        return folder / f"JARVIS sync - {name} {d['id']}{SUFFIX}"

    def sync_once(self) -> dict[str, Any]:
        """Bring in what the other PCs wrote since last time, then write this PC's own file if anything changed."""
        cfg = self.sync_cfg()
        passphrase = self.settings.service_key(PASS_KEY)
        if not cfg["folder"] or not passphrase:
            raise BackupError("Choose a sync folder and a passphrase first.")
        folder = Path(cfg["folder"]).expanduser()
        if not folder.is_dir():
            raise BackupError(f"The sync folder {folder.name or folder} isn't there (is the drive connected?).")
        mine = self.own_file(folder)
        applied = self._meta("sync_applied", {})
        brought: dict[str, int] = {}
        others = []
        for f in sorted(folder.glob(f"JARVIS sync - *{SUFFIX}")):
            if f == mine:
                continue
            try:
                blob = f.read_bytes()
                head = peek(blob)
                dev = str((head.get("device") or {}).get("id") or f.name)
                others.append(str((head.get("device") or {}).get("name") or f.stem))
                if float(head.get("made_at") or 0) <= float(applied.get(dev) or 0):
                    continue
                for k, v in self.restore(decrypt(blob, passphrase)).items():
                    brought[k] = brought.get(k, 0) + v
                applied[dev] = float(head.get("made_at") or 0)
                self._set_meta("sync_applied", applied)
            except (BackupError, OSError, ValueError) as e:
                log.warning("sync: couldn't read %s: %s", f.name, e)
                brought.setdefault("unreadable", 0)
                brought["unreadable"] += 1
        snap = self.snapshot(cfg["include_secrets"])
        content = _h({k: v for k, v in snap.items() if k not in ("made_at",)})
        wrote = False
        if content != self._meta("sync_written") or not mine.is_file():
            self.write(mine, passphrase, cfg["include_secrets"])
            self._set_meta("sync_written", content)
            wrote = True
        self.last = {"at": time.time(), "brought": {k: v for k, v in brought.items() if v}, "wrote": wrote, "other_pcs": others}
        return self.last

    async def run(self) -> None:
        import asyncio

        await asyncio.sleep(60)
        while True:
            try:
                if self.sync_cfg()["enabled"]:
                    await asyncio.to_thread(self.sync_once)
            except BackupError as e:
                self.last = {"at": time.time(), "error": str(e)}
            except Exception:  # noqa: BLE001 - keep syncing
                log.exception("sync failed")
            await asyncio.sleep(SYNC_EVERY_S)
