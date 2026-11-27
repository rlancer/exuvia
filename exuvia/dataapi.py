"""The data API: read-only access to many hub projects' outputs at once.

For apps built on exuvia data that need more than its views give them. A
data key (datakeys.py) names the projects an app may read; with it the app
can

* ``GET /api/data/projects``: list those projects and their tables;
* ``GET /api/data/projects/{project}/nodes/{node}``: read one node's output;
* ``GET /api/data/projects/{project}/nodes/{node}/artifacts/{artifact_id}.parquet``:
  fetch a stored output by its content hash, cached for good;
* ``POST /api/data/query`` (or ``GET`` with ``?sql=``): run one DuckDB
  ``SELECT`` across all of them, each project a catalog of its own
  (``"sales".orders``, ``"crm".accounts``),

as JSON or as an Arrow stream. People manage their keys at
``/api/data-keys``.

The query is the fast path. Its connection is the sandbox
``outputs.outputs_db`` builds (only the files behind its views are
readable, nothing else, and the settings are locked), with each project's
views attached in a catalog named after the project. Building one costs tens
of milliseconds, so the connection is kept and each request gets a cursor
on it (microseconds); it is built again once any of its projects changes
(a run finishes, code is saved) or after CACHE_TTL, which bounds how long a
change made on another replica takes to show. Only projects whose id
appears in the SQL are attached, unless the request names its ``projects``.
Only one ``SELECT`` (or ``EXPLAIN``) runs per request, so nothing a key
sends can change a connection other requests share. Requests that find
the connection stale together wait for one build (a run finishing makes it
stale for every request at once).

Answers carry an ``ETag`` that names what they were made from, never a
counter of one process: a node read's names the run and the stored output
it reads, a query's the SQL, its limit and the views of the projects it
attached (their files are content-addressed). A ``GET`` with that tag in
``If-None-Match`` answers 304 without running DuckDB or writing JSON. By
default answers say ``Cache-Control: no-cache`` (a cache may keep one, but
asks again before each use, so it is never stale); CACHE_SECONDS lets
caches use one for that long, and as long again while they fetch a new
one. ``Vary: Authorization`` keeps each key's answers apart in a shared
cache.

A hub with no cache in front of it still answers a repeated JSON query
from memory (ResultCache): the encoded answer, kept while none of the
projects it read has changed and for at most CACHE_TTL, within a budget of
RESULT_CACHE_MB. Requests for the same query that arrive while it runs wait
for that one run.

One key may have KEY_CONCURRENCY queries and node reads running at once
(more answer 429), and a query stops after QUERY_TIMEOUT, so one app can't
take every core from the others. ``scripts/loadtest_data_api.py`` measures
what a hub serves.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Awaitable, Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, Security
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, auth
from .arrowstream import wants_arrow
from fastapi.responses import FileResponse

from .http_util import (
    ARTIFACT_EXTENSIONS, ARTIFACT_KINDS, ARTIFACT_MEDIA_TYPES, FastJSON, arrow_response, need_partitioned,
    partition_range, query_params, resolve_params,
)
from .datakeys import MAX_PROJECTS, TURNED_AWAY, DataKey, DataKeyStore, clean_projects, looks_like_key
from .sharing import at_least
from .outputs import lock_outputs, output_parts, outputs_connection, query_arrow, query_json
from .workspace import Workspace

PREFIX = "/api/data/"
CACHE_TTL = 5.0  # seconds a query connection is reused without a local change
CACHE_MAX = 32  # query connections kept (one per set of projects queried together)
ACCESS_TTL = 10.0  # seconds a key's readable projects are answered from memory
JSON_ROWS = 500  # rows a JSON answer has unless `limit` says otherwise
JSON_MAX = 5000
# Seconds a query may run before it is stopped (as Arrow: until its first
# rows; after that the client's reading paces it). 0 turns it off.
QUERY_TIMEOUT = float(os.environ.get("EXUVIA_DATA_QUERY_TIMEOUT") or 60)
# Queries and node reads one key may have running at once; more answer 429.
# Below DATA_WORKERS, so one app's requests never take every worker. 0
# turns it off.
KEY_CONCURRENCY = int(os.environ.get("EXUVIA_DATA_KEY_CONCURRENCY") or 4)
# Threads the data API's blocking work runs on: its own, not the event
# loop's default pool (cpu + 4 threads) the rest of the hub shares, so a
# burst of queries neither waits behind the hub's work nor holds it up.
DATA_WORKERS = int(os.environ.get("EXUVIA_DATA_WORKERS") or max(16, 2 * (os.cpu_count() or 1)))
# Seconds a cache (a browser's, a proxy's) may use a data API answer without
# asking the hub again; as long again it may answer with it while it fetches
# a new one (stale-while-revalidate). 0: every use is checked first (no-cache).
CACHE_SECONDS = float(os.environ.get("EXUVIA_DATA_CACHE_SECONDS") or 0)
# An answer depends on the request's key and on Accept (JSON or Arrow).
VARY = "Authorization, Accept"
# Functions whose value changes between two runs of the same query over the
# same data: a query using one has no ETag (and is never cached). False
# positives only cost a 304.
VOLATILE = re.compile(
    r"(?<![a-z0-9_])(random|setseed|uuid|uuidv4|uuidv7|gen_random_uuid|now|today|age|current_date|current_time"
    r"|current_timestamp|current_localtime|current_localtimestamp|localtime|localtimestamp|get_current_time"
    r"|get_current_timestamp|transaction_timestamp|nextval|currval|txid_current|current_query|current_query_id"
    r"|current_transaction_id|current_connection_id|getvariable|sample|tablesample|reservoir_quantile)(?![a-z0-9_])"
)
# The answers of JSON queries kept in memory (ResultCache): MiB in all
# (0 turns it off), the most one answer may take, and how long a query must
# have taken to be worth keeping.
RESULT_CACHE_MB = float(os.environ.get("EXUVIA_DATA_RESULT_CACHE_MB") or 64)
RESULT_MAX_BYTES = 2 * 2**20
RESULT_MIN_SECONDS = 0.010
# DuckDB's own catalogs: a project of one of these names can't be attached.
RESERVED_CATALOGS = {"main", "memory", "system", "temp"}


class DataKeyReq(BaseModel):
    name: str = ""
    projects: list[str]


class DataKeyPatch(BaseModel):
    name: str | None = None
    projects: list[str] | None = None


class DataQueryReq(BaseModel):
    sql: str = Field(description='One SELECT (or EXPLAIN). Each project is a catalog: `"sales".orders`.')
    limit: int | None = Field(None, ge=0, description="Rows to return. JSON: 500 unless given, 5000 at most. Arrow: every row unless given.")
    projects: list[str] | None = Field(
        None, description="The projects to attach. Default: the key's projects whose id appears in `sql`."
    )


# -- the answers, typed for the data API's OpenAPI spec -----------------------------------
class Column(BaseModel):
    name: str
    type: str = Field(description="The DuckDB type (query answers) or the pandas dtype the run recorded (node listings).")


class Part(BaseModel):
    name: str
    kind: str | None = Field(None, description="`table` or `json`.")
    rows: int | None = None


class DataNode(BaseModel):
    name: str
    description: str | None = Field(None, description="The node's docstring.")
    kind: Literal["table", "multi", "json", "none"] | None = Field(
        None, description="Its current output (default parameters): null before its first successful run."
    )
    rows: int | None = None
    columns: list[Column] | None = Field(None, description="For a table output.")
    parts: list[Part] | None = Field(None, description="For a node with several outputs: each is `<project>.<node>.<part>`.")
    run_id: str | None = None
    artifact_id: str | None = Field(None, description="The stored output's content hash: it changes when the data does.")
    url: str | None = Field(
        None, description="The stored output as a file (Parquet, JSON, or a zip of several), named by `artifact_id`: "
        "it never changes, so caches keep it for good. Null for a partitioned output."
    )
    finished_at: float | None = Field(None, description="When the run that made it finished (Unix seconds).")
    partitioned: bool
    history: bool = Field(description="Whether `<project>.<node>__runs` exists: its output for every parameter set it ran with.")


class DataProject(BaseModel):
    id: str = Field(description="The project id, which is also its catalog name in SQL.")
    name: str
    nodes: list[DataNode]


class KeyRef(BaseModel):
    id: str
    name: str


class DataProjects(BaseModel):
    key: KeyRef
    projects: list[DataProject]


class QueryAnswer(BaseModel):
    columns: list[Column]
    rows: list[list[Any]] = Field(description="Values as JSON: decimals as numbers, dates and times as ISO 8601 text, NaN as null.")
    projects: list[str] = Field(description="The projects that were attached.")


class NodeOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["table", "multi", "json", "none"] | None
    columns: list[Column] | None = None
    rows: list[list[Any]] | None = Field(None, description="For a table: the first `limit` rows from `offset`.")
    total: int | None = Field(None, description="For a table: how many rows it has.")
    value: Any = Field(None, description="For a JSON output: the value.")
    parts: list[Part] | None = Field(None, description="For a node with several outputs, without `part`.")
    run_id: str | None = None
    run_params: dict[str, Any] | None = None
    finished_at: float | None = None


class Problem(BaseModel):
    detail: str


ARROW_DOC = {"application/vnd.apache.arrow.stream": {"schema": {"type": "string", "format": "binary"}}}
PROBLEMS: dict[int | str, dict[str, Any]] = {
    400: {"model": Problem, "description": "Bad SQL, a statement that isn't a SELECT, or bad parameters."},
    401: {"model": Problem, "description": "No data key, or an unknown or revoked one."},
    403: {"model": Problem, "description": "The person who made the key can no longer sign in."},
    404: {"model": Problem, "description": "A project the key does not read, or a node it does not have."},
}
SPEC_DESCRIPTION = """Read-only access to the outputs of the hub projects a data key reads.

Send the key as `Authorization: Bearer exd_...` (people make keys in the hub's
account menu, **Data API keys**). Start with `GET /api/data/projects` to see the
projects and their nodes, then read a node's output, or run one SQL `SELECT`
across projects with `POST /api/data/query`: each project is a DuckDB catalog
named after it (`"sales".orders`, `"sales".orders__runs`, `"sales".prices.close`).

Every data route answers JSON, or an Arrow IPC stream (typed columns, every row)
with `Accept: application/vnd.apache.arrow.stream`. The key never changes
anything and never reads more than the person who made it can open."""


# A request's data key and the projects it may read now.
Access = tuple[DataKey, tuple[str, ...]]

bearer = HTTPBearer(
    scheme_name="dataKey", bearerFormat="exd_...", auto_error=False,
    description="A data API key, made in the hub's account menu (Data API keys).",
)


def check_select(cur: Any, sql: str) -> None:
    """Raise ValueError unless ``sql`` is one SELECT (or EXPLAIN). The
    statement's kind is not all that keeps a query from writing: EXPLAIN
    ANALYZE runs what it explains, so every query also runs in a read-only
    transaction (``read_only``)."""
    import duckdb

    try:
        statements = cur.extract_statements(sql)
    except duckdb.Error as e:
        raise ValueError(f"{type(e).__name__}: {e}") from e
    if len(statements) != 1:
        raise ValueError("send one SELECT statement per request")
    kind = str(statements[0].type).rsplit(".", 1)[-1]
    if kind not in ("SELECT", "EXPLAIN"):
        raise ValueError(f"the data API is read-only: only SELECT runs here, not {kind}")


def named_in(sql: str, projects: tuple[str, ...] | list[str]) -> list[str]:
    """The projects ``sql`` names: each whose id is in it as a word of its
    own (`domain` does not name a project called `main`). A project named
    like one of DuckDB's own catalogs is only attached when asked for by
    name (``projects``): `crm.main.accounts` must not fail for a key that
    also reads a project called `main`."""
    text = sql.lower()
    return [
        p
        for p in projects
        if p.lower() not in RESERVED_CATALOGS
        and re.search(rf"(?<![a-z0-9_]){re.escape(p.lower())}(?![a-z0-9_])", text)
    ]


def read_only(cur: Any) -> Any:
    """``cur`` in a transaction that cannot write, until it is closed. The
    connection under it is shared by every key that reads the same projects,
    so nothing one request sends may change what the next one reads."""
    cur.execute("begin transaction read only")
    return cur


class QueryConnections:
    """Locked DuckDB connections over sets of projects, kept between
    requests (see the module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # projects -> (stamp, built at, connection, digest of its views)
        self._cons: "OrderedDict[tuple[str, ...], tuple[Any, float, Any, str]]" = OrderedDict()
        # projects -> the lock held while that set's connection is built, so
        # requests that miss together wait for one build instead of each
        # making their own (a run finishing invalidates the connection for
        # every request at once).
        self._building: dict[tuple[str, ...], threading.Lock] = {}
        self.builds = 0  # connections built, for tests and the load test

    @staticmethod
    def _stamp(workspaces: dict[str, Workspace]) -> tuple[Any, ...]:
        return tuple((pid, id(ws), ws.bus.version) for pid, ws in sorted(workspaces.items()))

    def _cached(self, key: tuple[str, ...], stamp: tuple[Any, ...]) -> tuple[Any, str] | None:
        with self._lock:
            hit = self._cons.get(key)
            if hit and hit[0] == stamp and time.monotonic() - hit[1] < CACHE_TTL:
                self._cons.move_to_end(key)
                return hit[2].cursor(), hit[3]
        return None

    def cursor(self, workspaces: dict[str, Workspace]) -> Any:
        """A cursor on a connection with each project's outputs in a catalog
        named after it. Blocking (a build may download outputs)."""
        return self.open(workspaces)[0]

    def kept(self, workspaces: dict[str, Workspace]) -> tuple[Any, str] | None:
        """``open`` when the connection is kept and current; None when it
        would have to be built."""
        return self._cached(tuple(sorted(workspaces)), self._stamp(workspaces))

    def open(self, workspaces: dict[str, Workspace]) -> tuple[Any, str]:
        """``cursor``, and a digest of what the connection reads: its views,
        each project's files named relative to the project (content-
        addressed), so the same data has the same digest in every process.
        Blocking."""
        key = tuple(sorted(workspaces))
        stamp = self._stamp(workspaces)
        hit = self._cached(key, stamp)
        if hit is not None:
            return hit
        with self._lock:
            building = self._building.setdefault(key, threading.Lock())
        with building:
            hit = self._cached(key, stamp)  # built while this request waited
            if hit is not None:
                return hit
            return self._build(key, workspaces, stamp)

    @staticmethod
    def _digest(statements: list[str], workspaces: dict[str, Workspace]) -> str:
        text = "\n".join(statements)
        for pid, ws in workspaces.items():
            text = text.replace(str(ws.project.root), f"<{pid}>")
        return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()

    def _build(self, key: tuple[str, ...], workspaces: dict[str, Workspace], stamp: tuple[Any, ...]) -> tuple[Any, str]:
        now = time.monotonic()
        con = outputs_connection()
        rec = Recorder(con)  # what the views are made of, for the digest
        files: list[str] = []
        for pid in key:
            if pid.lower() in RESERVED_CATALOGS:
                raise ValueError(
                    f"project {pid} can't be queried with others: {pid} is the name of one of DuckDB's own "
                    f"catalogs. Read its nodes at {PREFIX}projects/{pid}/nodes/<node>"
                )
            files += workspaces[pid].add_output_views(rec, None, catalog=pid)
        digest = self._digest(rec.statements, workspaces)
        lock_outputs(con, files)
        with self._lock:
            self.builds += 1
            self._cons[key] = (stamp, now, con, digest)
            self._cons.move_to_end(key)
            while len(self._cons) > CACHE_MAX:
                old, _ = self._cons.popitem(last=False)  # a cursor still reading keeps it alive
                self._building.pop(old, None)
        return con.cursor(), digest

    def clear(self) -> None:
        with self._lock:
            self._cons.clear()
            self._building.clear()


class Recorder:
    """A DuckDB connection that notes every statement executed on it (and
    its parameters): what a query connection's views are made of."""

    def __init__(self, con: Any) -> None:
        self._con = con
        self.statements: list[str] = []

    def execute(self, sql: str, *args: Any) -> Any:
        self.statements.append(sql if not args else f"{sql} {json.dumps(args, default=str)}")
        return self._con.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


class ResultCache:
    """Encoded JSON query answers, kept in memory so a repeated query is
    answered without DuckDB.

    An answer is keyed by the projects it attached and their versions (as
    QueryConnections' stamp), its SQL and its limit, and is used for at most
    CACHE_TTL (which bounds how long a change on another replica takes to
    show). The answers together take at most ``budget`` bytes, the least
    recently used going first; one larger than ``RESULT_MAX_BYTES``, or from
    a query that took less than ``RESULT_MIN_SECONDS``, is not kept. Errors
    never are.

    Identical queries that arrive while one runs wait for it (``flight``),
    kept or not: a run finishing makes every dashboard's query miss at once."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self._lock = threading.Lock()
        # key -> (body, etag, kept at)
        self._entries: "OrderedDict[tuple[Any, ...], tuple[bytes, str | None, float]]" = OrderedDict()
        self._flights: dict[tuple[Any, ...], "Flight"] = {}
        self.bytes = 0
        self.hits = self.misses = self.coalesced = self.stored = self.evictions = self.skipped = 0

    def get(self, key: tuple[Any, ...]) -> tuple[bytes, str | None] | None:
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and time.monotonic() - hit[2] < CACHE_TTL:
                self._entries.move_to_end(key)
                self.hits += 1
                return hit[0], hit[1]
            if hit is not None:
                self._drop(key)
        return None

    def put(self, key: tuple[Any, ...], body: bytes, tag: str | None, took: float) -> None:
        size = len(body)
        if self.budget <= 0 or size > min(RESULT_MAX_BYTES, self.budget) or took < RESULT_MIN_SECONDS:
            with self._lock:
                self.skipped += 1
            return
        with self._lock:
            if key in self._entries:
                self._drop(key)
            self._entries[key] = (body, tag, time.monotonic())
            self.bytes += size
            self.stored += 1
            while self.bytes > self.budget:
                old = next(iter(self._entries))
                self._drop(old)
                self.evictions += 1

    def _drop(self, key: tuple[Any, ...]) -> None:
        body = self._entries.pop(key)[0]
        self.bytes -= len(body)

    def flight(self, key: tuple[Any, ...]) -> tuple["Flight", bool]:
        """The run of ``key`` in flight, and whether this request leads it
        (runs the query) or follows (waits for the leader's answer)."""
        with self._lock:
            f = self._flights.get(key)
            if f is not None:
                self.coalesced += 1
                return f, False
            self.misses += 1
            f = self._flights[key] = Flight()
            return f, True

    def land(self, key: tuple[Any, ...], f: "Flight", answer: tuple[bytes, str | None] | None) -> None:
        """The leader's answer (None: it failed) to the requests following it."""
        with self._lock:
            if self._flights.get(key) is f:
                del self._flights[key]
        f.answer = answer
        f.done.set()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.bytes = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries), "bytes": self.bytes, "budget": self.budget, "hits": self.hits,
                "misses": self.misses, "coalesced": self.coalesced, "stored": self.stored,
                "evictions": self.evictions, "not_kept": self.skipped,
            }


class Flight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.answer: tuple[bytes, str | None] | None = None


class KeySlots:
    """The queries and node reads each key has running, up to
    KEY_CONCURRENCY at once. Taken in the worker thread that runs the
    request (a request cancelled before its thread starts takes none) and
    given back when the answer is written, or its Arrow stream read."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, int] = {}

    def take(self, kid: str) -> Callable[[], None]:
        """Take a slot (429 when the key has none left); returns what gives
        it back, which may be called more than once."""
        limit = KEY_CONCURRENCY
        with self._lock:
            held = self._held.get(kid, 0)
            if limit > 0 and held >= limit:
                raise HTTPException(
                    429, f"this key already has {held} requests running, its limit: wait for one to finish",
                    headers={"Retry-After": "1"},
                )
            self._held[kid] = held + 1
        given = False

        def give() -> None:
            nonlocal given
            with self._lock:
                if given:
                    return
                given = True
                left = self._held.get(kid, 1) - 1
                if left > 0:
                    self._held[kid] = left
                else:
                    self._held.pop(kid, None)

        return give

    def held(self, kid: str) -> int:
        with self._lock:
            return self._held.get(kid, 0)


class Finally:
    """An Arrow stream that calls ``done`` once it has been read, or
    dropped unread (a client that went away before the body)."""

    def __init__(self, stream: Any, done: Callable[[], None]):
        self._stream, self._done = stream, done

    def __iter__(self) -> Any:
        try:
            yield from self._stream
        finally:
            self._done()

    def __del__(self) -> None:
        self._done()


def deadline(cur: Any, seconds: float) -> threading.Timer | None:
    """Interrupt ``cur``'s query after ``seconds`` (None: no limit)."""
    if seconds <= 0:
        return None
    timer = threading.Timer(seconds, cur.interrupt)
    timer.daemon = True
    timer.start()
    return timer


def cache_control() -> str:
    """The data API answers' Cache-Control (see CACHE_SECONDS). ``public``
    with a lifetime: a shared cache may then keep an answer to a request
    with a key (``Vary: Authorization`` keeps each key's apart)."""
    n = CACHE_SECONDS
    return f"public, max-age={n:g}, stale-while-revalidate={n:g}" if n > 0 else "no-cache"


def etag(*parts: Any) -> str:
    """A weak ETag over ``parts``: weak, since two answers with the same tag
    hold the same rows, though not always in the same bytes (an answer
    without ``order by`` may come in another order)."""
    raw = json.dumps([__version__, *parts], default=str, sort_keys=True, separators=(",", ":"))
    return f'W/"{hashlib.blake2b(raw.encode(), digest_size=16).hexdigest()}"'


def matches(if_none_match: str | None, tag: str | None) -> bool:
    """Whether ``If-None-Match`` names ``tag`` (weak comparison)."""
    if not if_none_match or not tag:
        return False
    if if_none_match.strip() == "*":
        return True
    bare = tag.removeprefix("W/")
    return any(t.strip().removeprefix("W/") == bare for t in if_none_match.split(","))


def caching(tag: str | None) -> dict[str, str]:
    """The headers of a data answer (``tag`` None: one never cached)."""
    if tag is None:
        return {"Cache-Control": "no-store", "Vary": VARY}
    return {"ETag": tag, "Cache-Control": cache_control(), "Vary": VARY}


def not_modified(tag: str) -> Response:
    return Response(status_code=304, headers=caching(tag))


def volatile(sql: str) -> bool:
    """Whether ``sql`` may answer differently over the same data."""
    return VOLATILE.search(sql.lower()) is not None


def describe(ws: Workspace, pid: str) -> list[dict[str, Any]]:
    """A project's nodes and their current outputs (default parameters),
    for an app to find what to read: ``kind`` (``table``, ``multi``,
    ``json`` or null before the first run), ``rows``, ``columns`` and, for a
    node with several outputs, ``parts``."""
    try:
        resolved = ws.resolve_params(None)
    except ValueError:
        resolved = {}
    out = []
    for name, node in ws.graph.nodes.items():
        ls = ws.store.last_success(name, ws.node_params(name, resolved)[1])
        kind = ls["kind"] if ls else None
        item: dict[str, Any] = {
            "name": name,
            "description": node.description or None,
            "kind": kind,
            "rows": ls.get("rows") if ls else None,
            "columns": ls.get("schema") if kind == "table" and isinstance(ls.get("schema"), list) else None,
            "run_id": ls.get("id") if ls else None,
            "artifact_id": ls.get("artifact_id") if ls else None,
            "finished_at": ls.get("finished_at") if ls else None,
            "partitioned": bool(node.partition_by),
            "history": bool(ws.graph.effective_params(name)),
        }
        if kind == "multi":
            item["parts"] = output_parts(ls.get("schema"))  # type: ignore[union-attr]
        if ls and kind in ARTIFACT_EXTENSIONS and ls.get("artifact_id") and not ws.is_partitioned(ls):
            item["url"] = f"{PREFIX}projects/{pid}/nodes/{name}/artifacts/{ls['artifact_id']}.{ARTIFACT_EXTENSIONS[kind]}"
        out.append(item)
    return out


def install(app: FastAPI, hub: Any, role_on: Callable[[str], str | None], is_admin: Callable[[], bool]) -> None:
    """The data API's routes on a hub app. ``role_on`` is the request's role
    on a project, ``is_admin`` whether the request is an admin's or the
    operator's."""
    from .hub import PROJECT_ID

    store: DataKeyStore = hub.data_keys
    connections = QueryConnections()
    app.state.data_connections = connections
    slots = KeySlots()
    app.state.data_slots = slots
    results = ResultCache(int(RESULT_CACHE_MB * 2**20))
    app.state.data_results = results
    pool = ThreadPoolExecutor(DATA_WORKERS, thread_name_prefix="exuvia-data")

    def blocking(fn: Callable[..., Any], *args: Any) -> Awaitable[Any]:
        """``asyncio.to_thread`` on the data API's own threads."""
        ctx = contextvars.copy_context()
        return asyncio.get_running_loop().run_in_executor(pool, functools.partial(ctx.run, fn, *args))
    # project -> ((workspace, its version), built at, describe()): a listing
    # reads every node's last run, so it is kept until the project changes
    # (or CACHE_TTL passes: a default parameter like "today" moves on its own).
    described: dict[str, tuple[tuple[int, int], float, list[dict[str, Any]]]] = {}
    # project -> the lock held while its listing is built: requests that find
    # it stale together wait for one describe() instead of each running their
    # own (every listing expires CACHE_TTL after it was built, so 32 clients
    # listing 40 projects ran up to 32 x 40 of them, each through the
    # database connection, which a shared database has one of per process).
    describing: dict[str, threading.Lock] = {}
    describing_lock = threading.Lock()

    def nodes_of(pid: str, ws: Workspace) -> list[dict[str, Any]]:
        stamp = (id(ws), ws.bus.version)

        def fresh() -> list[dict[str, Any]] | None:
            hit = described.get(pid)
            return hit[2] if hit and hit[0] == stamp and time.monotonic() - hit[1] < CACHE_TTL else None

        out = fresh()
        if out is not None:
            return out
        with describing_lock:
            lock = describing.setdefault(pid, threading.Lock())
        with lock:
            out = fresh()  # built while this request waited
            if out is None:
                now = time.monotonic()
                out = describe(ws, pid)
                described[pid] = (stamp, now, out)
            return out
    access_cache: dict[str, tuple[tuple[str, ...], float]] = {}
    access_lock = threading.Lock()
    # (project, node, parameters) -> ((workspace, its version), looked up at,
    # what a node read's ETag is made of): kept like a listing, so a node
    # read's tag costs no lookup in the run records.
    output_runs: dict[tuple[str, str, str], tuple[tuple[int, int], float, Any]] = {}

    def output_run(pid: str, ws: Workspace, node: str, resolved: dict[str, Any]) -> Any:
        key = (pid, node, json.dumps(resolved, sort_keys=True, default=str))
        stamp = (id(ws), ws.bus.version)
        hit = output_runs.get(key)
        if hit and hit[0] == stamp and time.monotonic() - hit[1] < CACHE_TTL:
            return hit[2]
        now = time.monotonic()
        ls = ws.store.last_success(node, ws.node_params(node, resolved)[1])
        out = ls and [ls.get("id"), ls.get("artifact_id"), ls.get("version")]
        if len(output_runs) > 4096:
            output_runs.clear()
        output_runs[key] = (stamp, now, out)
        return out

    # -- managing keys (people, not keys) -----------------------------------------------
    def manager() -> str | None:
        """Who is managing data keys: a signed-in person's user id, or None
        for the operator. Agent tokens and share links can't: a leaked token
        must not mint keys."""
        ident = auth.current()
        if ident is None:
            return None
        if ident.method in ("token", "link") or not ident.uid:
            raise HTTPException(403, "data keys belong to a signed-in person: sign in to manage them")
        return ident.uid

    def check_projects(projects: list[str]) -> list[str]:
        try:
            projects = clean_projects(projects)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        for p in projects:
            if not PROJECT_ID.match(p) or not hub.exists(p):
                raise HTTPException(404, f"no project {p}")
            if not at_least(role_on(p), "viewer"):
                raise HTTPException(403, f"you can't open project {p}, so a key of yours can't read it")
        return projects

    def own_key(kid: str) -> dict[str, Any]:
        owner = manager()
        row = store.get(kid)
        if row is None or (row["owner_id"] != owner and not is_admin()):
            raise HTTPException(404, f"no data key {kid}")
        return row

    def keys_changed() -> None:
        with access_lock:
            access_cache.clear()

    @app.get("/api/data-keys")
    def data_keys_list(all: bool = False) -> dict[str, Any]:
        """Your data keys (``?all=true``: every key on the hub, for an admin),
        newest first, never the keys themselves."""
        owner = manager()
        if all and not is_admin():
            raise HTTPException(403, "admins only: every key on the hub")
        return {"keys": store.list(owner, everyone=all), "max_projects": MAX_PROJECTS}

    @app.get("/api/data-keys/usage")
    def data_keys_usage(days: int = Query(30, ge=1, le=90)) -> dict[str, Any]:
        """Every key on the hub with how much it was used, for the admin
        page: by day over the last ``days`` days, and the requests turned
        away for an unknown or revoked key. Admins only."""
        manager()
        if not is_admin():
            raise HTTPException(403, "admins only: every key on the hub")
        used = store.usage(days)
        span, by_key = used["days"], used["keys"]
        week = set(span[-7:])
        today = span[-1]

        def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "today": sum(r["requests"] for r in rows if r["day"] == today),
                "week": sum(r["requests"] for r in rows if r["day"] in week),
                "requests": sum(r["requests"] for r in rows),
                "queries": sum(r["queries"] for r in rows),
                "errors": sum(r["errors"] for r in rows),
            }

        def owner(uid: str | None) -> dict[str, Any] | None:
            if uid is None:
                return None
            user = hub.users.get(uid) if hub.users is not None else None
            return {"id": uid, "name": user.name if user else uid, "disabled": bool(user and user.disabled)}

        keys = []
        for k in store.list(everyone=True):
            rows = by_key.get(k["id"], [])
            keys.append({**k, "owner": owner(k["owner_id"]), "usage": totals(rows),
                         "daily": [{"day": r["day"], "requests": r["requests"]} for r in rows]})
        daily: dict[str, dict[str, int]] = {d: {"requests": 0, "queries": 0, "errors": 0} for d in span}
        for kid, rows in by_key.items():
            if kid == TURNED_AWAY:
                continue
            for r in rows:
                for f in ("requests", "queries", "errors"):
                    daily[r["day"]][f] += r[f]
        return {
            "days": days,
            "keys": keys,
            "daily": [{"day": d, **daily[d]} for d in span],
            "turned_away": totals(by_key.get(TURNED_AWAY, [])),
            "result_cache": results.stats(),
        }

    @app.post("/api/data-keys", status_code=201)
    def data_keys_create(body: DataKeyReq) -> dict[str, Any]:
        """A new read-only key for ``projects`` (each one you can open).
        ``key`` is in this answer only: it is not kept."""
        owner = manager()
        row, key = store.create(owner, body.name, check_projects(body.projects))
        return {**row, "key": key}

    @app.patch("/api/data-keys/{kid}")
    def data_keys_update(kid: str, body: DataKeyPatch) -> dict[str, Any]:
        """Rename a key, or replace the projects it reads."""
        own_key(kid)
        projects = check_projects(body.projects) if body.projects is not None else None
        row = store.update(kid, name=body.name, projects=projects)
        keys_changed()
        return row

    @app.delete("/api/data-keys/{kid}")
    def data_keys_revoke(kid: str) -> dict[str, Any]:
        own_key(kid)
        store.revoke(kid)
        keys_changed()
        return {"ok": True}

    # -- reading with a key ----------------------------------------------------------
    def readable(key: DataKey) -> tuple[str, ...]:
        """The key's projects its maker can still open (all of them for the
        operator's). Raises 403 when the maker is gone or disabled."""
        now = time.monotonic()
        with access_lock:
            hit = access_cache.get(key.id)
        if hit and now - hit[1] < ACCESS_TTL:
            return hit[0]
        ident: auth.Identity | None = None
        if key.owner_id:
            user = hub.users.get(key.owner_id) if hub.users is not None else None
            if hub.users is not None and (user is None or user.disabled):
                raise HTTPException(403, "the person who made this data key can no longer sign in: it reads nothing")
            ident = auth.Identity(
                user=user.name if user else None, email=user.email if user else None, method="data_key",
                uid=key.owner_id, role=user.role if user else None,
            )
        out = []
        for p in key.projects:
            if not hub.exists(p):
                continue
            role = hub.sharing.role(p, ident) if hub.sharing is not None else "owner"
            if at_least(role, "viewer"):
                out.append(p)
        with access_lock:
            access_cache[key.id] = (tuple(out), now)
        return tuple(out)

    async def count(kid: str, query: bool, ok: bool) -> None:
        """Count a request with a key (datakeys.py, usage), writing the
        counts when they are due."""
        if store.note(kid, query=query, ok=ok):
            try:
                await blocking(store.flush)
            except Exception as e:  # noqa: BLE001 - a count not written must not fail the request; it is kept for next time
                print(f"exuvia: could not write data key usage: {e}", file=sys.stderr)

    async def key_projects(
        request: Request,
        creds: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> AsyncIterator[Access]:
        token = creds.credentials.strip() if creds else ""
        if not looks_like_key(token):
            raise HTTPException(401, "send a data key: Authorization: Bearer exd_... (make one in the hub's account menu, Data API keys)")
        key = await blocking(store.by_key, token)
        query = request.url.path.endswith("/query")
        if key is None:
            await count(TURNED_AWAY, query, False)
            raise HTTPException(401, "unknown or revoked data key")
        # The request counts once it is answered: a failure in it (here or in the route) counts as an error.
        ok = False
        try:
            yield key, await blocking(readable, key)
            ok = True
        finally:
            await count(key.id, query, ok)

    async def workspace(project: str, projects: tuple[str, ...]) -> Workspace:
        if project not in projects:
            raise HTTPException(404, f"this key does not read a project {project}")
        return (await hub.get(project)).ws

    # The answers are written as JSON directly (FastJSON); `responses` only
    # documents them, so FastAPI neither validates nor re-encodes them.
    route = {"tags": ["data"], "response_model": None, "response_class": FastJSON}

    def revalidated(answer: FastJSON, if_none_match: str | None) -> Response:
        """A listing with an ETag over its bytes: 304 when the client has them."""
        tag = etag(hashlib.blake2b(answer.body, digest_size=16).hexdigest())
        if matches(if_none_match, tag):
            return not_modified(tag)
        answer.headers.update(caching(tag))
        return answer

    inm_doc = "An ETag of an earlier answer: 304 Not Modified when the answer would be the same."
    INM = Header(None, alias="If-None-Match", description=inm_doc)
    cached_doc = {
        "ETag": {"description": "Names what the answer was made from: send it back in `If-None-Match`.",
                 "schema": {"type": "string"}},
        "Cache-Control": {"description": "`no-cache` (revalidate each use), or `public, max-age=N, "
                          "stale-while-revalidate=N` when the hub sets EXUVIA_DATA_CACHE_SECONDS.",
                          "schema": {"type": "string"}},
        "Vary": {"description": "`Authorization, Accept`.", "schema": {"type": "string"}},
    }
    NOT_MODIFIED = {304: {"description": "Not Modified: the `If-None-Match` ETag is current.", "headers": cached_doc}}

    @app.get(PREFIX + "projects", responses={200: {"model": DataProjects, "headers": cached_doc}, **NOT_MODIFIED, **PROBLEMS}, **route)
    async def data_projects(if_none_match: str | None = INM, access: Access = Depends(key_projects)) -> Any:
        """The projects this key reads, each with its nodes and their current
        outputs: the place to start."""
        key, projects = access
        out = []
        for pid in projects:
            ws = (await hub.get(pid)).ws
            out.append({"id": pid, "name": ws.project.name, "nodes": await blocking(nodes_of, pid, ws)})
        return revalidated(FastJSON({"key": {"id": key.id, "name": key.name}, "projects": out}), if_none_match)

    @app.get(PREFIX + "projects/{project}", responses={200: {"model": DataProject, "headers": cached_doc}, **NOT_MODIFIED, **PROBLEMS}, **route)
    async def data_project(project: str, if_none_match: str | None = INM, access: Access = Depends(key_projects)) -> Any:
        """One of the key's projects, with its nodes."""
        ws = await workspace(project, access[1])
        answer = FastJSON({"id": project, "name": ws.project.name, "nodes": await blocking(nodes_of, project, ws)})
        return revalidated(answer, if_none_match)

    @app.get(
        PREFIX + "projects/{project}/nodes/{node}",
        responses={200: {"model": NodeOutput, "content": ARROW_DOC, "headers": cached_doc}, **NOT_MODIFIED, **PROBLEMS}, **route,
    )
    async def data_node(
        project: str,
        node: str,
        request: Request,
        limit: int | None = Query(None, ge=0, description="JSON: 200 rows unless given, 5000 at most. Arrow: every row unless given."),
        offset: int = Query(0, ge=0, description="Rows to skip first."),
        params: str | None = Query(None, description="Run parameters as a JSON object; default: the project's defaults."),
        part: str | None = Query(None, description="One output of a node with several."),
        partitions: str | None = Query(None, description="For a partitioned node: `lo..hi` (either end optional) or one value."),
        if_none_match: str | None = INM,
        access: Access = Depends(key_projects),
    ) -> Any:
        """A node's output: JSON rows, or with `Accept:
        application/vnd.apache.arrow.stream` an Arrow stream of every row.
        A JSON output is answered as JSON whatever `Accept` says."""
        projects = access[1]
        ws = await workspace(project, projects)
        if node not in ws.graph.nodes:
            raise HTTPException(404, f"no node {node} in project {project}")
        resolved = resolve_params(ws, query_params(params))
        rng = partition_range(partitions)
        need_partitioned(ws, node, rng, None)
        arrow = wants_arrow(request.headers.get("accept"))

        def read() -> Any:
            # The tag before the answer: a run finishing in between leaves a
            # newer answer under an older tag (asked again, it differs), never
            # an older answer under the newer tag.
            tag = etag("node", project, node, output_run(project, ws, node, resolved), limit, offset, resolved, part, rng, arrow)
            if matches(if_none_match, tag):
                return not_modified(tag)
            give = slots.take(access[0].id)
            try:
                if arrow:
                    stream = ws.preview_arrow(node, limit=limit, offset=offset, params=resolved, part=part, partitions=rng)
                    if stream is not None:
                        answer = arrow_response(Finally(stream, give), caching(tag))  # gives the slot back once read
                        give = None
                        return answer
                rows = 200 if limit is None else min(limit, JSON_MAX)
                try:
                    out = ws.preview(node, limit=rows, offset=offset, params=resolved, part=part, partitions=rng, raw=True)
                except KeyError as e:
                    raise HTTPException(404, str(e.args[0])) from e
                return FastJSON(out, headers=caching(tag))
            finally:
                if give is not None:
                    give()

        return await blocking(read)

    immutable = {"Cache-Control": "public, max-age=31536000, immutable", "Vary": "Authorization"}
    artifact_doc = {
        200: {"description": "The file, byte for byte. `Range` is honoured (part of a Parquet file).",
              "content": {t: {"schema": {"type": "string", "format": "binary"}} for t in ARTIFACT_MEDIA_TYPES.values()},
              "headers": {"ETag": {"description": "The artifact id.", "schema": {"type": "string"}},
                          "Cache-Control": {"description": "`public, max-age=31536000, immutable`", "schema": {"type": "string"}}}},
        **NOT_MODIFIED, **PROBLEMS,
    }

    def artifact_answer(found: tuple[Any, str] | None, kind: str | None, tag: str, if_none_match: str | None, what: str) -> Response:
        if found is None or found[1] != kind:
            raise HTTPException(404, f"no output {what}")
        headers = {**immutable, "ETag": tag}
        if matches(if_none_match, tag):
            return Response(status_code=304, headers=headers)
        return FileResponse(found[0], media_type=ARTIFACT_MEDIA_TYPES[kind], headers=headers)

    artifact_path = PREFIX + "projects/{project}/nodes/{node}/artifacts/{filename}"
    part_path = PREFIX + "projects/{project}/nodes/{node}/artifacts/{artifact_id}/{filename}"

    @app.get(artifact_path, responses=artifact_doc, tags=["data"], response_model=None)
    async def data_artifact(
        project: str, node: str, filename: str, if_none_match: str | None = INM, access: Access = Depends(key_projects),
    ) -> Any:
        """A node's stored output by its content hash (a node's `artifact_id`
        and `url` in the listing): `<artifact_id>.parquet`, `.json`, or
        `.zip` for a node with several outputs. The id names the content, so
        the answer never changes: caches keep it for good (`immutable`). Read
        the listing (revalidated) to learn the current id, then fetch this.
        Only an output the node in this project produced is served."""
        ws = await workspace(project, access[1])
        if node not in ws.graph.nodes:
            raise HTTPException(404, f"no node {node} in project {project}")
        artifact_id, _, ext = filename.rpartition(".")
        kind = ARTIFACT_KINDS.get(ext)
        found = await blocking(ws.artifact_file, node, artifact_id) if kind and artifact_id else None
        return artifact_answer(found, kind, f'"{artifact_id}"', if_none_match, f"{filename} of {node}")

    @app.get(part_path, responses=artifact_doc, tags=["data"], response_model=None)
    async def data_artifact_part(
        project: str, node: str, artifact_id: str, filename: str, if_none_match: str | None = INM,
        access: Access = Depends(key_projects),
    ) -> Any:
        """One output of a node with several, by the id of the whole
        (`<artifact_id>.zip`): `<part>.parquet` or `<part>.json`. Cached for
        good like the whole."""
        ws = await workspace(project, access[1])
        if node not in ws.graph.nodes:
            raise HTTPException(404, f"no node {node} in project {project}")
        part, _, ext = filename.rpartition(".")
        kind = ARTIFACT_KINDS.get(ext)
        found = await blocking(ws.artifact_file, node, artifact_id, part) if kind in ("table", "json") and part else None
        return artifact_answer(found, kind, f'"{artifact_id}/{part}"', if_none_match, f"{artifact_id}/{filename} of {node}")

    # HEAD too (a client checking a file's size before ranged reads), left out of the spec.
    app.head(artifact_path, include_in_schema=False, response_model=None)(data_artifact)
    app.head(part_path, include_in_schema=False, response_model=None)(data_artifact_part)

    async def answer_query(
        sql: str, limit: int | None, asked: list[str] | None, request: Request, access: Access,
        if_none_match: str | None,
    ) -> Any:
        projects = access[1]
        if asked is not None:
            missing = [p for p in asked if p not in projects]
            if missing:
                raise HTTPException(404, f"this key does not read {', '.join(missing)}")
            chosen = list(dict.fromkeys(asked))
        else:
            chosen = named_in(sql, projects)
        workspaces = {pid: (await hub.get(pid)).ws for pid in chosen}
        arrow = wants_arrow(request.headers.get("accept"))
        rows = limit if arrow else JSON_ROWS if limit is None else min(limit, JSON_MAX)
        cacheable = not volatile(sql)

        def tagged(digest: str) -> str | None:
            return etag("query", digest, sql, rows, chosen, arrow) if cacheable else None

        def execute() -> Any:
            import duckdb

            if if_none_match and cacheable:
                # A 304 from a kept connection takes no slot: it costs next to nothing.
                hit = connections.kept(workspaces)
                if hit is not None:
                    hit[0].close()
                    tag = tagged(hit[1])
                    if matches(if_none_match, tag):
                        return not_modified(tag)  # type: ignore[arg-type]
            give = slots.take(access[0].id)
            cur = None
            timer = None
            streaming = False
            try:
                cur, digest = connections.open(workspaces)
                tag = tagged(digest)
                if matches(if_none_match, tag):
                    return not_modified(tag)  # type: ignore[arg-type]
                read_only(cur)
                check_select(cur, sql)
                timer = deadline(cur, QUERY_TIMEOUT)
                if arrow:
                    stream = query_arrow(cur, sql, rows)  # closes the cursor when it has been read
                    streaming = True
                    return arrow_response(Finally(stream, give), caching(tag))  # gives the slot back once read
                return FastJSON({**query_json(cur, sql, rows, raw=True), "projects": chosen}, headers=caching(tag))
            except HTTPException:
                raise
            except duckdb.InterruptException as e:
                raise HTTPException(
                    400, f"the query ran longer than {QUERY_TIMEOUT:g} seconds, the data API's limit: "
                    "filter or aggregate more, or ask for fewer rows",
                ) from e
            except Exception as e:
                raise HTTPException(400, str(e) if type(e) is ValueError else f"{type(e).__name__}: {e}") from e
            finally:
                if timer is not None:
                    timer.cancel()  # Arrow: the first rows are in; the client's reading paces the rest
                if not streaming:
                    give()
                    if cur is not None:
                        cur.close()

        def from_memory(kept: tuple[bytes, str | None]) -> Response:
            body, tag = kept
            if matches(if_none_match, tag):
                return not_modified(tag)  # type: ignore[arg-type]
            return Response(body, media_type="application/json", headers=caching(tag))

        def run() -> Any:
            """``execute``, through the result cache for a JSON answer: a
            hit takes no slot, and a miss that finds the same query running
            waits for it."""
            if arrow or not cacheable or results.budget <= 0:
                return execute()
            key = (QueryConnections._stamp(workspaces), sql, rows, tuple(chosen))
            kept = results.get(key)
            if kept is not None:
                return from_memory(kept)
            flight, leads = results.flight(key)
            if not leads:
                flight.done.wait(QUERY_TIMEOUT if QUERY_TIMEOUT > 0 else None)
                return from_memory(flight.answer) if flight.answer is not None else execute()
            answer = None
            try:
                t = time.perf_counter()
                out = execute()
                if out.status_code == 200:
                    answer = (bytes(out.body), out.headers.get("etag"))
                    results.put(key, answer[0], answer[1], time.perf_counter() - t)
                return out
            finally:
                results.land(key, flight, answer)  # None (it failed, or was a 304): the others run it themselves

        return await blocking(run)

    query_doc = {200: {"model": QueryAnswer, "content": ARROW_DOC, "headers": cached_doc}, **PROBLEMS}

    @app.post(PREFIX + "query", responses=query_doc, **route)
    async def data_query(body: DataQueryReq, request: Request, access: Access = Depends(key_projects)) -> Any:
        """One DuckDB SELECT across the key's projects. Each project is a
        catalog: `"<project>".<node>`, `"<project>".<node>.<part>` for one
        output of a node with several, `"<project>".<node>__runs` for every
        parameter set it ran with. JSON: the first `limit` rows (500 unless
        given, 5000 at most). With `Accept:
        application/vnd.apache.arrow.stream`: every row unless `limit`.
        The answer's ETag is the one `GET /api/data/query` answers 304 to
        (caches keep GET answers, not POST ones)."""
        return await answer_query(body.sql, body.limit, body.projects, request, access, None)

    @app.get(PREFIX + "query", responses={**query_doc, **NOT_MODIFIED}, **route)
    async def data_query_get(
        request: Request,
        sql: str = Query(description='One SELECT (or EXPLAIN). Each project is a catalog: `"sales".orders`.'),
        limit: int | None = Query(None, ge=0, description="Rows to return. JSON: 500 unless given, 5000 at most. Arrow: every row unless given."),
        projects: list[str] | None = Query(
            None, description="The projects to attach (repeat it for several). Default: the key's projects whose id appears in `sql`."
        ),
        if_none_match: str | None = INM,
        access: Access = Depends(key_projects),
    ) -> Any:
        """`POST /api/data/query` as a GET, which browsers and proxies cache:
        send the answer's `ETag` back in `If-None-Match` for a 304 when
        nothing it read has changed (DuckDB does not run). The URL is
        limited in length (about 8 KB behind nginx) and the SQL lands in
        access logs: POST a query that is long or names what it shouldn't.
        A query using a function like `now()` or `random()` has no ETag."""
        return await answer_query(sql, limit, projects, request, access, if_none_match)

    spec_cache: dict[str, dict[str, Any]] = {}

    @app.get(PREFIX + "openapi.json", include_in_schema=False)
    def data_spec(request: Request) -> Any:
        """The data API's own OpenAPI spec: only the routes a data key can
        use. No key needed, so an agent can read it before it has one."""
        # Built once. Only `servers` differs by request (it is the address the
        # request came to, which the Host header names), so nothing is kept
        # per address: anyone can send any Host.
        spec = spec_cache.get("spec")
        if spec is None:
            routes = [r for r in app.routes if getattr(r, "path", "").startswith(PREFIX)]
            spec = get_openapi(title="exuvia data API", version=__version__, description=SPEC_DESCRIPTION, routes=routes)
            spec["security"] = [{"dataKey": []}]
            spec_cache["spec"] = spec
        return {**spec, "servers": [{"url": str(request.base_url).rstrip("/")}]}
