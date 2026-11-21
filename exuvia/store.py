"""Run history, schedules, agent sessions and code revisions.

The rows live in a ``Database`` (``db.py``): by default a SQLite file at
``.exuvia/exuvia.db`` in the project, or a shared SQLite or Postgres database
when ``EXUVIA_DB`` is set. Every row carries its project's scope, so one
database can hold many projects.

A node's *current* output is the artifact of its latest successful run **for a
given set of run parameters** (``params_key``; empty when the node depends on
no parameter). Staleness is derived from what that run consumed versus what is
current now, so there is no separate "dirty" flag to drift out of sync.

Code revisions record every version of the project's files (node files,
``exuvia.json``, ``AGENTS.md``) as it was saved. The text is kept once per
content hash in ``code_blobs``, keyed by the same hash runs record in
``code_hash``, so the exact source behind any run can be looked up.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .db import Database, SQLiteDatabase, database_url, scope_prefix, shared_database

if TYPE_CHECKING:
    from .project import Project

# One dialect for every backend (see db.py). `project` is the scope column.
TABLES = {
    "runs": """create table if not exists runs (
    project text not null default '',
    id text not null,
    node text not null,
    batch_id text,
    trigger text not null,
    status text not null,
    queued_at double precision not null,
    started_at double precision,
    finished_at double precision,
    code_hash text,
    inputs text,            -- json {dep: artifact_id}
    artifact_id text,
    artifact_path text,     -- relative to the project root
    kind text,              -- table | json | none (pickle: written by older versions, never read)
    rows integer,
    schema text,            -- json [{name, type}]
    log text default '',
    error text,
    params text,            -- json {name: value}: the parameters this node depends on
    params_key text not null default '',
    primary key (project, id)
)""",
    "schedules": """create table if not exists schedules (
    project text not null default '',
    node text not null,
    paused integer not null default 0,
    primary key (project, node)
)""",
    "schedule_state": """create table if not exists schedule_state (
    project text not null default '',
    node text not null,
    cron text not null,
    last_fired double precision not null,   -- also "first seen" for a new/changed cron
    primary key (project, node)
)""",
    "agent_sessions": """create table if not exists agent_sessions (
    project text not null default '',
    id text not null,
    history text not null,     -- json: provider message history
    events text not null,      -- json: UI transcript
    updated_at double precision not null,
    primary key (project, id)
)""",
    "stored_artifacts": """create table if not exists stored_artifacts (
    project text not null default '',
    path text not null,        -- artifact_path of an output that is in the remote artifact store
    primary key (project, path)
)""",
    "code_blobs": """create table if not exists code_blobs (
    project text not null default '',
    hash text not null,        -- project._hash(source), the same value runs.code_hash holds
    source text not null,
    primary key (project, hash)
)""",
    "code_revisions": """create table if not exists code_revisions (
    project text not null default '',
    id text not null,
    path text not null,        -- relative to the project root: nodes/x.py, views/v/App.tsx, exuvia.json, ...
    node text,                 -- node name for a node file
    hash text,                 -- code_blobs.hash; null when the file was deleted
    origin text not null,      -- who saved it: agent, api, file, load, template, restore, ...
    saved_at double precision not null,
    view_name text,            -- view name for a file under views/<name>/
    primary key (project, id)
)""",
}

INDEXES = (
    "create index if not exists runs_node on runs(project, node, queued_at)",
    "create index if not exists runs_node_key on runs(project, node, params_key, queued_at)",
    "create index if not exists code_revisions_path on code_revisions(project, path, saved_at)",
    "create index if not exists code_revisions_node on code_revisions(project, node, saved_at)",
    "create index if not exists code_revisions_view on code_revisions(project, view_name, saved_at)",
)

# Columns each table had before scopes existed, in order (for the rebuild).
LEGACY_COLUMNS = {
    "runs": (
        "id,node,batch_id,trigger,status,queued_at,started_at,finished_at,code_hash,inputs,"
        "artifact_id,artifact_path,kind,rows,schema,log,error,params,params_key"
    ),
    "schedules": "node,paused",
    "schedule_state": "node,cron,last_fired",
    "agent_sessions": "id,history,events,updated_at",
    "stored_artifacts": "path",
}

RUN_COLS = (
    "id,node,batch_id,trigger,status,queued_at,started_at,finished_at,"
    "artifact_id,kind,rows,error,params,params_key"
)

# One changed file for ``Store.record_code``: (path, node, view, hash,
# source). ``node`` / ``view`` name the node or view the file belongs to (or
# None); hash and source are None for a deletion.
CodeChange = tuple[str, str | None, str | None, str | None, str | None]

# Who changed the code, recorded on each revision.
ORIGIN_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-")


def clean_origin(origin: str | None, default: str) -> str:
    o = (origin or "").strip().lower()[:32]
    return o if o and set(o) <= ORIGIN_CHARS else default


_revision_lock = threading.Lock()
_last_revision_ns = 0


def revision_id() -> str:
    """A unique id that sorts after every id this process made before it.

    Revisions are read newest first by ``saved_at``, and two saves can share a
    ``saved_at``: the clock does not tick between them (``time.time()`` moves
    in 15.6 ms steps on Windows before Python 3.13) or one change records
    several files. The id breaks such ties in the order the revisions were
    written: 16 hex digits of a strictly increasing nanosecond counter, then
    16 random ones so ids from different processes cannot collide."""
    global _last_revision_ns
    with _revision_lock:
        _last_revision_ns = max(time.time_ns(), _last_revision_ns + 1)
        ns = _last_revision_ns
    return f"{ns:016x}{uuid.uuid4().hex[:16]}"


_prepared: "weakref.WeakSet[Any]" = weakref.WeakSet()

# Postgres advisory lock key that serializes schema creation and upgrades
# across processes. Any fixed 64-bit value works; this one is "exuvia" in hex.
SCHEMA_LOCK_KEY = 0x657875766961


def prepare(db: Database) -> None:
    """Create or upgrade the schema once per database connection.

    Postgres does not make ``create table if not exists`` atomic: two
    processes creating the same table at once (hub replicas starting together
    on a new database) can fail with a duplicate ``pg_type`` key. A
    transaction-level advisory lock makes every other process wait until the
    first commits, then find the tables (and upgraded columns) in place.
    SQLite's ``begin immediate`` already serializes the whole block."""
    if db in _prepared:
        return
    with db.transaction():
        if db.dialect == "postgres":
            db.query(f"select pg_advisory_xact_lock({SCHEMA_LOCK_KEY})")
        _upgrade_legacy(db)
        for ddl in TABLES.values():
            db.query(ddl)
        for ddl in INDEXES:
            db.query(ddl)
    _prepared.add(db)


def _upgrade_legacy(db: Database) -> None:
    """Project databases written before scopes existed: rebuild each table
    with the ``project`` column (rows keep the empty scope)."""
    cols = db.columns("runs")
    if cols and "params" not in cols:
        db.query("alter table runs add column params text")
    if cols and "params_key" not in cols:
        db.query("alter table runs add column params_key text not null default ''")
    cols = db.columns("code_revisions")
    if cols and "view_name" not in cols:  # from before views existed
        db.query("alter table code_revisions add column view_name text")
    for table, legacy in LEGACY_COLUMNS.items():
        have = db.columns(table)
        if not have or "project" in have:
            continue
        db.query(f"alter table {table} rename to {table}_legacy")
        db.query(TABLES[table])
        db.query(f"insert into {table} ({legacy}) select {legacy} from {table}_legacy")
        db.query(f"drop table {table}_legacy")  # its old indexes go with it


def open_store(project: "Project") -> "Store":
    """The store for ``project``, in whichever database ``EXUVIA_DB`` names."""
    url = database_url()
    if not url:
        return Store(SQLiteDatabase(project.state_dir / "exuvia.db"), scope="", owns_db=True)
    return Store(shared_database(url), scope=project_scope(project.root.name), owns_db=False)


def project_scope(project_id: str) -> str:
    """The scope a hub project has in a shared database."""
    return scope_prefix() + project_id


class Store:
    def __init__(self, db: Database, scope: str = "", owns_db: bool = True):
        self.db = db
        self.scope = scope
        self._owns_db = owns_db
        prepare(db)

    def fail_interrupted(self) -> None:
        """Runs left queued or running by a process that died never finish;
        mark them failed. Only the process now serving the project calls this."""
        self._q(
            "update runs set status='failed', error='interrupted (server restarted)', finished_at=? "
            "where project=? and status in ('queued','running')",
            (time.time(), self.scope),
        )

    def _q(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return self.db.query(sql, tuple(args))

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def backup_to(self, target: Path) -> None:
        """A consistent copy of the database (safe while runs are writing).
        Only for a per-project SQLite file (``path`` is set)."""
        self.db.backup_to(target)

    def delete_all(self) -> None:
        """Remove every row of this scope (the project is being deleted)."""
        with self.db.transaction():
            for table in TABLES:
                self._q(f"delete from {table} where project=?", (self.scope,))

    # -- runs --------------------------------------------------------------
    def create_run(
        self, node: str, batch_id: str, trigger: str, params: dict[str, Any] | None = None, params_key: str = ""
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        self._q(
            "insert into runs (project, id, node, batch_id, trigger, status, queued_at, params, params_key) "
            "values (?,?,?,?,?,?,?,?,?)",
            (self.scope, run_id, node, batch_id, trigger, "queued", time.time(), json.dumps(params or {}), params_key),
        )
        return run_id

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        for k in ("inputs", "schema"):
            if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
                fields[k] = json.dumps(fields[k])
        cols = ", ".join(f"{k}=?" for k in fields)
        self._q(f"update runs set {cols} where project=? and id=?", (*fields.values(), self.scope, run_id))

    def append_log(self, run_id: str, text: str) -> None:
        self._q("update runs set log = coalesce(log,'') || ? where project=? and id=?", (text, self.scope, run_id))

    def run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._q("select * from runs where project=? and id=?", (self.scope, run_id))
        return _row(rows[0]) if rows else None

    def runs_for(self, node: str, limit: int = 30, params_key: str | None = None) -> list[dict[str, Any]]:
        where, args = "project=? and node=?", [self.scope, node]
        if params_key is not None:
            where += " and params_key=?"
            args.append(params_key)
        rows = self._q(f"select {RUN_COLS} from runs where {where} order by queued_at desc limit ?", (*args, limit))
        return [_row(r) for r in rows]

    def recent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._q(
            f"select {RUN_COLS} from runs where project=? order by queued_at desc limit ?", (self.scope, limit)
        )
        return [_row(r) for r in rows]

    def last_success(self, node: str, params_key: str = "") -> dict[str, Any] | None:
        rows = self._q(
            "select * from runs where project=? and node=? and params_key=? and status='success' "
            "order by finished_at desc limit 1",
            (self.scope, node, params_key),
        )
        return _row(rows[0]) if rows else None

    def run_with_artifact(self, node: str, artifact_id: str) -> dict[str, Any] | None:
        """The latest successful run of ``node`` whose output is ``artifact_id``."""
        rows = self._q(
            "select * from runs where project=? and node=? and artifact_id=? and status='success' "
            "order by finished_at desc limit 1",
            (self.scope, node, artifact_id),
        )
        return _row(rows[0]) if rows else None

    def last_run(self, node: str, params_key: str = "") -> dict[str, Any] | None:
        rows = self._q(
            "select id,status,trigger,queued_at,started_at,finished_at,error,rows,params,params_key "
            "from runs where project=? and node=? and params_key=? order by queued_at desc limit 1",
            (self.scope, node, params_key),
        )
        return _row(rows[0]) if rows else None

    def history(self, node: str, limit: int = 400) -> list[dict[str, Any]]:
        """The current output of ``node`` for every parameter set it has run
        with: one row per ``params_key``, most recent first."""
        rows = self._q(
            "select * from (select runs.*, row_number() over (partition by params_key order by finished_at desc) rn "
            "from runs where project=? and node=? and status='success') latest where rn=1 "
            "order by finished_at desc limit ?",
            (self.scope, node, limit),
        )
        out = []
        for r in rows:
            d = _row(r)
            d.pop("rn", None)
            d.pop("log", None)
            d.pop("project", None)
            out.append(d)
        return out

    # -- remote artifact store ------------------------------------------------
    def mark_stored(self, path: str) -> None:
        self._q(
            "insert into stored_artifacts(project, path) values(?,?) on conflict(project, path) do nothing",
            (self.scope, path),
        )

    def stored_paths(self) -> set[str]:
        return {r["path"] for r in self._q("select path from stored_artifacts where project=?", (self.scope,))}

    def unstored_outputs(self) -> list[str]:
        """Outputs recorded by successful runs that are not in the remote store yet."""
        rows = self._q(
            "select distinct artifact_path from runs where project=? and status='success' "
            "and artifact_path is not null "
            "and artifact_path not in (select path from stored_artifacts where project=?)",
            (self.scope, self.scope),
        )
        return [r["artifact_path"] for r in rows]

    def param_sets(self) -> list[dict[str, Any]]:
        """Every distinct parameter set any run has used."""
        rows = self._q(
            "select distinct params from runs where project=? and params is not null and params != '{}'",
            (self.scope,),
        )
        return [json.loads(r["params"]) for r in rows]

    # -- schedules ---------------------------------------------------------
    def paused(self) -> set[str]:
        return {r["node"] for r in self._q("select node from schedules where project=? and paused=1", (self.scope,))}

    def set_paused(self, node: str, paused: bool) -> None:
        self._q(
            "insert into schedules(project, node, paused) values(?,?,?) "
            "on conflict(project, node) do update set paused=excluded.paused",
            (self.scope, node, int(paused)),
        )

    def schedule_states(self) -> dict[str, dict[str, Any]]:
        rows = self._q("select node, cron, last_fired from schedule_state where project=?", (self.scope,))
        return {r["node"]: r for r in rows}

    def set_schedule_state(self, node: str, cron: str, last_fired: float) -> None:
        self._q(
            "insert into schedule_state(project, node, cron, last_fired) values(?,?,?,?) "
            "on conflict(project, node) do update set cron=excluded.cron, last_fired=excluded.last_fired",
            (self.scope, node, cron, last_fired),
        )

    # -- agent sessions ----------------------------------------------------
    def agent_session(self, session_id: str) -> dict[str, Any] | None:
        rows = self._q(
            "select history, events, updated_at from agent_sessions where project=? and id=?",
            (self.scope, session_id),
        )
        if not rows:
            return None
        r = rows[0]
        return {"history": json.loads(r["history"]), "events": json.loads(r["events"]), "updated_at": r["updated_at"]}

    def save_agent_session(self, session_id: str, history: list[Any], events: list[Any]) -> None:
        self._q(
            "insert into agent_sessions(project, id, history, events, updated_at) values(?,?,?,?,?) "
            "on conflict(project, id) do update set history=excluded.history, events=excluded.events, "
            "updated_at=excluded.updated_at",
            (
                self.scope,
                session_id,
                json.dumps(history, default=_dump),
                json.dumps(events, default=str),
                time.time(),
            ),
        )

    def delete_agent_session(self, session_id: str) -> None:
        self._q("delete from agent_sessions where project=? and id=?", (self.scope, session_id))

    # -- code revisions -----------------------------------------------------
    def latest_code(self) -> dict[str, str | None]:
        """Each recorded path's latest content hash (None: deleted)."""
        rows = self._q(
            "select path, hash from (select path, hash, row_number() over "
            "(partition by path order by saved_at desc, id desc) rn from code_revisions where project=?) latest "
            "where rn=1",
            (self.scope,),
        )
        return {r["path"]: r["hash"] for r in rows}

    def record_code(self, changes: list[CodeChange], origin: str, now: float | None = None) -> int:
        """Record changed files as new revisions (see ``CodeChange``)."""
        if not changes:
            return 0
        saved_at = time.time() if now is None else now
        with self.db.transaction():
            for path, node, view, digest, source in changes:
                if digest is not None:
                    self._q(
                        "insert into code_blobs(project, hash, source) values(?,?,?) "
                        "on conflict(project, hash) do nothing",
                        (self.scope, digest, source),
                    )
                self._q(
                    "insert into code_revisions(project, id, path, node, view_name, hash, origin, saved_at) "
                    "values(?,?,?,?,?,?,?,?)",
                    (self.scope, revision_id(), path, node, view, digest, origin, saved_at),
                )
        return len(changes)

    def code_revisions(
        self, *, node: str | None = None, view: str | None = None, path: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Revisions newest first, optionally for one node, one view's files
        or one path."""
        where, args = "project=?", [self.scope]
        for column, value in (("node", node), ("view_name", view), ("path", path)):
            if value is not None:
                where += f" and {column}=?"
                args.append(value)
        rows = self._q(
            f"select id, path, node, view_name, hash, origin, saved_at from code_revisions where {where} "
            "order by saved_at desc, id desc limit ?",
            (*args, limit),
        )
        for r in rows:
            r["view"] = r.pop("view_name")
        return rows

    def code_source(self, digest: str) -> str | None:
        """The text with this content hash, if any revision recorded it."""
        rows = self._q("select source from code_blobs where project=? and hash=?", (self.scope, digest))
        return rows[0]["source"] if rows else None


def _dump(obj: Any) -> Any:
    """Provider SDK objects (pydantic content blocks) -> plain JSON."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    return str(obj)


def _row(r: dict[str, Any]) -> dict[str, Any]:
    d = dict(r)
    d.pop("project", None)
    for k in ("inputs", "schema", "params"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d


def stored_paths_at(db: Path) -> set[str]:
    """``Store.stored_paths`` for a per-project database file that is not
    open (an unloaded project, or a copy of one)."""
    import sqlite3

    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute("select path from stored_artifacts")}
    except sqlite3.OperationalError:
        return set()  # created before the remote store existed
    finally:
        con.close()
