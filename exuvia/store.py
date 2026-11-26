"""Run history, schedules, agent sessions and code revisions.

The rows live in a ``Database`` (``db.py``): by default a SQLite file at
``.exuvia/exuvia.db`` in the project, or a shared SQLite or Postgres database
when ``EXUVIA_DB`` is set. Every row carries its project's scope, so one
database can hold many projects.

A node's *current* output is the artifact of its latest successful run **for a
given set of run parameters** (``params_key``; empty when the node depends on
no parameter). Staleness is derived from what that run consumed versus what is
current now, so there is no separate "dirty" flag to drift out of sync.

Environment variables (``env_vars``) are the project's own settings for its
nodes: API keys and the like, set in the workbench (see ``envvars.py``).

Code revisions record every version of the project's files (node files,
``exuvia.json``, ``AGENTS.md``) as it was saved. The text is kept once per
content hash in ``code_blobs``, keyed by the same hash runs record in
``code_hash``, so the exact source behind any run can be looked up. A code
tag (``code_tags``) names the whole project's code as of one revision.
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
    kind text,              -- table | json | multi | none (pickle: written by older versions, never read)
    rows integer,
    schema text,            -- json [{name, type}]
    log text default '',
    error text,
    params text,            -- json {name: value}: the parameters this node depends on
    params_key text not null default '',
    started_by text,        -- the signed-in person who started it, or whose run cascaded into it, by name (see auth.py)
    started_by_user text,   -- the same person's user id (users.py)
    version integer,        -- a partitioned node's output: its manifest version (output_partitions; 0: empty)
    partition_by text,      -- and the column it is partitioned by
    unique_key text,        -- json [column]: the key its whole output was checked against (keys.py)
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
    title text,                -- the first thing the person asked, shortened, for the list of conversations
    created_at double precision,
    started_by text,           -- the signed-in person who started it, by name (see auth.py); null when nobody was
    started_by_user text,      -- the same person's user id (users.py)
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
    author text,               -- the signed-in person who saved it, by name (see auth.py); null when nobody was
    author_user text,          -- the same person's user id (users.py)
    primary key (project, id)
)""",
    # A name for the whole project's code as of one revision (git's tags):
    # every file's latest revision up to and including it. Never moved: a
    # tag is deleted and made again instead, so a name always meant one thing.
    "code_tags": """create table if not exists code_tags (
    project text not null default '',
    name text not null,        -- v1, q3-review, ...: one per project (see workspace.TAG_NAME)
    revision text not null,    -- code_revisions.id: the last revision of the save it was made at
    message text,              -- what it marks, in words
    author text,               -- the signed-in person who made it, by name (see auth.py)
    author_user text,          -- the same person's user id (users.py)
    created_at double precision not null,
    primary key (project, name)
)""",
    "env_vars": """create table if not exists env_vars (
    project text not null default '',
    name text not null,        -- an environment variable every node of the project gets (see envvars.py)
    value text not null,       -- a secret's is encrypted: enc:v1:<key id>:<token> (envvars.Keyring)
    secret integer not null default 1,   -- 1: never shown again once saved, and hidden in run logs
    updated_at double precision not null,
    updated_by text,           -- the signed-in person who last set it, by name (see auth.py)
    primary key (project, name)
)""",
    # A partitioned node's output (partitioned.py), one manifest version per
    # successful run that changed it. A row is part of every version from
    # `since` up to, not including, `until`, so storage grows with the
    # partitions written, not with partitions times runs.
    "output_partitions": """create table if not exists output_partitions (
    project text not null default '',
    node text not null,
    params_key text not null default '',
    value text not null,       -- one partition: the partition column's value as text
    digest text not null,      -- SHA-256 of its rows as text, sorted (partitioned.py)
    rows integer not null,
    since integer not null,    -- the version that wrote it
    until integer,             -- the version that replaced or dropped it; null while current
    primary key (project, node, params_key, value, since)
)""",
    "output_files": """create table if not exists output_files (
    project text not null default '',
    node text not null,
    params_key text not null default '',
    file text not null,        -- a Parquet chunk, relative to the project root (.exuvia/artifacts/<node>/<sha>.parquet)
    partitions text not null,  -- json: the partition values it holds
    rows integer not null,
    since integer not null,
    until integer,
    primary key (project, node, params_key, file, since)
)""",
}

INDEXES = (
    "create index if not exists runs_node on runs(project, node, queued_at)",
    "create index if not exists runs_node_key on runs(project, node, params_key, queued_at)",
    "create index if not exists code_revisions_path on code_revisions(project, path, saved_at)",
    "create index if not exists code_revisions_node on code_revisions(project, node, saved_at)",
    "create index if not exists code_revisions_view on code_revisions(project, view_name, saved_at)",
    "create index if not exists output_partitions_version on output_partitions(project, node, params_key, since)",
    "create index if not exists output_files_version on output_files(project, node, params_key, since)",
    "create index if not exists output_files_file on output_files(project, file)",
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

# Columns agent_sessions gained after it first shipped (all may be null).
AGENT_SESSION_ADDED = (
    ("title", "text"),
    ("created_at", "double precision"),
    ("started_by", "text"),
    ("started_by_user", "text"),
)

# How long a conversation's title is in the list of conversations.
TITLE_CHARS = 80

RUN_COLS = (
    "id,node,batch_id,trigger,status,queued_at,started_at,finished_at,"
    "artifact_id,kind,rows,error,params,params_key,started_by,started_by_user,version,partition_by,unique_key"
)

# One changed file for ``Store.record_code``: (path, node, view, hash,
# source). ``node`` / ``view`` name the node or view the file belongs to (or
# None); hash and source are None for a deletion.
CodeChange = tuple[str, str | None, str | None, str | None, str | None]

# One version of a partitioned output as ``Store.manifest`` reads it:
# ({value: (digest, rows)}, [{file, partitions, rows}]).
Manifest = tuple[dict[str, tuple[str, int]], list[dict[str, Any]]]

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
    if cols and "started_by" not in cols:  # from before signed-in users
        db.query("alter table runs add column started_by text")
    if cols and "started_by_user" not in cols:  # from before the user list
        db.query("alter table runs add column started_by_user text")
    if cols and "version" not in cols:  # from before partitioned outputs
        db.query("alter table runs add column version integer")
        db.query("alter table runs add column partition_by text")
    if cols and "unique_key" not in cols:  # from before keys
        db.query("alter table runs add column unique_key text")
    cols = db.columns("code_revisions")
    if cols and "view_name" not in cols:  # from before views existed
        db.query("alter table code_revisions add column view_name text")
    if cols and "author" not in cols:  # from before signed-in users
        db.query("alter table code_revisions add column author text")
    if cols and "author_user" not in cols:  # from before the user list
        db.query("alter table code_revisions add column author_user text")
    cols = db.columns("agent_sessions")
    for col, kind in AGENT_SESSION_ADDED:  # from before the list of conversations
        if cols and col not in cols:
            db.query(f"alter table agent_sessions add column {col} {kind}")
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
        self,
        node: str,
        batch_id: str,
        trigger: str,
        params: dict[str, Any] | None = None,
        params_key: str = "",
        started_by: str | None = None,
        started_by_user: str | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        self._q(
            "insert into runs (project, id, node, batch_id, trigger, status, queued_at, params, params_key, "
            "started_by, started_by_user) values (?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.scope, run_id, node, batch_id, trigger, "queued", time.time(),
                json.dumps(params or {}), params_key, started_by, started_by_user,
            ),
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

    def last_by_trigger(self, node: str, trigger: str) -> dict[str, Any] | None:
        """The latest run of ``node`` that ``trigger`` started (``schedule``,
        ``live``, ...), for any parameter set."""
        rows = self._q(
            "select id,status,trigger,queued_at,started_at,finished_at,error,rows,params,params_key "
            "from runs where project=? and node=? and trigger=? order by queued_at desc limit 1",
            (self.scope, node, trigger),
        )
        return _row(rows[0]) if rows else None

    def last_attempt(self, node: str, params_key: str = "") -> dict[str, Any] | None:
        """The latest run that executed the node's code: one that succeeded or
        failed. Runs that were cached or skipped, or are still queued, didn't
        try, so they say nothing about whether the node works."""
        rows = self._q(
            "select id,status,queued_at,error from runs where project=? and node=? and params_key=? "
            "and status in ('success','failed') order by queued_at desc limit 1",
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
            "and artifact_path not in (select path from stored_artifacts where project=?) "
            "union select distinct file from output_files where project=? "
            "and file not in (select path from stored_artifacts where project=?)",
            (self.scope, self.scope, self.scope, self.scope),
        )
        return [r["artifact_path"] for r in rows]

    def param_sets(self) -> list[dict[str, Any]]:
        """Every distinct parameter set any run has used."""
        rows = self._q(
            "select distinct params from runs where project=? and params is not null and params != '{}'",
            (self.scope,),
        )
        return [json.loads(r["params"]) for r in rows]

    # -- partitioned outputs (partitioned.py) ------------------------------------
    def manifest(self, node: str, params_key: str, version: int | None) -> Manifest:
        """The partitions ({value: (digest, rows)}) and files of one version
        of a partitioned output; empty for version None or 0."""
        if not version:
            return {}, []
        where = "project=? and node=? and params_key=? and since<=? and (until is null or until>?)"
        args = (self.scope, node, params_key, version, version)
        parts = {
            r["value"]: (r["digest"], int(r["rows"]))
            for r in self._q(f"select value, digest, rows from output_partitions where {where}", args)
        }
        return parts, self.manifest_files(node, params_key, version)

    def manifest_files(self, node: str, params_key: str, version: int | None) -> list[dict[str, Any]]:
        """``manifest``'s files alone (what a reader needs)."""
        if not version:
            return []
        rows = self._q(
            "select file, partitions, rows from output_files where project=? and node=? and params_key=? "
            "and since<=? and (until is null or until>?) order by file",
            (self.scope, node, params_key, version, version),
        )
        return [{"file": r["file"], "partitions": json.loads(r["partitions"]), "rows": int(r["rows"])} for r in rows]

    def record_manifest(self, node: str, params_key: str, new: Manifest, run_id: str, **run: Any) -> int:
        """Record ``new`` as the next version of a partitioned output (what
        differs from the current version: partitions and files it no longer
        has are closed, new ones added) and mark run ``run_id`` successful
        with it (and ``run``'s other fields), in one transaction: a version
        exists only with its run. Returns the version."""
        new_parts, new_files = new
        key = (self.scope, node, params_key)
        with self.db.transaction():
            (row,) = self._q(
                "select max(v) as v from (select max(coalesce(until, since)) as v from output_partitions "
                "where project=? and node=? and params_key=? union all select max(coalesce(until, since)) "
                "from output_files where project=? and node=? and params_key=?) t",
                (*key, *key),
            )
            version = int(row["v"] or 0) + 1
            open_parts = {
                r["value"]: r["digest"]
                for r in self._q(
                    "select value, digest from output_partitions where project=? and node=? and params_key=? "
                    "and until is null",
                    key,
                )
            }
            open_files = {
                r["file"]
                for r in self._q(
                    "select file from output_files where project=? and node=? and params_key=? and until is null", key
                )
            }
            for value, digest in open_parts.items():
                if (new_parts.get(value) or (None,))[0] != digest:
                    self._q(
                        "update output_partitions set until=? where project=? and node=? and params_key=? "
                        "and value=? and until is null",
                        (version, *key, value),
                    )
            for value, (digest, rows) in new_parts.items():
                if open_parts.get(value) != digest:
                    self._q(
                        "insert into output_partitions (project, node, params_key, value, digest, rows, since) "
                        "values (?,?,?,?,?,?,?)",
                        (*key, value, digest, rows, version),
                    )
            kept = {f["file"] for f in new_files}
            for file in open_files - kept:
                self._q(
                    "update output_files set until=? where project=? and node=? and params_key=? "
                    "and file=? and until is null",
                    (version, *key, file),
                )
            for f in new_files:
                if f["file"] not in open_files:
                    self._q(
                        "insert into output_files (project, node, params_key, file, partitions, rows, since) "
                        "values (?,?,?,?,?,?,?)",
                        (*key, f["file"], json.dumps(f["partitions"]), f["rows"], version),
                    )
            self.update_run(run_id, status="success", version=version, **run)
        return version

    def chunk(self, node: str, name: str) -> str | None:
        """The recorded path of chunk ``name`` (``<sha>.parquet``) of
        ``node``'s partitioned output, when some version lists it."""
        for r in self._q(
            "select distinct file from output_files where project=? and node=? and file like ?",
            (self.scope, node, f"%{name}"),
        ):
            if r["file"].rsplit("/", 1)[-1] == name:
                return r["file"]
        return None

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

    def save_agent_session(
        self,
        session_id: str,
        history: list[Any],
        events: list[Any],
        started_by: str | None = None,
        started_by_user: str | None = None,
    ) -> None:
        """Save a conversation. Its title (the first thing asked), when it was
        started and who started it are set by the first save and kept."""
        now = time.time()
        self._q(
            "insert into agent_sessions(project, id, history, events, updated_at, title, created_at, started_by, "
            "started_by_user) values(?,?,?,?,?,?,?,?,?) "
            "on conflict(project, id) do update set history=excluded.history, events=excluded.events, "
            "updated_at=excluded.updated_at, title=coalesce(agent_sessions.title, excluded.title)",
            (
                self.scope,
                session_id,
                json.dumps(history, default=_dump),
                json.dumps(events, default=str),
                now,
                _session_title(events),
                now,
                started_by,
                started_by_user,
            ),
        )

    def agent_sessions(
        self, *, started_by_user: str | None = None, started_by: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Saved conversations, the latest first, without their transcripts:
        every one, or only those one person started (by user id, or by name
        where there is no user list)."""
        sql = (
            "select id, title, created_at, updated_at, started_by from agent_sessions where project=?"
        )
        args: list[Any] = [self.scope]
        if started_by_user is not None:
            sql += " and started_by_user=?"
            args.append(started_by_user)
        if started_by is not None:
            sql += " and started_by=?"
            args.append(started_by)
        sql += " order by updated_at desc limit ?"
        args.append(int(limit))
        return [
            {
                "id": r["id"],
                "title": r["title"] or "",
                "created_at": r["created_at"] if r["created_at"] is not None else r["updated_at"],
                "updated_at": r["updated_at"],
                "started_by": r["started_by"],
            }
            for r in self._q(sql, tuple(args))
        ]

    def delete_agent_session(self, session_id: str) -> None:
        self._q("delete from agent_sessions where project=? and id=?", (self.scope, session_id))

    # -- environment variables (envvars.py) ----------------------------------------
    def env_vars(self) -> list[dict[str, Any]]:
        """Every variable, by name, with its value: for runs, never for clients."""
        rows = self._q(
            "select name, value, secret, updated_at, updated_by from env_vars where project=? order by name",
            (self.scope,),
        )
        return [{**r, "secret": bool(r["secret"])} for r in rows]

    def set_env_var(self, name: str, value: str, secret: bool, updated_by: str | None = None) -> None:
        self._q(
            "insert into env_vars (project, name, value, secret, updated_at, updated_by) values (?,?,?,?,?,?) "
            "on conflict (project, name) do update set value=excluded.value, secret=excluded.secret, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (self.scope, name, value, int(secret), time.time(), updated_by),
        )

    def reseal_env_var(self, name: str, old: str, new: str) -> None:
        """Replace a secret's stored form (encrypted again, same value),
        unless someone changed it in between."""
        self._q("update env_vars set value=? where project=? and name=? and value=?", (new, self.scope, name, old))

    def delete_env_var(self, name: str) -> bool:
        had = bool(self._q("select 1 from env_vars where project=? and name=?", (self.scope, name)))
        self._q("delete from env_vars where project=? and name=?", (self.scope, name))
        return had

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

    def record_code(
        self,
        changes: list[CodeChange],
        origin: str,
        now: float | None = None,
        author: str | None = None,
        author_user: str | None = None,
    ) -> int:
        """Record changed files as new revisions (see ``CodeChange``),
        saved by ``author`` (the signed-in person's name, if any; their user
        id is ``author_user``) through ``origin``."""
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
                    "insert into code_revisions(project, id, path, node, view_name, hash, origin, author, author_user, "
                    "saved_at) values(?,?,?,?,?,?,?,?,?,?)",
                    (self.scope, revision_id(), path, node, view, digest, origin, author, author_user, saved_at),
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
            f"select id, path, node, view_name, hash, origin, author, author_user, saved_at from code_revisions where {where} "
            "order by saved_at desc, id desc limit ?",
            (*args, limit),
        )
        for r in rows:
            r["view"] = r.pop("view_name")
        return rows

    def code_revisions_after(self, after: str | None, limit: int = 1000) -> list[dict[str, Any]]:
        """Revisions saved after revision ``after`` (from the first when it
        is None), oldest first: the order ``exuvia mirror`` replays them in,
        a page at a time. KeyError when no revision has the id ``after``."""
        where, args = "project=?", [self.scope]
        if after:
            found = self._q("select saved_at from code_revisions where project=? and id=?", (self.scope, after))
            if not found:
                raise KeyError(after)
            t = found[0]["saved_at"]
            where += " and (saved_at > ? or (saved_at = ? and id > ?))"
            args += [t, t, after]
        rows = self._q(
            f"select id, path, node, view_name, hash, origin, author, author_user, saved_at from code_revisions where {where} "
            "order by saved_at, id limit ?",
            (*args, limit),
        )
        for r in rows:
            r["view"] = r.pop("view_name")
        return rows

    def code_source(self, digest: str) -> str | None:
        """The text with this content hash, if any revision recorded it."""
        rows = self._q("select source from code_blobs where project=? and hash=?", (self.scope, digest))
        return rows[0]["source"] if rows else None

    def code_revision(self, rid: str) -> dict[str, Any] | None:
        rows = self._q(
            "select id, path, node, view_name, hash, origin, author, author_user, saved_at from code_revisions "
            "where project=? and id=?",
            (self.scope, rid),
        )
        if not rows:
            return None
        rows[0]["view"] = rows[0].pop("view_name")
        return rows[0]

    def save_end(self, rid: str) -> str | None:
        """The last revision of the save revision ``rid`` belongs to (the
        files one save recorded share its time, origin and author, as
        ``exuvia mirror`` groups them into one commit); None when there is no
        revision ``rid``. With no ``rid``, the project's latest revision."""
        if not rid:
            rows = self._q(
                "select id from code_revisions where project=? order by saved_at desc, id desc limit 1", (self.scope,)
            )
            return rows[0]["id"] if rows else None
        r = self.code_revision(rid)
        if r is None:
            return None
        rows = self._q(
            "select id from code_revisions where project=? and saved_at=? and origin=? "
            "and coalesce(author, '')=? and coalesce(author_user, '')=? order by id desc limit 1",
            (self.scope, r["saved_at"], r["origin"], r["author"] or "", r["author_user"] or ""),
        )
        return rows[0]["id"]

    def code_at(self, rid: str) -> dict[str, dict[str, Any]]:
        """The project's code as of revision ``rid``: each file's latest
        revision up to and including it, by path, leaving out files deleted
        by then. Empty when there is no revision ``rid``."""
        r = self.code_revision(rid)
        if r is None:
            return {}
        t = r["saved_at"]
        rows = self._q(
            "select id, path, node, view_name, hash, origin, author, author_user, saved_at from (select *, row_number() "
            "over (partition by path order by saved_at desc, id desc) rn from code_revisions where project=? "
            "and (saved_at < ? or (saved_at = ? and id <= ?))) upto where rn=1 and hash is not null order by path",
            (self.scope, t, t, rid),
        )
        out = {}
        for row in rows:
            row["view"] = row.pop("view_name")
            out[row["path"]] = row
        return out

    # -- code tags ----------------------------------------------------------
    def create_tag(
        self,
        name: str,
        revision: str,
        message: str | None = None,
        author: str | None = None,
        author_user: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Name the code as of ``revision``. ValueError when the name is taken."""
        with self.db.transaction():
            if self._q("select 1 from code_tags where project=? and name=?", (self.scope, name)):
                raise ValueError(f"there is already a tag {name}: delete it first to tag another version with that name")
            self._q(
                "insert into code_tags(project, name, revision, message, author, author_user, created_at) "
                "values(?,?,?,?,?,?,?)",
                (self.scope, name, revision, message or None, author, author_user, time.time() if now is None else now),
            )
        tag = self.code_tag(name)
        assert tag is not None
        return tag

    def code_tags(self) -> list[dict[str, Any]]:
        """Every tag, newest first, with when its revision was saved."""
        return self._q(
            "select t.name, t.revision, t.message, t.author, t.author_user, t.created_at, r.saved_at "
            "from code_tags t left join code_revisions r on r.project = t.project and r.id = t.revision "
            "where t.project=? order by t.created_at desc, t.name",
            (self.scope,),
        )

    def code_tag(self, name: str) -> dict[str, Any] | None:
        return next((t for t in self.code_tags() if t["name"] == name), None)

    def delete_tag(self, name: str) -> bool:
        had = bool(self._q("select 1 from code_tags where project=? and name=?", (self.scope, name)))
        self._q("delete from code_tags where project=? and name=?", (self.scope, name))
        return had


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


def _session_title(events: list[Any]) -> str | None:
    """A conversation's title: the first thing the person asked, on one line."""
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "user":
            text = " ".join(str(ev.get("text", "")).split())
            return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rstrip() + "…"
    return None
