"""When each person last opened and last changed each hub project.

The hub's home page sorts its projects by it ("Recently used", "Recently
changed") and puts the ones you worked on lately in a group of their own.

Every request to a project's API passes the hub's access check
(``create_hub_app``'s ``get_ctx``), which calls ``touch``: any request opens
the project, one that needs edit access (saving code, running a node, the
agent, creating the project) changes it. Writes are kept to one a minute per
person and project, so a workbench polling its project costs nothing.

The rows live in the database the hub's groups use (groups.py: the hub's
``.exuvia/hub.db``, or the shared ``EXUVIA_DB``). They are bookkeeping, not
data: a hub export leaves them out, and a failed write never fails the
request it was noted for.
"""

from __future__ import annotations

import sys
import threading
import time
import weakref
from typing import Any

from .db import Database
from .store import SCHEMA_LOCK_KEY

# How often one person's use of one project is written (seconds).
TOUCH_EVERY = 60.0
# Who did it when nobody is signed in: a hub without people, the API token or
# a share link.
NOBODY = ""

TABLES = {
    "project_activity": """create table if not exists project_activity (
    scope text not null default '',
    project text not null,
    user_id text not null,       -- users.py id; '' when nobody was signed in
    opened_at double precision,  -- their latest request to the project
    edited_at double precision,  -- their latest change to it (anything that needs edit access)
    primary key (scope, project, user_id)
)""",
}
INDEXES = ("create index if not exists project_activity_user on project_activity(scope, user_id)",)

_prepared: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _prepare(db: Database) -> None:
    """Create the table once per database (see users._prepare)."""
    if db in _prepared:
        return
    if not all(db.columns(t) for t in TABLES):
        with db.transaction():
            if db.dialect == "postgres":
                db.query(f"select pg_advisory_xact_lock({SCHEMA_LOCK_KEY})")
            for ddl in (*TABLES.values(), *INDEXES):
                db.query(ddl)
    _prepared.add(db)


class ActivityStore:
    def __init__(self, db: Database, scope: str = "", owns_db: bool = False):
        self.db = db
        self.scope = scope
        self._owns_db = owns_db
        _prepare(db)
        self._lock = threading.Lock()
        # (project, user, edited) -> when it was last written from here.
        self._written: dict[tuple[str, str, bool], float] = {}

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def touch(self, project: str, uid: str | None, edited: bool = False, now: float | None = None) -> None:
        """Note that ``uid`` (None: nobody signed in) opened ``project``, and
        changed it when ``edited``."""
        now = time.time() if now is None else now
        user = uid or NOBODY
        key = (project, user, edited)
        with self._lock:
            if now - self._written.get(key, float("-inf")) < TOUCH_EVERY:
                return
            self._written[key] = now
        try:
            self.db.query(
                "insert into project_activity(scope, project, user_id, opened_at, edited_at) values(?,?,?,?,?) "
                "on conflict(scope, project, user_id) do update set opened_at=excluded.opened_at, "
                "edited_at=coalesce(excluded.edited_at, project_activity.edited_at)",
                (self.scope, project, user, now, now if edited else None),
            )
        except Exception as e:  # noqa: BLE001 - recency is worth less than the request it rides on
            with self._lock:
                self._written.pop(key, None)
            print(f"exuvia: could not note activity on {project}: {e}", file=sys.stderr)

    def of(self, uid: str | None) -> dict[str, dict[str, float | None]]:
        """One person's (None: nobody signed in) ``opened_at`` and
        ``edited_at`` on each project they used."""
        rows = self.db.query(
            "select project, opened_at, edited_at from project_activity where scope=? and user_id=?",
            (self.scope, uid or NOBODY),
        )
        return {r["project"]: {"opened_at": r["opened_at"], "edited_at": r["edited_at"]} for r in rows}

    def changed(self) -> dict[str, float]:
        """When anyone last changed each project."""
        rows = self.db.query(
            "select project, max(edited_at) as edited_at from project_activity "
            "where scope=? and edited_at is not null group by project",
            (self.scope,),
        )
        return {r["project"]: r["edited_at"] for r in rows}

    def forget(self, project: str) -> None:
        """A deleted project's rows (and a new one of the same id starts clean)."""
        self.db.query("delete from project_activity where scope=? and project=?", (self.scope, project))
        with self._lock:
            self._written = {k: v for k, v in self._written.items() if k[0] != project}

