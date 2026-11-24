"""Groups of hub projects: shared access, and environment variables the
projects inherit. Also the hub's own variables, which every project gets.

A group is a folder of projects on a hub:

* **Anyone signed in can create one** and owns it. The owner gives other
  people a role on it, as on a project (sharing.py): ``viewer`` or
  ``editor``, by user id, or by email for someone who has not signed in yet.
  Hub admins and the hub's API token are owners of every group.
* **A project is in at most one group.** A group role is also a role on
  every project in the group (folder-style): a group viewer can open each of
  them, a group editor can change and run each of them. A group owner gets
  ``editor`` on the projects, not ``owner``: deleting a project or changing
  its own sharing stays with the project's owner. A person's role on a
  project is the highest the project's own rules or its group give them.
* **Variables.** A group has environment variables and secrets of its own,
  with the rules of a project's (envvars.py: the same names, normalization,
  encryption, masking in logs, and a secret's value is never sent back). A
  run of a project in the group gets them; where a name is set in more than
  one place the project's own variable wins, then the group's, then the
  server's environment. Group secrets are encrypted with
  ``EXUVIA_SECRETS_KEY``, or else with the hub's own key in
  ``<hub>/.exuvia/secrets.key``.

Who can do what:

=====================================  =========================================
see the group, its projects, members   viewer
and its variables' names (and plain
values)
set, rotate and remove its variables   editor
add a project                          editor on the group **and** owner of the
                                       project
take a project out                     editor on the group, or owner of the
                                       project
rename it, manage its people, hand it  owner
over, delete it
=====================================  =========================================

Adding a project needs its owner because it widens who can open the project
(the group's people get their role on it) and who can read the group's
secrets (anyone who can edit a project can run code in it, and so can read
every variable its runs get: masking a secret in logs only stops accidents).

**Hub variables.** Below the groups there is one more layer: variables the
hub gives every project on it (``hub_env``), with the same rules again.
Only hub admins (and the hub's API token) set, rotate and remove them;
anyone signed in sees their names and plain values. A run gets them unless
its project or its group sets the same name: the project's own variable
wins, then the group's, then the hub's, then the server's environment.
Everyone who can edit any project on the hub can read them, so they are for
what the whole hub shares (an LLM gateway's key, a warehouse's address), not
for a key only some people should have: that belongs in a group.

Everything is in the hub's database beside sharing (``.exuvia/hub.db``, or
the shared ``EXUVIA_DB`` under ``EXUVIA_DB_SCOPE``). Each server answers roles
from memory for SEEN_EVERY, as sharing does; a run reads its group's
variables from the database every time.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .auth import Identity
from .db import Database, database_url, scope_prefix, shared_database
from .sharing import GRANT_ROLES, RANK, _clean_email, email_principal, higher, user_principal
from .store import SCHEMA_LOCK_KEY
from .users import SEEN_EVERY, UserStore

ROLES = ("viewer", "editor", "owner")
# What a group role gives on each project in the group.
PROJECT_ROLE: dict[str | None, str | None] = {None: None, "viewer": "viewer", "editor": "editor", "owner": "editor"}
MAX_NAME = 80
MAX_GROUPS = 1000

TABLES = {
    "project_groups": """create table if not exists project_groups (
    scope text not null default '',
    id text not null,
    name text not null,
    owner_id text,               -- a user id (users.py); null: made by the hub's operator
    created_at double precision not null,
    updated_at double precision not null,
    primary key (scope, id)
)""",
    "project_group_members": """create table if not exists project_group_members (
    scope text not null default '',
    group_id text not null,
    principal text not null,     -- user:<id>, or email:<address> for someone not signed in yet
    role text not null,          -- viewer | editor
    created_at double precision not null,
    created_by text,
    primary key (scope, group_id, principal)
)""",
    "project_group_projects": """create table if not exists project_group_projects (
    scope text not null default '',
    project text not null,       -- a project is in at most one group
    group_id text not null,
    added_at double precision not null,
    added_by text,
    primary key (scope, project)
)""",
    "project_group_env": """create table if not exists project_group_env (
    scope text not null default '',
    group_id text not null,
    name text not null,          -- an environment variable every project in the group gets (envvars.py)
    value text not null,         -- a secret's is encrypted: enc:v1:<key id>:<token> (envvars.Keyring)
    secret integer not null default 1,
    updated_at double precision not null,
    updated_by text,
    primary key (scope, group_id, name)
)""",
    "hub_env": """create table if not exists hub_env (
    scope text not null default '',
    name text not null,          -- an environment variable every project on the hub gets (envvars.py)
    value text not null,         -- a secret's is encrypted: enc:v1:<key id>:<token> (envvars.Keyring)
    secret integer not null default 1,
    updated_at double precision not null,
    updated_by text,
    primary key (scope, name)
)""",
}
INDEXES = ("create index if not exists project_group_projects_group on project_group_projects(scope, group_id)",)

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


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("give the group a name")
    if len(name) > MAX_NAME:
        raise ValueError(f"a group's name is at most {MAX_NAME} characters")
    return name


@dataclass(frozen=True)
class Snapshot:
    """Every group's owner and people, and which group each project is in."""

    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    members: dict[str, dict[str, str]] = field(default_factory=dict)  # group -> principal -> role
    project_group: dict[str, str] = field(default_factory=dict)  # project -> group


class GroupStore:
    def __init__(self, db: Database, scope: str = "", owns_db: bool = False, users: UserStore | None = None):
        self.db = db
        self.scope = scope
        self.users = users
        self._owns_db = owns_db
        _prepare(db)
        self._lock = threading.Lock()
        self._snap: tuple[Snapshot, float] | None = None
        self._generation = 0  # one more for each change, so a read begun before it is not kept

    @property
    def people(self) -> bool:
        return self.users is not None

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.db.query(sql, args)

    def _changed(self) -> None:
        with self._lock:
            self._snap = None
            self._generation += 1

    # -- reading ---------------------------------------------------------------------
    def snapshot(self, now: float | None = None, fresh: bool = False) -> Snapshot:
        now = time.time() if now is None else now
        with self._lock:
            hit = None if fresh else self._snap
            generation = self._generation
        if hit and now - hit[1] < SEEN_EVERY:
            return hit[0]
        groups = {r["id"]: r for r in self._q(
            "select id, name, owner_id, created_at, updated_at from project_groups where scope=?", (self.scope,))}
        members: dict[str, dict[str, str]] = {}
        for r in self._q("select group_id, principal, role from project_group_members where scope=?", (self.scope,)):
            members.setdefault(r["group_id"], {})[r["principal"]] = r["role"]
        projects = {r["project"]: r["group_id"] for r in self._q(
            "select project, group_id from project_group_projects where scope=?", (self.scope,))}
        snap = Snapshot(groups, members, projects)
        with self._lock:
            if self._generation == generation:  # else a change came in while reading: not kept
                self._snap = (snap, now)
        return snap

    def _snapshot_with(self, gid: str) -> Snapshot | None:
        """The snapshot, read again when it lacks ``gid`` (a group made on
        another replica a moment ago); None: no such group."""
        snap = self.snapshot()
        if gid not in snap.groups:
            snap = self.snapshot(fresh=True)
        return snap if gid in snap.groups else None

    def exists(self, gid: str) -> bool:
        return self._snapshot_with(gid) is not None

    def role(self, gid: str, ident: Identity | None) -> str | None:
        """The request's role on a group: the operator (nobody signed in) and
        admins own every group."""
        snap = self._snapshot_with(gid)
        if snap is None:
            return None
        if ident is None or ident.role == "admin":
            return "owner"
        return self._role_in(snap, gid, ident)

    @staticmethod
    def _role_in(snap: Snapshot, gid: str, ident: Identity) -> str | None:
        group = snap.groups.get(gid)
        if group is None or not ident.uid:
            return None
        if ident.uid == group["owner_id"]:
            return "owner"
        members = snap.members.get(gid, {})
        role = members.get(user_principal(ident.uid))
        if ident.email:
            role = higher(role, members.get(email_principal(ident.email)))
        return role

    def project_role(self, project: str, ident: Identity | None) -> str | None:
        """What the project's group gives this person on the project (admins
        and the operator are sharing.py's business)."""
        if ident is None or not ident.uid:
            return None
        snap = self.snapshot()
        gid = snap.project_group.get(project)
        return PROJECT_ROLE[self._role_in(snap, gid, ident)] if gid else None

    def group_of(self, project: str, fresh: bool = False) -> dict[str, Any] | None:
        """The group a project is in, ``{id, name}``; ``fresh`` reads the
        database (a run's variables must not wait for the cache)."""
        if fresh:
            rows = self._q(
                "select g.id, g.name from project_group_projects p join project_groups g "
                "on g.scope=p.scope and g.id=p.group_id where p.scope=? and p.project=?",
                (self.scope, project),
            )
            return {"id": rows[0]["id"], "name": rows[0]["name"]} if rows else None
        snap = self.snapshot()
        gid = snap.project_group.get(project)
        g = snap.groups.get(gid) if gid else None
        return {"id": g["id"], "name": g["name"]} if g else None

    def projects(self, gid: str) -> list[str]:
        return sorted(r["project"] for r in self._q(
            "select project from project_group_projects where scope=? and group_id=?", (self.scope, gid)))

    def _person(self, uid: str | None) -> dict[str, Any] | None:
        if uid is None:
            return None
        user = self.users.get(uid) if self.users is not None else None
        if user is None:
            return {"id": uid, "name": uid, "email": None, "disabled": False}
        return {"id": user.id, "name": user.name, "email": user.email, "disabled": user.disabled}

    def describe(self, gid: str) -> dict[str, Any]:
        rows = self._q("select id, name, owner_id, created_at from project_groups where scope=? and id=?", (self.scope, gid))
        if not rows:
            raise KeyError(gid)
        r = rows[0]
        return {"id": r["id"], "name": r["name"], "owner": self._person(r["owner_id"]), "created_at": r["created_at"]}

    def members(self, gid: str) -> list[dict[str, Any]]:
        """Everyone the group is shared with by name, editors first."""
        rows = self._q(
            "select principal, role, created_at, created_by from project_group_members where scope=? and group_id=? "
            "order by created_at, principal",
            (self.scope, gid),
        )
        out = []
        for r in rows:
            kind, _, key = r["principal"].partition(":")
            if kind == "user":
                out.append({**r, "kind": "user", "user": self._person(key) or {}, "pending": False})
            else:
                out.append({**r, "kind": "email", "user": {"id": None, "name": key, "email": key, "disabled": False},
                            "pending": True})
        out.sort(key=lambda m: (-RANK[m["role"]], m["user"]["name"].lower()))
        return out

    def visible(self, ident: Identity | None) -> list[dict[str, Any]]:
        """The groups this request has a role on, by name, each with that
        role and how many projects and variables it has."""
        snap = self.snapshot(fresh=True)
        counts: dict[str, int] = {}
        for gid in snap.project_group.values():
            counts[gid] = counts.get(gid, 0) + 1
        nvars = {r["group_id"]: int(r["n"]) for r in self._q(
            "select group_id, count(*) as n from project_group_env where scope=? group by group_id", (self.scope,))}
        out = []
        for gid, g in snap.groups.items():
            role = "owner" if ident is None or ident.role == "admin" else self._role_in(snap, gid, ident)
            if role is None:
                continue
            out.append({
                "id": gid, "name": g["name"], "owner": self._person(g["owner_id"]), "access": role,
                "projects": counts.get(gid, 0), "variables": nvars.get(gid, 0),
            })
        out.sort(key=lambda g: (g["name"].lower(), g["id"]))
        return out

    # -- changing ----------------------------------------------------------------------
    def create(self, name: str, owner_id: str | None, now: float | None = None) -> str:
        now = time.time() if now is None else now
        name = clean_name(name)
        if owner_id is not None and (self.users is None or self.users.get(owner_id) is None):
            raise ValueError(f"no user {owner_id} to own the group")
        # Counted per owner: one person can't use up everyone's groups.
        if owner_id is None:
            rows = self._q("select count(*) as n from project_groups where scope=? and owner_id is null", (self.scope,))
        else:
            rows = self._q("select count(*) as n from project_groups where scope=? and owner_id=?", (self.scope, owner_id))
        if int(rows[0]["n"]) >= MAX_GROUPS:
            raise ValueError(f"at most {MAX_GROUPS} groups each")
        gid = "g" + uuid.uuid4().hex[:11]
        self._q(
            "insert into project_groups (scope, id, name, owner_id, created_at, updated_at) values (?,?,?,?,?,?)",
            (self.scope, gid, name, owner_id, now, now),
        )
        self._changed()
        return gid

    def rename(self, gid: str, name: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        name = clean_name(name)
        self._q("update project_groups set name=?, updated_at=? where scope=? and id=?", (name, now, self.scope, gid))
        self._changed()

    def delete(self, gid: str) -> list[str]:
        """Delete the group and its variables. Returns the projects that were
        in it: they stay, outside any group."""
        with self.db.transaction():
            projects = self.projects(gid)
            self._q("delete from project_groups where scope=? and id=?", (self.scope, gid))
            for table in ("project_group_members", "project_group_projects", "project_group_env"):
                self._q(f"delete from {table} where scope=? and group_id=?", (self.scope, gid))
        self._changed()
        return projects

    def set_owner(self, gid: str, uid: str, by: str | None = None, now: float | None = None) -> None:
        """Make ``uid`` the owner. The previous owner stays on as an editor."""
        now = time.time() if now is None else now
        if self.users is None or self.users.get(uid) is None:
            raise KeyError(uid)
        with self.db.transaction():
            old = self.describe(gid)["owner"]
            old_id = old["id"] if old else None
            if old_id == uid:
                return
            self._q("update project_groups set owner_id=?, updated_at=? where scope=? and id=?", (uid, now, self.scope, gid))
            self._q("delete from project_group_members where scope=? and group_id=? and principal=?",
                    (self.scope, gid, user_principal(uid)))
            if old_id:
                self._upsert_member(gid, user_principal(old_id), "editor", by, now)
        self._changed()

    def _upsert_member(self, gid: str, principal: str, role: str, by: str | None, now: float) -> None:
        self._q(
            "insert into project_group_members (scope, group_id, principal, role, created_at, created_by) "
            "values (?,?,?,?,?,?) on conflict (scope, group_id, principal) do update set role=excluded.role",
            (self.scope, gid, principal, role, now, by),
        )

    def grant(
        self, gid: str, role: str, *, user_id: str | None = None, email: str | None = None,
        by: str | None = None, now: float | None = None,
    ) -> str:
        """Give someone ``role`` (viewer or editor) on the group, as
        SharingStore.grant does on a project. Returns the principal."""
        now = time.time() if now is None else now
        if role not in GRANT_ROLES:
            raise ValueError(f"role must be one of {', '.join(GRANT_ROLES)} (ownership is transferred, not granted)")
        if self.users is None:
            raise ValueError("nobody signs in on this hub, so there is nobody to add to a group")
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
        owner = self.describe(gid)["owner"]
        if user_id is not None and owner and owner["id"] == user_id:
            raise ValueError("that person owns the group")
        self._upsert_member(gid, principal, role, by, now)
        self._changed()
        return principal

    def revoke(self, gid: str, principal: str) -> bool:
        with self.db.transaction():
            found = self._q("select 1 from project_group_members where scope=? and group_id=? and principal=?",
                            (self.scope, gid, principal))
            if found:
                self._q("delete from project_group_members where scope=? and group_id=? and principal=?",
                        (self.scope, gid, principal))
        self._changed()
        return bool(found)

    def set_project_group(self, project: str, gid: str | None, by: str | None = None, now: float | None = None) -> None:
        """Put a project in a group (out of any other), or out of its group
        with ``gid=None``. The caller checks who may."""
        now = time.time() if now is None else now
        with self.db.transaction():
            self._q("delete from project_group_projects where scope=? and project=?", (self.scope, project))
            if gid is not None:
                if not self._q("select 1 from project_groups where scope=? and id=?", (self.scope, gid)):
                    raise KeyError(gid)
                self._q(
                    "insert into project_group_projects (scope, project, group_id, added_at, added_by) values (?,?,?,?,?)",
                    (self.scope, project, gid, now, by),
                )
        self._changed()

    def forget_project(self, project: str) -> None:
        """A deleted project leaves its group."""
        self._q("delete from project_group_projects where scope=? and project=?", (self.scope, project))
        self._changed()

    # -- variables (the store side of envvars.Env) -------------------------------------
    def env_rows(self, gid: str) -> list[dict[str, Any]]:
        rows = self._q(
            "select name, value, secret, updated_at, updated_by from project_group_env where scope=? and group_id=? "
            "order by name",
            (self.scope, gid),
        )
        return [{**r, "secret": bool(r["secret"])} for r in rows]

    def all_env_groups(self) -> list[str]:
        return [r["group_id"] for r in self._q(
            "select distinct group_id from project_group_env where scope=?", (self.scope,))]

    def vars(self, gid: str) -> "GroupVars":
        return GroupVars(self, gid)

    def hub_env_rows(self) -> list[dict[str, Any]]:
        rows = self._q(
            "select name, value, secret, updated_at, updated_by from hub_env where scope=? order by name", (self.scope,))
        return [{**r, "secret": bool(r["secret"])} for r in rows]

    def hub_vars(self) -> "HubVars":
        return HubVars(self)

    # -- hub export / import -------------------------------------------------------------
    def dump_to(self, target: Path) -> None:
        """This scope's groups as tables in a SQLite file (the hub export's
        ``hub.db``, beside the user list and sharing)."""
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
        """Replace this scope's groups with an archive's (none, from a hub
        before groups)."""
        con = sqlite3.connect(source)
        con.row_factory = sqlite3.Row
        try:
            present = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
            data = {t: [dict(r) for r in con.execute(f"select * from {t}")] if t in present else [] for t in TABLES}
        except sqlite3.OperationalError as e:
            raise ValueError(f"the archive's groups are unreadable: {e}") from e
        finally:
            con.close()
        with self.db.transaction():
            for table, rows in data.items():
                self._q(f"delete from {table} where scope=?", (self.scope,))
                for r in rows:
                    r["scope"] = self.scope
                    self._q(f"insert into {table} ({', '.join(r)}) values ({', '.join('?' * len(r))})", tuple(r.values()))
        self._changed()


class GroupVars:
    """One group's variables, shaped like the project Store methods that
    envvars.Env uses, so a group's variables get exactly a project's rules."""

    def __init__(self, store: GroupStore, gid: str):
        self.store = store
        self.gid = gid

    def env_vars(self) -> list[dict[str, Any]]:
        return self.store.env_rows(self.gid)

    def set_env_var(self, name: str, value: str, secret: bool, updated_by: str | None = None) -> None:
        s = self.store
        s._q(
            "insert into project_group_env (scope, group_id, name, value, secret, updated_at, updated_by) "
            "values (?,?,?,?,?,?,?) on conflict (scope, group_id, name) do update set value=excluded.value, "
            "secret=excluded.secret, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (s.scope, self.gid, name, value, int(secret), time.time(), updated_by),
        )

    def reseal_env_var(self, name: str, old: str, new: str) -> None:
        s = self.store
        s._q("update project_group_env set value=? where scope=? and group_id=? and name=? and value=?",
             (new, s.scope, self.gid, name, old))

    def delete_env_var(self, name: str) -> bool:
        s = self.store
        had = bool(s._q("select 1 from project_group_env where scope=? and group_id=? and name=?", (s.scope, self.gid, name)))
        s._q("delete from project_group_env where scope=? and group_id=? and name=?", (s.scope, self.gid, name))
        return had


class HubVars:
    """The hub's own variables, shaped like GroupVars (the project Store
    methods envvars.Env uses)."""

    def __init__(self, store: GroupStore):
        self.store = store

    def env_vars(self) -> list[dict[str, Any]]:
        return self.store.hub_env_rows()

    def set_env_var(self, name: str, value: str, secret: bool, updated_by: str | None = None) -> None:
        s = self.store
        s._q(
            "insert into hub_env (scope, name, value, secret, updated_at, updated_by) values (?,?,?,?,?,?) "
            "on conflict (scope, name) do update set value=excluded.value, secret=excluded.secret, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (s.scope, name, value, int(secret), time.time(), updated_by),
        )

    def reseal_env_var(self, name: str, old: str, new: str) -> None:
        s = self.store
        s._q("update hub_env set value=? where scope=? and name=? and value=?", (new, s.scope, name, old))

    def delete_env_var(self, name: str) -> bool:
        s = self.store
        had = bool(s._q("select 1 from hub_env where scope=? and name=?", (s.scope, name)))
        s._q("delete from hub_env where scope=? and name=?", (s.scope, name))
        return had


def open_groups(local: Path, users: UserStore | None, sharing: Any = None) -> GroupStore:
    """A hub's groups: in the database its user list or sharing uses, else
    the shared ``EXUVIA_DB``, else ``local`` (the hub's SQLite file)."""
    if users is not None:
        return GroupStore(users.db, scope=users.scope, users=users)
    if sharing is not None:
        return GroupStore(sharing.db, scope=sharing.scope)
    url = database_url()
    if url:
        return GroupStore(shared_database(url), scope=scope_prefix())
    from .db import SQLiteDatabase

    local.parent.mkdir(parents=True, exist_ok=True)
    return GroupStore(SQLiteDatabase(local), owns_db=True)
