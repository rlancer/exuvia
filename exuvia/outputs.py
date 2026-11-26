"""SQL over a project's node outputs: the DuckDB sandbox the workbench's
``/api/query``, a view's queries and the hub's data API run in.

``outputs_db`` builds a connection with every table output as a view and
locks it (``lock_outputs``) so the SQL reads those files and nothing else.
``query_json`` and ``query_arrow`` run one statement on such a connection.
Workspace keeps thin wrappers (``query``, ``query_arrow``, ``_outputs_db``,
``add_output_views``).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .arrowstream import ArrowStream
from .runner import PART_EXT

if TYPE_CHECKING:
    from .workspace import Workspace

HISTORY_VIEW_LIMIT = 400  # parameter sets unioned into a `<node>__runs` view


def outputs_db(ws: Workspace, params: dict[str, Any] | None, only: list[str] | None = None) -> Any:
    """A DuckDB connection with every table output as a view.

    ``<node>`` is the node's current table for ``params`` (the defaults
    when omitted). ``<node>__runs`` unions its output across every
    parameter set it has run with, adding one ``_<param>`` column per
    parameter — the view to compare runs over time. A node with several
    outputs is a schema instead: ``<node>.<part>`` for each table part,
    and ``<node>.<part>__runs``. Each of those is also a view named
    ``"<node>.<part>"`` (one quoted name, as views and DuckDB's own
    "Did you mean" hint write it), so that form works too.

    The connection runs in the server's process, so it is a sandbox: it
    can read the files behind its views and nothing else, and it is
    locked so the SQL cannot reach any other file (the server's
    environment in ``/proc/self/environ``, another project, exuvia's
    database), the network or an extension (``read_csv``, ``COPY ... TO``,
    ``ATTACH`` of a file, ``INSTALL`` all fail) nor undo the lock. With
    ``only`` it has just those nodes' views (a view's ``reads``), and
    ``read_parquet`` of another node's output fails too.
    """
    con = outputs_connection()
    files = add_output_views(ws, con, params, only=only)
    lock_outputs(con, files)
    return con


def add_output_views(
    ws: Workspace, con: Any, params: dict[str, Any] | None, only: list[str] | None = None, catalog: str | None = None
) -> list[str]:
    """Create ``outputs_db``'s views on ``con``, in the attached
    in-memory database ``catalog`` when given (the data API puts each
    project in a catalog named after it: ``"<project>".<node>``).
    Returns the files the views read, for ``lock_outputs``. Blocking:
    it may download outputs from the remote artifact store."""
    import duckdb

    resolved = ws.resolve_params(params)
    names = [n for n in ws.graph.nodes if only is None or n in only]
    # ((schema or None, table), select, a select to try when the first one fails)
    views: list[tuple[tuple[str | None, str], str, str | None]] = []
    # ("<node>.<part>" as one table name, the (schema, table) it shows)
    aliases: list[tuple[str, tuple[str | None, str]]] = []
    files: list[str] = []

    def read(path: str | None, cols: str = "") -> str:
        files.append(path or "")
        return f"select {cols}* from read_parquet({_lit(path)})"

    def read_all(paths: list[str]) -> str:
        files.extend(paths)
        return f"select * from read_parquet([{', '.join(_lit(p) for p in paths)}], union_by_name = true)"

    def table_parts(run: dict[str, Any]) -> list[str]:
        return [p["name"] for p in output_parts(run.get("schema")) if p["kind"] == "table"]

    def part_path(run: dict[str, Any], part: str) -> str | None:
        found = ws.part_file(run, part)
        return str(found[0]) if found else None

    schemas: set[str] = set()
    for name in names:
        ls = ws.store.last_success(name, ws.node_params(name, resolved)[1])
        if ws.is_partitioned(ls):
            # Exactly the manifest's files: no chunk of another version is readable.
            paths = ws.table_files(ls)  # type: ignore[arg-type]
            if paths:
                views.append(((None, name), read_all(paths), None))
        elif ls and ls["kind"] == "table":
            views.append(((None, name), read(ws.artifact_path(ls)), None))
        elif ls and ls["kind"] == "multi":
            # Several outputs: each table is <node>.<part>, in a schema named after the node.
            schemas.add(name)
            for part in table_parts(ls):
                views.append(((name, part), read(part_path(ls, part)), None))
                aliases.append((f"{name}.{part}", (name, part)))
        if not ws.graph.effective_params(name):
            continue
        history = ws.store.history(name, HISTORY_VIEW_LIMIT)
        outputs: dict[tuple[str | None, str], list[tuple[str, dict[str, Any]]]] = {}  # view -> [(file, run params)]
        for run in history:
            params = run.get("params") or {}
            if run["kind"] == "table":
                for path in ws.table_files(run):
                    outputs.setdefault((None, f"{name}__runs"), []).append((path, params))
            elif run["kind"] == "multi":
                schemas.add(name)
                for part in table_parts(run):
                    path = part_path(run, part)
                    if path:
                        outputs.setdefault((name, f"{part}__runs"), []).append((path, params))
                        aliases.append((f"{name}.{part}__runs", (name, f"{part}__runs")))
        for view, runs in outputs.items():
            files.extend(path for path, _ in runs)
            views.append((view, _history_select(runs), _history_union(runs)))
    # The views read their files only once external access is off and
    # the files are allowed (lock_outputs), but creating a view over a
    # file reads its schema, so the files must be allowed first.
    con.execute("set allowed_paths = ?", [sorted({*_allowed_paths(con), *(f for f in files if f)})])
    db = f"{_ident(catalog)}." if catalog else ""
    if catalog:
        con.execute(f"attach ':memory:' as {_ident(catalog)}")

    def qualified(view: tuple[str | None, str]) -> str:
        schema, table = view
        if schema is None:
            return f"{db}main.{_ident(table)}" if catalog else _ident(table)
        return f"{db}{_ident(schema)}.{_ident(table)}"

    for schema in sorted(schemas):
        con.execute(f"create schema if not exists {db}{_ident(schema)}")
    made: set[tuple[str | None, str]] = set()
    for view, select, fallback in views:
        try:
            con.execute(f"create view {qualified(view)} as {select}")
        except duckdb.Error:
            if fallback is None:
                raise
            try:
                con.execute(f"create view {qualified(view)} as {fallback}")
            except duckdb.Error:
                continue  # incompatible schemas across runs: leave the history view out
        made.add(view)
    for alias, view in dict(aliases).items():
        if view in made:
            con.execute(f"create view {qualified((None, alias))} as select * from {qualified(view)}")
    return [f for f in files if f]


def query_json(con: Any, sql: str, limit: int, offset: int = 0) -> dict[str, Any]:
    """``limit`` rows of ``sql`` on ``con`` from row ``offset`` on, as JSON-ready columns and rows."""
    rel = _sql(con, sql)
    if rel is None:
        return {"columns": [], "rows": []}
    rows = rel.limit(limit, offset).fetchall()
    return {
        "columns": [{"name": c, "type": str(t)} for c, t in zip(rel.columns, rel.types, strict=True)],
        "rows": jsonable([list(r) for r in rows]),
    }


def query_arrow(con: Any, sql: str, limit: int | None, offset: int = 0) -> ArrowStream:
    """``sql`` on ``con`` as an Arrow stream: every row unless ``limit``, from row ``offset`` on."""
    rel = _sql(con, sql)
    if rel is not None and (limit is not None or offset):
        rel = rel.limit(_ALL_ROWS if limit is None else int(limit), int(offset))
    return ArrowStream(rel, owner=con)


_ALL_ROWS = 2**62  # the limit of an offset-only read
_MISSING_TABLE = re.compile(r'Table with name "?([^"\s!]+)"? does not exist')


def _sql(con: Any, sql: str) -> Any:
    """``con.sql(sql)`` over an outputs database, where a missing table that
    is a node with several outputs, or one of those outputs named without its
    node, gets an error naming the tables to query instead (DuckDB's own
    hint can point at a name that is not there)."""
    import duckdb

    try:
        return con.sql(sql)
    except duckdb.CatalogException as e:
        m = _MISSING_TABLE.search(str(e))
        if not m:
            raise
        missing = m.group(1)
        rows = con.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema <> 'main' and not ends_with(table_name, '__runs') order by table_schema, table_name"
        ).fetchall()
        parts: dict[str, list[str]] = {}
        for schema, table in rows:
            parts.setdefault(schema, []).append(table)
        if missing in parts:
            tables = ", ".join(f"{missing}.{t}" for t in parts[missing])
            raise duckdb.CatalogException(
                f"{missing} has several outputs, each a table of its own: query {tables}"
            ) from None
        owners = [s for s, tables in parts.items() if missing in tables]
        if owners:
            named = " or ".join(f"{s}.{missing}" for s in owners)
            raise duckdb.CatalogException(
                f"{missing} is an output of a node with several: query it as {named}"
            ) from None
        raise


def output_parts(schema: Any) -> list[dict[str, Any]]:
    """``[{name, kind, rows}]`` for the parts of an output with several,
    from the schema its run recorded (see ``runner.save_artifact``)."""
    if not isinstance(schema, list):
        return []
    return [
        {"name": p["name"], "kind": p.get("type"), "rows": p.get("rows")}
        for p in schema
        if isinstance(p, dict) and isinstance(p.get("name"), str) and p.get("type") in PART_EXT
    ]


# The column naming the file each row came from, in a `<node>__runs` view's
# read (dropped from what the view shows).
_RUN_FILE = "__exuvia_file"


def _history_select(runs: list[tuple[str, dict[str, Any]]]) -> str:
    """A `<node>__runs` view: one ``read_parquet`` over every run's file,
    joined to each run's parameters (one ``_<param>`` column each) by file.
    Two runs with the same output share a file, read once and joined to
    both. About twice as fast as a union of one read per run."""
    names = sorted({k for _, params in runs for k in params})
    files = ", ".join(_lit(f) for f in dict.fromkeys(path for path, _ in runs))
    rows = ", ".join(
        "(" + ", ".join([_lit(path), *(_lit(params.get(k)) for k in names)]) + ")" for path, params in runs
    )
    cols = ", ".join(f'"_{k}"' for k in names)
    return (
        f'select r.* exclude ("{_RUN_FILE}"), f.* exclude ("{_RUN_FILE}") '
        f"from read_parquet([{files}], union_by_name = true, filename = '{_RUN_FILE}') f "
        f'join (values {rows}) r("{_RUN_FILE}"{", " + cols if cols else ""}) using ("{_RUN_FILE}")'
    )


def _history_union(runs: list[tuple[str, dict[str, Any]]]) -> str:
    """The same view as a union of one read per run: slower, but it holds
    where the single read fails (types across runs that don't combine)."""
    selects = []
    for path, params in runs:
        cols = ", ".join(f'{_lit(v)} as "_{k}"' for k, v in sorted(params.items()))
        selects.append(f"select {cols + ', ' if cols else ''}* from read_parquet({_lit(path)})")
    return " union all by name ".join(selects)


def outputs_connection() -> Any:
    """A DuckDB connection for SQL over outputs: no extension installs or
    loads itself (see ``outputs_db``)."""
    import duckdb

    return duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})


def lock_outputs(con: Any, files: list[str]) -> None:
    """Lock an outputs connection: only ``files`` (the files behind its
    views) stay readable, nothing reaches the network or another file, and
    no setting can change again."""
    con.execute("set allowed_paths = ?", [sorted({f for f in files if f})])
    con.execute("set enable_external_access = false")
    con.execute("set lock_configuration = true")


def _allowed_paths(con: Any) -> list[str]:
    row = con.execute("select current_setting('allowed_paths')").fetchone()
    value = row[0] if row else None
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: Any) -> str:
    """A SQL literal (DDL cannot take prepared parameters)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def jsonable(rows: list[list[Any]]) -> list[list[Any]]:
    import math
    from datetime import date
    from decimal import Decimal

    def conv(v: Any) -> Any:
        if v is None or isinstance(v, (str, bool, int)):
            return v
        if isinstance(v, float):
            return None if math.isnan(v) or math.isinf(v) else v
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, (datetime, date)):
            return v.isoformat()
        if isinstance(v, (list, tuple)):
            return [conv(x) for x in v]
        if isinstance(v, dict):
            return {str(k): conv(x) for k, x in v.items()}
        return str(v)

    return [[conv(v) for v in r] for r in rows]
