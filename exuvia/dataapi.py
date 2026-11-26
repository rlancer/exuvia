"""The data API: read-only access to many hub projects' outputs at once.

For apps built on exuvia data that need more than its views give them. A
data key (datakeys.py) names the projects an app may read; with it the app
can

* ``GET /api/data/projects``: list those projects and their tables;
* ``GET /api/data/projects/{project}/nodes/{node}``: read one node's output;
* ``POST /api/data/query``: run one DuckDB ``SELECT`` across all of them,
  each project a catalog of its own (``"sales".orders``, ``"crm".accounts``),

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
sends can change a connection other requests share.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, auth
from .arrowstream import wants_arrow
from .http_util import FastJSON, arrow_response, need_partitioned, partition_range, query_params, resolve_params
from .datakeys import MAX_PROJECTS, DataKey, DataKeyStore, clean_projects, looks_like_key
from .sharing import at_least
from .outputs import lock_outputs, output_parts, outputs_connection, query_arrow, query_json
from .workspace import Workspace

PREFIX = "/api/data/"
CACHE_TTL = 5.0  # seconds a query connection is reused without a local change
CACHE_MAX = 32  # query connections kept (one per set of projects queried together)
ACCESS_TTL = 10.0  # seconds a key's readable projects are answered from memory
JSON_ROWS = 500  # rows a JSON answer has unless `limit` says otherwise
JSON_MAX = 5000
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
        # projects -> (stamp, built at, connection)
        self._cons: "OrderedDict[tuple[str, ...], tuple[Any, float, Any]]" = OrderedDict()

    @staticmethod
    def _stamp(workspaces: dict[str, Workspace]) -> tuple[Any, ...]:
        return tuple((pid, id(ws), ws.bus.version) for pid, ws in sorted(workspaces.items()))

    def cursor(self, workspaces: dict[str, Workspace]) -> Any:
        """A cursor on a connection with each project's outputs in a catalog
        named after it. Blocking (a build may download outputs)."""
        key = tuple(sorted(workspaces))
        stamp = self._stamp(workspaces)
        now = time.monotonic()
        with self._lock:
            hit = self._cons.get(key)
            if hit and hit[0] == stamp and now - hit[1] < CACHE_TTL:
                self._cons.move_to_end(key)
                return hit[2].cursor()
        con = outputs_connection()
        files: list[str] = []
        for pid in key:
            if pid.lower() in RESERVED_CATALOGS:
                raise ValueError(
                    f"project {pid} can't be queried with others: {pid} is the name of one of DuckDB's own "
                    f"catalogs. Read its nodes at {PREFIX}projects/{pid}/nodes/<node>"
                )
            files += workspaces[pid].add_output_views(con, None, catalog=pid)
        lock_outputs(con, files)
        with self._lock:
            self._cons[key] = (stamp, now, con)
            self._cons.move_to_end(key)
            while len(self._cons) > CACHE_MAX:
                self._cons.popitem(last=False)  # a cursor still reading keeps it alive
        return con.cursor()

    def clear(self) -> None:
        with self._lock:
            self._cons.clear()


def describe(ws: Workspace) -> list[dict[str, Any]]:
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
        out.append(item)
    return out


def install(app: FastAPI, hub: Any, role_on: Callable[[str], str | None], is_admin: Callable[[], bool]) -> None:
    """The data API's routes on a hub app. ``role_on`` is the request's role
    on a project, ``is_admin`` whether the request is an admin's or the
    operator's."""
    from .hub import PROJECT_ID

    store: DataKeyStore = hub.data_keys
    connections = QueryConnections()
    access_cache: dict[str, tuple[tuple[str, ...], float]] = {}
    access_lock = threading.Lock()

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

    async def key_projects(
        creds: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> Access:
        token = creds.credentials.strip() if creds else ""
        if not looks_like_key(token):
            raise HTTPException(401, "send a data key: Authorization: Bearer exd_... (make one in the hub's account menu, Data API keys)")
        key = await asyncio.to_thread(store.by_key, token)
        if key is None:
            raise HTTPException(401, "unknown or revoked data key")
        return key, await asyncio.to_thread(readable, key)

    async def workspace(project: str, projects: tuple[str, ...]) -> Workspace:
        if project not in projects:
            raise HTTPException(404, f"this key does not read a project {project}")
        return (await hub.get(project)).ws

    # The answers are written as JSON directly (FastJSON); `responses` only
    # documents them, so FastAPI neither validates nor re-encodes them.
    route = {"tags": ["data"], "response_model": None, "response_class": FastJSON}

    @app.get(PREFIX + "projects", responses={200: {"model": DataProjects}, **PROBLEMS}, **route)
    async def data_projects(access: Access = Depends(key_projects)) -> Any:
        """The projects this key reads, each with its nodes and their current
        outputs: the place to start."""
        key, projects = access
        out = []
        for pid in projects:
            ws = (await hub.get(pid)).ws
            out.append({"id": pid, "name": ws.project.name, "nodes": await asyncio.to_thread(describe, ws)})
        return FastJSON({"key": {"id": key.id, "name": key.name}, "projects": out})

    @app.get(PREFIX + "projects/{project}", responses={200: {"model": DataProject}, **PROBLEMS}, **route)
    async def data_project(project: str, access: Access = Depends(key_projects)) -> Any:
        """One of the key's projects, with its nodes."""
        ws = await workspace(project, access[1])
        return FastJSON({"id": project, "name": ws.project.name, "nodes": await asyncio.to_thread(describe, ws)})

    @app.get(
        PREFIX + "projects/{project}/nodes/{node}",
        responses={200: {"model": NodeOutput, "content": ARROW_DOC}, **PROBLEMS}, **route,
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

        def read() -> Any:
            if wants_arrow(request.headers.get("accept")):
                stream = ws.preview_arrow(node, limit=limit, offset=offset, params=resolved, part=part, partitions=rng)
                if stream is not None:
                    return arrow_response(stream)
            rows = 200 if limit is None else min(limit, JSON_MAX)
            try:
                return FastJSON(ws.preview(node, limit=rows, offset=offset, params=resolved, part=part, partitions=rng))
            except KeyError as e:
                raise HTTPException(404, str(e.args[0])) from e

        return await asyncio.to_thread(read)

    @app.post(PREFIX + "query", responses={200: {"model": QueryAnswer, "content": ARROW_DOC}, **PROBLEMS}, **route)
    async def data_query(body: DataQueryReq, request: Request, access: Access = Depends(key_projects)) -> Any:
        """One DuckDB SELECT across the key's projects. Each project is a
        catalog: `"<project>".<node>`, `"<project>".<node>.<part>` for one
        output of a node with several, `"<project>".<node>__runs` for every
        parameter set it ran with. JSON: the first `limit` rows (500 unless
        given, 5000 at most). With `Accept:
        application/vnd.apache.arrow.stream`: every row unless `limit`."""
        projects = access[1]
        if body.projects is not None:
            missing = [p for p in body.projects if p not in projects]
            if missing:
                raise HTTPException(404, f"this key does not read {', '.join(missing)}")
            chosen = list(dict.fromkeys(body.projects))
        else:
            chosen = named_in(body.sql, projects)
        workspaces = {pid: (await hub.get(pid)).ws for pid in chosen}

        def run() -> Any:
            cur = None
            streaming = False
            try:
                cur = read_only(connections.cursor(workspaces))
                check_select(cur, body.sql)
                if wants_arrow(request.headers.get("accept")):
                    stream = query_arrow(cur, body.sql, body.limit)  # closes the cursor when it has been read
                    streaming = True
                    return arrow_response(stream)
                rows = JSON_ROWS if body.limit is None else min(body.limit, JSON_MAX)
                return FastJSON({**query_json(cur, body.sql, rows), "projects": chosen})
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(400, str(e) if type(e) is ValueError else f"{type(e).__name__}: {e}") from e
            finally:
                if cur is not None and not streaming:
                    cur.close()

        return await asyncio.to_thread(run)

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
