"""Who can open and change each project on a hub.

A hub project is one workflow, and it is shared the way a document is:

* **Owner.** The person who created it (``Hub.create`` records them). The
  owner manages its sharing, can hand ownership to someone else, and is the
  only one besides an admin who can delete it or replace its code with a
  template.
* **People.** The owner gives others a role on it: ``viewer`` (open it, read
  its code, outputs, runs and views) or ``editor`` (also change code, run
  nodes, use the agent). A person is named by their user id, or by email
  when they have never signed in: the grant takes effect on their first
  sign-in with that email.
* **General access.** What everyone else signed in on the hub gets:
  ``private`` (nothing), ``view`` or ``edit``. A new project someone owns
  starts with ``view`` (everyone signed in can open it, and only its owner
  and the editors they add can change it), unless the creator picks another.
  A project with no owner (made by a script with the hub's API token) starts
  with ``edit``, and so does one with no settings of its own (made before
  sharing existed): with ``view``, nobody but an admin could change it.
  ``EXUVIA_DEFAULT_ACCESS``, when set, replaces all three. A project without
  an owner keeps none until an admin gives it one.
* **Links.** A share link (``/s/<token>``) opens one project read-only for
  whoever holds it, signed in or not. Links are only ever ``viewer``: an
  editing link would let anyone run code on the hub. Each can expire and be
  revoked; only a hash of the token is kept.
* **Access requests.** Someone signed in without (enough) access asks for a
  role; the owner approves or dismisses the request.
* **Groups.** A project in a group (groups.py) also gives the group's people
  their group role on it: ``viewer`` stays ``viewer``, ``editor`` and the
  group's owner get ``editor``.

Hub admins (users.py) and callers with the hub's API token can do anything
on every project. Roles are ranked ``viewer`` < ``editor`` < ``owner``, and a
person gets the highest role any rule gives them.

The rules live beside the user list: the hub's ``.exuvia/hub.db`` or the
shared ``EXUVIA_DB`` under ``EXUVIA_DB_SCOPE``. Each server answers from
memory for SEEN_EVERY, so with several replicas a change can take that long
to reach the others (the replica that made it sees it at once).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import auth
from .auth import Identity
from .db import Database, database_url, scope_prefix, shared_database
from .store import SCHEMA_LOCK_KEY
from .users import SEEN_EVERY, UserStore

ROLES = ("viewer", "editor", "owner")
GRANT_ROLES = ("viewer", "editor")  # what a person or a request can be given; ownership is transferred
RANK = {None: 0, "viewer": 1, "editor": 2, "owner": 3}
GENERAL = ("private", "view", "edit")
GENERAL_ROLE: dict[str, str | None] = {"private": None, "view": "viewer", "edit": "editor"}
LINK_PREFIX = "exs_"
LINK_COOKIE = "exuvia_link"
MAX_NAME = 80
MAX_MESSAGE = 500
MAX_LINK_DAYS = 3650

TABLES = {
    "project_access": """create table if not exists project_access (
    scope text not null default '',
    project text not null,
    owner_id text,               -- a user id (users.py); null: nobody owns it
    general text not null,       -- private | view | edit: everyone else signed in
    created_at double precision not null,
    updated_at double precision not null,
    primary key (scope, project)
)""",
    "project_members": """create table if not exists project_members (
    scope text not null default '',
    project text not null,
    principal text not null,     -- user:<id>, or email:<address> for someone not signed in yet
    role text not null,          -- viewer | editor
    created_at double precision not null,
    created_by text,
    primary key (scope, project, principal)
)""",
    "project_links": """create table if not exists project_links (
    scope text not null default '',
    id text not null,
    project text not null,
    name text not null,
    hash text not null,          -- sha256 of the token; the token itself is never kept
    hint text not null,          -- its last 4 characters
    created_at double precision not null,
    created_by text,
    expires_at double precision, -- null: until revoked
    last_used double precision,
    primary key (scope, id)
)""",
    "project_requests": """create table if not exists project_requests (
    scope text not null default '',
    project text not null,
    user_id text not null,
    role text not null,
    message text not null default '',
    created_at double precision not null,
    primary key (scope, project, user_id)
)""",
}
INDEXES = (
    "create unique index if not exists project_links_hash on project_links(scope, hash)",
    "create index if not exists project_links_project on project_links(scope, project)",
)
LINK_COLS = "id, name, hint, created_at, created_by, expires_at, last_used"

_prepared: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _prepare(db: Database) -> None:
    """Create the tables once per database (see users._prepare)."""
    if db in _prepared:
        return
    if not all(db.columns(t) for t in TABLES):
        with db.transaction():
            if db.dialect == "postgres":
                db.query(f"select pg_advisory_xact_lock({SCHEMA_LOCK_KEY})")
            for ddl in (*TABLES.values(), *INDEXES):
                db.query(ddl)
    _prepared.add(db)


def _configured_general() -> str | None:
    """``EXUVIA_DEFAULT_ACCESS``, when it is set."""
    v = os.environ.get("EXUVIA_DEFAULT_ACCESS", "").strip().lower()
    if v and v not in GENERAL:
        raise SystemExit(f"EXUVIA_DEFAULT_ACCESS must be one of {', '.join(GENERAL)}, not {v!r}")
    return v or None


def default_general() -> str:
    """What everyone signed in gets on a project with no settings of its own
    (made before sharing existed): ``EXUVIA_DEFAULT_ACCESS``, else ``edit``,
    as before projects had sharing."""
    return _configured_general() or "edit"


def new_project_general(owned: bool) -> str:
    """What everyone signed in gets on a new project its creator leaves at the
    default: ``EXUVIA_DEFAULT_ACCESS``, else ``view`` for a project someone
    owns (only the owner and the editors they add change it) and ``edit`` for
    one nobody owns, which nobody but an admin could change otherwise."""
    return _configured_general() or ("view" if owned else "edit")


def higher(a: str | None, b: str | None) -> str | None:
    return a if RANK[a] >= RANK[b] else b


def at_least(role: str | None, need: str) -> bool:
    return RANK[role] >= RANK[need]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def looks_like_link(value: str | None) -> bool:
    return bool(value) and value.startswith(LINK_PREFIX) and 20 < len(value) <= 200  # type: ignore[union-attr]


def user_principal(uid: str) -> str:
    return f"user:{uid}"


def email_principal(email: str) -> str:
    return f"email:{email.strip().lower()}"


def _clean_email(email: str) -> str:
    e = email.strip().lower()
    if not e or len(e) > 254 or "@" not in e or any(c.isspace() or ord(c) < 32 for c in e):
        raise ValueError(f"{email!r} is not an email address")
    return e


@dataclass(frozen=True)
class Rules:
    """One project's sharing, as read from the database."""

    owner_id: str | None
    general: str
    members: dict[str, str]  # principal -> role
    explicit: bool  # the project has settings of its own (else: the hub's default)

    def role_of(self, ident: Identity) -> str | None:
        """What this person gets from the project's own rules (admins and
        links are the caller's business)."""
        if ident.uid and ident.uid == self.owner_id:
            return "owner"
        role = GENERAL_ROLE[self.general] if ident.uid else None
        if ident.uid:
            role = higher(role, self.members.get(user_principal(ident.uid)))
        if ident.email and ident.uid:
            role = higher(role, self.members.get(email_principal(ident.email)))
        return role


class SharingStore:
    def __init__(self, db: Database, scope: str = "", owns_db: bool = False, users: UserStore | None = None):
        self.db = db
        self.scope = scope
        self.users = users
        self._owns_db = owns_db
        # The hub's groups (groups.py): a group role is also a role on each
        # project in the group. Set by create_hub_app.
        self.groups: Any = None
        _prepare(db)
        self._lock = threading.Lock()
        self._rules: dict[str, tuple[Rules, float]] = {}
        # Link tokens answered from memory: hash -> (project or None, expires_at, when read).
        self._links: dict[str, tuple[str | None, float | None, float]] = {}

    @property
    def people(self) -> bool:
        """Whether people can be named (the server keeps a user list)."""
        return self.users is not None

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.db.query(sql, args)

    def _changed(self, project: str | None = None) -> None:
        with self._lock:
            if project is None:
                self._rules.clear()
                self._links.clear()
            else:
                self._rules.pop(project, None)

    # -- reading the rules -----------------------------------------------------
    def rules(self, project: str, now: float | None = None, fresh: bool = False) -> Rules:
        """The project's rules; ``fresh`` reads the database even when this
        server remembers them."""
        now = time.time() if now is None else now
        with self._lock:
            hit = None if fresh else self._rules.get(project)
        if hit and now - hit[1] < SEEN_EVERY:
            return hit[0]
        rows = self._q("select owner_id, general from project_access where scope=? and project=?", (self.scope, project))
        members = {
            r["principal"]: r["role"]
            for r in self._q("select principal, role from project_members where scope=? and project=?", (self.scope, project))
        }
        if rows:
            rules = Rules(rows[0]["owner_id"], rows[0]["general"], members, True)
        else:
            rules = Rules(None, default_general(), members, False)
        with self._lock:
            self._rules[project] = (rules, now)
        return rules

    def role(self, project: str, ident: Identity | None) -> str | None:
        """The role this request has on ``project``. Nobody signed in is the
        operator (the hub's API token, or a hub nobody signs in to): owner.
        Admins are owners everywhere; the project's group adds the role it
        gives (groups.py); a share link adds ``viewer`` on its own project."""
        if ident is None:
            return "owner"
        if ident.role == "admin":
            return "owner"
        role = self.rules(project).role_of(ident) if ident.uid else None
        if self.groups is not None and ident.uid:
            role = higher(role, self.groups.project_role(project, ident))
        if ident.link == project:
            role = higher(role, "viewer")
        return role

    def current_role(self, project: str) -> str | None:
        return self.role(project, auth.current())

    # -- changing them -----------------------------------------------------------
    def _ensure(self, project: str, now: float) -> Rules:
        """The project's row, created from the hub default when it has none."""
        rules = self.rules(project, now=now, fresh=True)
        if not rules.explicit:
            self._q(
                "insert into project_access (scope, project, owner_id, general, created_at, updated_at) "
                "values (?,?,?,?,?,?) on conflict (scope, project) do nothing",
                (self.scope, project, None, rules.general, now, now),
            )
        return rules

    def init_project(self, project: str, owner_id: str | None, general: str | None = None, now: float | None = None) -> None:
        """Settings for a new project: its owner and general access (when not
        given: new_project_general). Replaces anything left under that id."""
        now = time.time() if now is None else now
        general = general or new_project_general(owned=owner_id is not None)
        if general not in GENERAL:
            raise ValueError(f"access must be one of {', '.join(GENERAL)}")
        with self.db.transaction():
            self._forget(project)
            self._q(
                "insert into project_access (scope, project, owner_id, general, created_at, updated_at) values (?,?,?,?,?,?)",
                (self.scope, project, owner_id, general, now, now),
            )
        self._changed(project)

    def _forget(self, project: str) -> None:
        for table in TABLES:
            self._q(f"delete from {table} where scope=? and project=?", (self.scope, project))

    def forget(self, project: str) -> None:
        """Drop everything about a deleted project, links included."""
        with self.db.transaction():
            self._forget(project)
        self._changed()

    def set_general(self, project: str, general: str, now: float | None = None) -> None:
        if general not in GENERAL:
            raise ValueError(f"general access must be one of {', '.join(GENERAL)}")
        now = time.time() if now is None else now
        with self.db.transaction():
            self._ensure(project, now)
            self._q(
                "update project_access set general=?, updated_at=? where scope=? and project=?",
                (general, now, self.scope, project),
            )
        self._changed(project)

    def set_owner(self, project: str, uid: str, by: str | None = None, now: float | None = None) -> None:
        """Make ``uid`` the owner. The previous owner stays on as an editor."""
        now = time.time() if now is None else now
        if self.users is None or self.users.get(uid) is None:
            raise KeyError(uid)
        with self.db.transaction():
            old = self._ensure(project, now).owner_id
            if old == uid:
                return
            self._q(
                "update project_access set owner_id=?, updated_at=? where scope=? and project=?",
                (uid, now, self.scope, project),
            )
            self._q(
                "delete from project_members where scope=? and project=? and principal=?",
                (self.scope, project, user_principal(uid)),
            )
            self._q("delete from project_requests where scope=? and project=? and user_id=?", (self.scope, project, uid))
            if old:
                self._upsert_member(project, user_principal(old), "editor", by, now)
        self._changed(project)

    def _upsert_member(self, project: str, principal: str, role: str, by: str | None, now: float) -> None:
        self._q(
            "insert into project_members (scope, project, principal, role, created_at, created_by) values (?,?,?,?,?,?) "
            "on conflict (scope, project, principal) do update set role=excluded.role",
            (self.scope, project, principal, role, now, by),
        )

    def grant(
        self, project: str, role: str, *, user_id: str | None = None, email: str | None = None,
        by: str | None = None, now: float | None = None,
    ) -> str:
        """Give someone ``role`` (viewer or editor) on the project, or change
        the role they have. Named by user id, or by email: an email someone
        in the user list has is stored as that user. Returns the member's
        principal. KeyError: no such user; ValueError: not allowed."""
        now = time.time() if now is None else now
        if role not in GRANT_ROLES:
            raise ValueError(f"role must be one of {', '.join(GRANT_ROLES)} (ownership is transferred, not granted)")
        if self.users is None:
            raise ValueError("nobody signs in on this hub, so there is nobody to share with: use a share link")
        if (user_id is None) == (email is None):
            raise ValueError("name the person by `user` (a user id) or by `email`")
        if email is not None:
            email = _clean_email(email)
            found = self.users.by_email(email)
            if found is not None:
                user_id = found.id
        if user_id is not None:
            if self.users.get(user_id) is None:
                raise KeyError(user_id)
            principal = user_principal(user_id)
        else:
            principal = email_principal(email or "")
        with self.db.transaction():
            rules = self._ensure(project, now)
            if user_id is not None and user_id == rules.owner_id:
                raise ValueError("that person owns the project")
            self._upsert_member(project, principal, role, by, now)
            if user_id is not None:
                self._q(
                    "delete from project_requests where scope=? and project=? and user_id=?",
                    (self.scope, project, user_id),
                )
        self._changed(project)
        return principal

    def revoke(self, project: str, principal: str) -> bool:
        with self.db.transaction():
            found = self._q(
                "select 1 from project_members where scope=? and project=? and principal=?", (self.scope, project, principal)
            )
            if found:
                self._q(
                    "delete from project_members where scope=? and project=? and principal=?",
                    (self.scope, project, principal),
                )
        self._changed(project)
        return bool(found)

    # -- describing them -----------------------------------------------------------
    def _person(self, uid: str | None) -> dict[str, Any] | None:
        if uid is None:
            return None
        user = self.users.get(uid) if self.users is not None else None
        if user is None:
            return {"id": uid, "name": uid, "email": None, "disabled": False}
        return {"id": user.id, "name": user.name, "email": user.email, "disabled": user.disabled}

    def members(self, project: str) -> list[dict[str, Any]]:
        """Everyone the project is shared with by name, editors first."""
        rows = self._q(
            "select principal, role, created_at, created_by from project_members where scope=? and project=? "
            "order by created_at, principal",
            (self.scope, project),
        )
        out = []
        for r in rows:
            kind, _, key = r["principal"].partition(":")
            if kind == "user":
                person = self._person(key) or {}
                out.append({**r, "kind": "user", "user": person, "pending": False})
            else:
                out.append({**r, "kind": "email", "user": {"id": None, "name": key, "email": key, "disabled": False},
                            "pending": True})
        out.sort(key=lambda m: (-RANK[m["role"]], m["user"]["name"].lower()))
        return out

    def describe(self, project: str) -> dict[str, Any]:
        rules = self.rules(project)
        return {
            "owner": self._person(rules.owner_id),
            "general": rules.general,
            "default": not rules.explicit,
        }

    def overview(self, projects: list[str], ident: Identity | None) -> dict[str, dict[str, Any]]:
        """For a project list: each visible project's owner, general access,
        whether it is shared and the caller's role (a project the caller has
        no role on is left out). Pending requests are counted for those who
        can answer them."""
        if not projects:
            return {}
        owners = {r["project"]: r for r in self._q(
            "select project, owner_id, general from project_access where scope=?", (self.scope,))}
        shared = {r["project"] for r in self._q(
            "select distinct project from project_members where scope=?", (self.scope,))}
        now = time.time()
        shared |= {r["project"] for r in self._q(
            "select distinct project from project_links where scope=? and (expires_at is null or expires_at > ?)",
            (self.scope, now))}
        asked: dict[str, int] = {}
        for r in self._q("select project, count(*) as n from project_requests where scope=? group by project", (self.scope,)):
            asked[r["project"]] = int(r["n"])
        out: dict[str, dict[str, Any]] = {}
        for pid in projects:
            role = self.role(pid, ident)
            if role is None:
                continue
            row = owners.get(pid)
            owner = row["owner_id"] if row else None
            out[pid] = {
                "access": role,
                "owner": self._person(owner),
                "general": row["general"] if row else default_general(),
                "shared": pid in shared,
                "requests": asked.get(pid, 0) if role == "owner" else 0,
            }
        return out

    # -- links ---------------------------------------------------------------------
    def create_link(
        self, project: str, name: str = "", expires_at: float | None = None, by: str | None = None, now: float | None = None,
    ) -> tuple[dict[str, Any], str]:
        """A new read-only link: its row, and the token, which is not kept."""
        now = time.time() if now is None else now
        if expires_at is not None and expires_at <= now:
            raise ValueError("a link has to expire in the future")
        name = " ".join(name.split())[:MAX_NAME] or "link"
        token = LINK_PREFIX + secrets.token_urlsafe(24)
        lid = uuid.uuid4().hex[:12]
        self._q(
            "insert into project_links (scope, id, project, name, hash, hint, created_at, created_by, expires_at, last_used) "
            "values (?,?,?,?,?,?,?,?,?,?)",
            (self.scope, lid, project, name, _hash(token), token[-4:], now, by, expires_at, None),
        )
        row = {"id": lid, "name": name, "hint": token[-4:], "created_at": now, "created_by": by,
               "expires_at": expires_at, "last_used": None, "role": "viewer"}
        return row, token

    def links(self, project: str) -> list[dict[str, Any]]:
        rows = self._q(
            f"select {LINK_COLS} from project_links where scope=? and project=? order by created_at desc, id",
            (self.scope, project),
        )
        now = time.time()
        return [{**r, "role": "viewer", "expired": r["expires_at"] is not None and r["expires_at"] <= now} for r in rows]

    def revoke_link(self, project: str, lid: str) -> bool:
        with self.db.transaction():
            found = self._q("select 1 from project_links where scope=? and project=? and id=?", (self.scope, project, lid))
            if found:
                self._q("delete from project_links where scope=? and project=? and id=?", (self.scope, project, lid))
        with self._lock:
            self._links.clear()  # this replica turns it away at once; others within SEEN_EVERY
        return bool(found)

    def link_project(self, token: str | None, now: float | None = None) -> str | None:
        """The project a link token opens (None: no such link, or it expired).
        Answered from memory for SEEN_EVERY; ``last_used`` is written at most
        that often."""
        if not looks_like_link(token):
            return None
        assert token is not None
        now = time.time() if now is None else now
        h = _hash(token)
        with self._lock:
            hit = self._links.get(h)
        if hit is None or now - hit[2] >= SEEN_EVERY:
            rows = self._q("select id, project, expires_at from project_links where scope=? and hash=?", (self.scope, h))
            if rows:
                self._q("update project_links set last_used=? where scope=? and id=?", (now, self.scope, rows[0]["id"]))
            hit = (rows[0]["project"], rows[0]["expires_at"], now) if rows else (None, None, now)
            with self._lock:
                self._links[h] = hit
        project, expires_at, _ = hit
        if expires_at is not None and expires_at <= now:
            return None
        return project

    def link_expiry(self, token: str) -> float | None:
        rows = self._q("select expires_at from project_links where scope=? and hash=?", (self.scope, _hash(token)))
        return rows[0]["expires_at"] if rows else None

    # -- access requests ---------------------------------------------------------------
    def request_access(self, project: str, uid: str, role: str = "viewer", message: str = "", now: float | None = None) -> None:
        if role not in GRANT_ROLES:
            raise ValueError(f"role must be one of {', '.join(GRANT_ROLES)}")
        now = time.time() if now is None else now
        message = message.strip()[:MAX_MESSAGE]
        self._q(
            "insert into project_requests (scope, project, user_id, role, message, created_at) values (?,?,?,?,?,?) "
            "on conflict (scope, project, user_id) do update set role=excluded.role, message=excluded.message, "
            "created_at=excluded.created_at",
            (self.scope, project, uid, role, message, now),
        )

    def requests(self, project: str) -> list[dict[str, Any]]:
        rows = self._q(
            "select user_id, role, message, created_at from project_requests where scope=? and project=? order by created_at",
            (self.scope, project),
        )
        return [{**r, "user": self._person(r["user_id"])} for r in rows]

    def request_of(self, project: str, uid: str) -> dict[str, Any] | None:
        rows = self._q(
            "select user_id, role, message, created_at from project_requests where scope=? and project=? and user_id=?",
            (self.scope, project, uid),
        )
        return rows[0] if rows else None

    def drop_request(self, project: str, uid: str) -> bool:
        with self.db.transaction():
            found = self.request_of(project, uid) is not None
            if found:
                self._q("delete from project_requests where scope=? and project=? and user_id=?", (self.scope, project, uid))
        return found

    def approve(self, project: str, uid: str, role: str | None = None, by: str | None = None) -> str:
        """Grant what was asked (or ``role``) and drop the request. KeyError:
        no such request."""
        req = self.request_of(project, uid)
        if req is None:
            raise KeyError(uid)
        return self.grant(project, role or req["role"], user_id=uid, by=by)

    # -- hub export / import --------------------------------------------------------------
    def dump_to(self, target: Path) -> None:
        """This scope's sharing as tables in a SQLite file (the hub export's
        ``hub.db``, beside the user list)."""
        con = sqlite3.connect(target)
        try:
            for ddl in (*TABLES.values(), *INDEXES):
                con.execute(ddl)
            for table in TABLES:
                con.execute(f"delete from {table}")
                for r in self._q(f"select * from {table} where scope=?", (self.scope,)):
                    con.execute(f"insert into {table} ({', '.join(r)}) values ({', '.join('?' * len(r))})", tuple(r.values()))
            con.commit()
        finally:
            con.close()

    def replace_from(self, source: Path) -> None:
        """Replace this scope's sharing with an archive's. An archive from
        before sharing has none: every project follows the hub's default."""
        con = sqlite3.connect(source)
        con.row_factory = sqlite3.Row
        try:
            present = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
            data = {t: [dict(r) for r in con.execute(f"select * from {t}")] if t in present else [] for t in TABLES}
        except sqlite3.OperationalError as e:
            raise ValueError(f"the archive's sharing settings are unreadable: {e}") from e
        finally:
            con.close()
        with self.db.transaction():
            for table, rows in data.items():
                self._q(f"delete from {table} where scope=?", (self.scope,))
                for r in rows:
                    r["scope"] = self.scope
                    self._q(f"insert into {table} ({', '.join(r)}) values ({', '.join('?' * len(r))})", tuple(r.values()))
        self._changed()


def open_sharing(local: Path, users: UserStore | None) -> SharingStore:
    """A hub's sharing rules: in the user list's database when there is one,
    else the shared ``EXUVIA_DB``, else ``local`` (the hub's SQLite file)."""
    if users is not None:
        return SharingStore(users.db, scope=users.scope, users=users)
    url = database_url()
    if url:
        return SharingStore(shared_database(url), scope=scope_prefix())
    from .db import SQLiteDatabase

    local.parent.mkdir(parents=True, exist_ok=True)
    return SharingStore(SQLiteDatabase(local), owns_db=True)
