"""People who have signed in, whichever way they signed in.

A *user* is a person as exuvia knows them: a stable id, the name and email
their latest sign-in gave, a role (``admin`` or ``member``) and whether they
are disabled. An *identity* is one way of signing in: a method (``headers``
now; OIDC and others later) and the subject that method names them by (the
user header's value, an OIDC ``sub``, ...). Every identity belongs to one
user, so a new sign-in method adds identities to the people already here and
their history stays theirs.

Anyone the sign-in method lets in is recorded on their first request (no
approval step). A new identity joins the user that already has its email,
since the methods that give an email vouch for it; otherwise it starts a new
user. ``EXUVIA_ADMINS`` (user names or emails, comma-separated) are admins
whenever they sign in, and the API can neither demote nor disable them, so a
hub can't lock out its last admin. Admins can make others admins and disable
or re-enable anyone else; a disabled user is turned away even when the
sign-in proxy lets them through.

Users belong to the server, not to a project: one table for a whole hub (in
``.exuvia/hub.db`` under the hub's directory, or the shared ``EXUVIA_DB``
under ``EXUVIA_DB_SCOPE``), the project's own database for ``exuvia serve``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auth import Identity
from .db import Database, database_url, scope_prefix, shared_database

ROLES = ("admin", "member")
# How long a sign-in is answered from memory before the database is read
# (and last_seen written) again. Also how long another replica can take to
# notice that a user was disabled.
SEEN_EVERY = 60.0

TABLES = {
    "users": """create table if not exists users (
    scope text not null default '',
    id text not null,
    name text not null,          -- what their latest sign-in called them: user name, else email
    email text,                  -- lower case
    role text not null default 'member',
    disabled integer not null default 0,
    created_at double precision not null,
    last_seen double precision not null,
    primary key (scope, id)
)""",
    "user_identities": """create table if not exists user_identities (
    scope text not null default '',
    method text not null,        -- how they signed in: headers, ...
    subject text not null,       -- who that method says they are
    user_id text not null,
    email text,
    created_at double precision not null,
    last_seen double precision not null,
    primary key (scope, method, subject)
)""",
}
INDEXES = (
    "create index if not exists users_email on users(scope, email)",
    "create index if not exists user_identities_user on user_identities(scope, user_id)",
)
USER_COLS = "id, name, email, role, disabled, created_at, last_seen"

_prepared: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _prepare(db: Database) -> None:
    if db in _prepared:
        return
    with db.transaction():
        for ddl in (*TABLES.values(), *INDEXES):
            db.query(ddl)
    _prepared.add(db)


def pinned_admins() -> set[str]:
    """``EXUVIA_ADMINS``, lower case."""
    return {a.strip().lower() for a in os.environ.get("EXUVIA_ADMINS", "").split(",") if a.strip()}


def _subject(ident: Identity) -> str:
    return ident.user or ident.email or ""


def _email(ident: Identity) -> str | None:
    return ident.email.lower() if ident.email else None


@dataclass(frozen=True)
class User:
    id: str
    name: str
    email: str | None
    role: str
    disabled: bool
    created_at: float
    last_seen: float

    @property
    def pinned(self) -> bool:
        """Listed in EXUVIA_ADMINS: always an admin, never disabled."""
        admins = pinned_admins()
        return self.name.lower() in admins or (self.email or "") in admins

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "email": self.email, "role": self.role,
            "disabled": self.disabled, "pinned_admin": self.pinned,
            "created_at": self.created_at, "last_seen": self.last_seen,
        }


def _user(r: dict[str, Any]) -> User:
    return User(r["id"], r["name"], r["email"], r["role"], bool(r["disabled"]), r["created_at"], r["last_seen"])


class UserStore:
    def __init__(self, db: Database, scope: str = "", owns_db: bool = False):
        self.db = db
        self.scope = scope
        self._owns_db = owns_db
        _prepare(db)
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str], tuple[User, float]] = {}

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.db.query(sql, args)

    # -- signing in ----------------------------------------------------------
    def recent(self, ident: Identity, now: float | None = None) -> User | None:
        """The user this identity signed in as less than SEEN_EVERY ago, from
        memory (None: ask ``sign_in``)."""
        now = time.time() if now is None else now
        with self._lock:
            hit = self._seen.get((ident.method, _subject(ident)))
        if hit and now - hit[1] < SEEN_EVERY and hit[0].name == ident.id and hit[0].email == (_email(ident) or hit[0].email):
            return hit[0]
        return None

    def sign_in(self, ident: Identity, now: float | None = None) -> User:
        """The user behind this identity, recorded (or brought up to date)
        now: created on first sight, linked by email to a user who already
        has it, and an admin when EXUVIA_ADMINS names them."""
        now = time.time() if now is None else now
        cached = self.recent(ident, now)
        if cached is not None:
            return cached
        method, subject, email = ident.method, _subject(ident), _email(ident)
        admins = pinned_admins()
        pinned = ident.id.lower() in admins or (email or "") in admins
        with self.db.transaction():
            uid = self._identity_owner(method, subject)
            if uid is None:
                found = (
                    self._q(
                        "select id from users where scope=? and email=? order by created_at, id limit 1",
                        (self.scope, email),
                    )
                    if email else []
                )
                uid = found[0]["id"] if found else uuid.uuid4().hex[:12]
                if not found:
                    self._q(
                        f"insert into users (scope, {USER_COLS}) values (?,?,?,?,?,?,?,?)",
                        (self.scope, uid, ident.id, email, "admin" if pinned else "member", 0, now, now),
                    )
                # Two replicas can meet the same new identity at once: the
                # first insert wins and the other one follows it.
                self._q(
                    "insert into user_identities (scope, method, subject, user_id, email, created_at, last_seen) "
                    "values (?,?,?,?,?,?,?) on conflict (scope, method, subject) do nothing",
                    (self.scope, method, subject, uid, email, now, now),
                )
                winner = self._identity_owner(method, subject)
                if winner != uid and not found:
                    self._q("delete from users where scope=? and id=?", (self.scope, uid))
                uid = winner or uid
            self._q(
                "update user_identities set email=coalesce(?, email), last_seen=? where scope=? and method=? and subject=?",
                (email, now, self.scope, method, subject),
            )
            self._q(
                "update users set name=?, email=coalesce(?, email), last_seen=?"
                + (", role='admin', disabled=0" if pinned else "")
                + " where scope=? and id=?",
                (ident.id, email, now, self.scope, uid),
            )
            rows = self._q(f"select {USER_COLS} from users where scope=? and id=?", (self.scope, uid))
        user = _user(rows[0])
        with self._lock:
            self._seen[(method, subject)] = (user, now)
        return user

    def _identity_owner(self, method: str, subject: str) -> str | None:
        rows = self._q(
            "select user_id from user_identities where scope=? and method=? and subject=?", (self.scope, method, subject)
        )
        if rows and self._q("select 1 from users where scope=? and id=?", (self.scope, rows[0]["user_id"])):
            return rows[0]["user_id"]
        if rows:  # its user is gone: start over
            self._q("delete from user_identities where scope=? and method=? and subject=?", (self.scope, method, subject))
        return None

    # -- administration --------------------------------------------------------
    def get(self, uid: str) -> User | None:
        rows = self._q(f"select {USER_COLS} from users where scope=? and id=?", (self.scope, uid))
        return _user(rows[0]) if rows else None

    def list(self) -> list[dict[str, Any]]:
        """Every user, most recently seen first, with the identities they
        signed in with."""
        users = [_user(r) for r in self._q(f"select {USER_COLS} from users where scope=? order by last_seen desc, id", (self.scope,))]
        idents: dict[str, list[dict[str, Any]]] = {}
        for r in self._q(
            "select user_id, method, subject, email, created_at, last_seen from user_identities where scope=? "
            "order by created_at",
            (self.scope,),
        ):
            idents.setdefault(r.pop("user_id"), []).append(r)
        return [{**u.as_dict(), "identities": idents.get(u.id, [])} for u in users]

    def update(self, uid: str, *, role: str | None = None, disabled: bool | None = None) -> User:
        """Change a user's role or disable / re-enable them. KeyError: no such
        user; ValueError: not allowed (an unknown role, or demoting or
        disabling someone EXUVIA_ADMINS names)."""
        user = self.get(uid)
        if user is None:
            raise KeyError(uid)
        if role is not None and role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if user.pinned and (role == "member" or disabled):
            raise ValueError(f"{user.name} is listed in EXUVIA_ADMINS: always an admin, and cannot be disabled")
        sets, args = [], []
        if role is not None:
            sets.append("role=?")
            args.append(role)
        if disabled is not None:
            sets.append("disabled=?")
            args.append(1 if disabled else 0)
        if sets:
            self._q(f"update users set {', '.join(sets)} where scope=? and id=?", (*args, self.scope, uid))
            with self._lock:
                self._seen.clear()  # the next request reads the change
        return self.get(uid)  # type: ignore[return-value]

    # -- hub export / import ------------------------------------------------------
    def dump_to(self, target: Path) -> None:
        """This scope's users and identities as a SQLite file (hub export)."""
        con = sqlite3.connect(target)
        try:
            for ddl in (*TABLES.values(), *INDEXES):
                con.execute(ddl)
            for table in TABLES:
                rows = self._q(f"select * from {table} where scope=?", (self.scope,))
                for r in rows:
                    cols = ", ".join(r)
                    con.execute(f"insert into {table} ({cols}) values ({', '.join('?' * len(r))})", tuple(r.values()))
            con.commit()
        finally:
            con.close()

    def replace_from(self, source: Path) -> int:
        """Replace this scope's users with the ones in a file ``dump_to``
        wrote (hub import). Returns how many users there are now."""
        con = sqlite3.connect(source)
        con.row_factory = sqlite3.Row
        try:
            data = {t: [dict(r) for r in con.execute(f"select * from {t}")] for t in TABLES}
        except sqlite3.OperationalError as e:
            raise ValueError(f"the archive's user list is unreadable: {e}") from e
        finally:
            con.close()
        with self.db.transaction():
            for table, rows in data.items():
                self._q(f"delete from {table} where scope=?", (self.scope,))
                for r in rows:
                    r["scope"] = self.scope
                    cols = ", ".join(r)
                    self._q(f"insert into {table} ({cols}) values ({', '.join('?' * len(r))})", tuple(r.values()))
        with self._lock:
            self._seen.clear()
        return len(data["users"])


def open_users(local: Path | Database) -> UserStore:
    """The user list: in the shared ``EXUVIA_DB`` when one is set, else in
    ``local`` (a SQLite file for a hub, the project's database for serve)."""
    url = database_url()
    if url:
        return UserStore(shared_database(url), scope=scope_prefix())
    if isinstance(local, Path):
        from .db import SQLiteDatabase

        local.parent.mkdir(parents=True, exist_ok=True)
        return UserStore(SQLiteDatabase(local), owns_db=True)
    return UserStore(local)
