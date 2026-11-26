"""Data API keys: read-only access to chosen hub projects' outputs.

A person (or the operator) makes a key for an app that wants exuvia data
without being limited to what views show: a dashboard elsewhere, a
notebook, a script, another service. Each key names the projects it may
read, and with it the app can list their tables, read a node's output and
run one DuckDB ``SELECT`` across all of them at once (dataapi.py). It never
writes: not code, not runs, not settings.

A key can never see more than the person who made it. Each request checks
the key's projects against what its maker can still view (sharing.py), so
a project unshared with them, or a disabled account, takes the key's access
with it. A key made by the operator (the hub's API token, or a hub nobody
signs in to) reads the projects it names. Deleting a project takes it off
every key, so a new project of the same name is never readable by an old
key.

Keys start with ``exd_`` and only ever work under ``/api/data/``. Only a
hash of each key is kept; the key itself is shown once, when it is made.
The rows live in the database the hub's groups use (groups.py), and a hub
export carries them beside the user list.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .db import Database, HubRecordStore

PREFIX = "exd_"
MAX_NAME = 80
MAX_PROJECTS = 200
# How long a key is answered from memory before the database is read (and
# last_used written) again; also how long another replica can take to notice
# a revoked key.
SEEN_EVERY = 60.0
# Lookups remembered at once: past it, the misses and the stale ones go first.
SEEN_MAX = 4096
# The maker of a key made by the operator.
OPERATOR = ""

TABLES = {
    "data_keys": """create table if not exists data_keys (
    scope text not null default '',
    id text not null,
    owner_id text not null,      -- users.py id of who made it; '' for the operator
    name text not null,          -- what it is for: "sales dashboard", ...
    hash text not null,          -- sha256 of the key; the key itself is never kept
    hint text not null,          -- its last 4 characters, to tell keys apart
    created_at double precision not null,
    last_used double precision,
    primary key (scope, id)
)""",
    "data_key_projects": """create table if not exists data_key_projects (
    scope text not null default '',
    key_id text not null,
    project text not null,
    primary key (scope, key_id, project)
)""",
}
INDEXES = (
    "create unique index if not exists data_keys_hash on data_keys(scope, hash)",
    "create index if not exists data_keys_owner on data_keys(scope, owner_id)",
    "create index if not exists data_key_projects_project on data_key_projects(scope, project)",
)
COLS = "id, owner_id, name, hint, created_at, last_used"

def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def looks_like_key(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX) and 20 <= len(value) <= 200  # type: ignore[union-attr]


def clean_projects(projects: Any) -> list[str]:
    """A key's project list: distinct ids, in the order given."""
    if not isinstance(projects, list) or not all(isinstance(p, str) for p in projects):
        raise ValueError("projects is a list of project ids")
    out = list(dict.fromkeys(p.strip() for p in projects if p.strip()))
    if not out:
        raise ValueError("a key needs at least one project to read")
    if len(out) > MAX_PROJECTS:
        raise ValueError(f"a key reads at most {MAX_PROJECTS} projects")
    return out


@dataclass(frozen=True)
class DataKey:
    id: str
    owner_id: str  # OPERATOR for the operator
    name: str
    projects: tuple[str, ...]


class DataKeyStore(HubRecordStore):
    TABLES, INDEXES = TABLES, INDEXES
    ARCHIVE_WHAT = "data keys"

    def __init__(self, db: Database, scope: str = "", owns_db: bool = False):
        super().__init__(db, scope, owns_db)
        self._lock = threading.Lock()
        # hash -> (key or None, when read)
        self._seen: dict[str, tuple[DataKey | None, float]] = {}
        # Counts the changes to the keys: what was read before one is not kept after it.
        self._version = 0

    def _replaced(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self._changed()

    def _changed(self) -> None:
        with self._lock:
            self._version += 1
            self._seen.clear()

    def _projects_of(self, ids: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {i: [] for i in ids}
        if not ids:
            return out
        marks = ", ".join("?" * len(ids))
        for r in self._q(
            f"select key_id, project from data_key_projects where scope=? and key_id in ({marks}) order by project",
            (self.scope, *ids),
        ):
            out[r["key_id"]].append(r["project"])
        return out

    def _rows(self, where: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        rows = self._q(f"select {COLS} from data_keys where scope=? {where} order by created_at desc, id", (self.scope, *args))
        projects = self._projects_of([r["id"] for r in rows])
        return [{**r, "owner_id": r["owner_id"] or None, "projects": projects[r["id"]]} for r in rows]

    def create(self, owner: str | None, name: str, projects: list[str], now: float | None = None) -> tuple[dict[str, Any], str]:
        """A new key: its row, and the key itself, which is not kept and so
        can't be shown again. ``projects`` must already be checked."""
        now = time.time() if now is None else now
        projects = clean_projects(projects)
        name = " ".join(name.split())[:MAX_NAME] or "app"
        key = PREFIX + secrets.token_urlsafe(32)
        kid = uuid.uuid4().hex[:12]
        with self.db.transaction():
            self._q(
                "insert into data_keys (scope, id, owner_id, name, hash, hint, created_at, last_used) values (?,?,?,?,?,?,?,?)",
                (self.scope, kid, owner or OPERATOR, name, _hash(key), key[-4:], now, None),
            )
            for p in projects:
                self._q("insert into data_key_projects (scope, key_id, project) values (?,?,?)", (self.scope, kid, p))
        row = {"id": kid, "owner_id": owner or None, "name": name, "hint": key[-4:], "created_at": now,
               "last_used": None, "projects": sorted(projects)}
        return row, key

    def list(self, owner: str | None = None, everyone: bool = False) -> list[dict[str, Any]]:
        """The keys ``owner`` made (None: the operator's), or every key on
        the hub when ``everyone``; newest first, never the keys themselves."""
        if everyone:
            return self._rows("", ())
        return self._rows("and owner_id=?", (owner or OPERATOR,))

    def get(self, kid: str) -> dict[str, Any] | None:
        rows = self._rows("and id=?", (kid,))
        return rows[0] if rows else None

    def update(self, kid: str, name: str | None = None, projects: list[str] | None = None) -> dict[str, Any]:
        """Rename a key or replace its projects (``projects`` already
        checked). KeyError: no such key."""
        if self.get(kid) is None:
            raise KeyError(kid)
        with self.db.transaction():
            if name is not None:
                self._q("update data_keys set name=? where scope=? and id=?",
                        (" ".join(name.split())[:MAX_NAME] or "app", self.scope, kid))
            if projects is not None:
                projects = clean_projects(projects)
                self._q("delete from data_key_projects where scope=? and key_id=?", (self.scope, kid))
                for p in projects:
                    self._q("insert into data_key_projects (scope, key_id, project) values (?,?,?)", (self.scope, kid, p))
        self._changed()
        return self.get(kid)  # type: ignore[return-value]

    def revoke(self, kid: str) -> bool:
        """Delete a key; False when there is no such key. This replica turns
        it away at once, others within SEEN_EVERY."""
        with self.db.transaction():
            found = self._q("select 1 from data_keys where scope=? and id=?", (self.scope, kid))
            if found:
                self._q("delete from data_key_projects where scope=? and key_id=?", (self.scope, kid))
                self._q("delete from data_keys where scope=? and id=?", (self.scope, kid))
        self._changed()
        return bool(found)

    def by_key(self, key: str, now: float | None = None) -> DataKey | None:
        """The key a request presents (None: no such key). Answered from
        memory for SEEN_EVERY; ``last_used`` is written at most that often."""
        if not looks_like_key(key):
            return None
        now = time.time() if now is None else now
        h = _hash(key)
        with self._lock:
            hit = self._seen.get(h)
            version = self._version
        if hit and now - hit[1] < SEEN_EVERY:
            return hit[0]
        rows = self._q(f"select {COLS} from data_keys where scope=? and hash=?", (self.scope, h))
        found = None
        if rows:
            r = rows[0]
            self._q("update data_keys set last_used=? where scope=? and id=?", (now, self.scope, r["id"]))
            found = DataKey(r["id"], r["owner_id"], r["name"], tuple(self._projects_of([r["id"]])[r["id"]]))
        with self._lock:
            # A key revoked (or changed) while it was being read is not
            # remembered as it was: the next request reads it again.
            if self._version == version:
                if len(self._seen) >= SEEN_MAX:
                    # Anyone can send any number of made-up keys: what is remembered stays bounded.
                    self._seen = {k: v for k, v in self._seen.items() if v[0] is not None and now - v[1] < SEEN_EVERY}
                    if len(self._seen) >= SEEN_MAX:
                        self._seen.clear()
                self._seen[h] = (found, now)
        return found

    def forget_project(self, project: str) -> None:
        """Take a deleted project off every key."""
        self._q("delete from data_key_projects where scope=? and project=?", (self.scope, project))
        self._changed()


