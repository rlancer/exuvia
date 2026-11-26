"""A prototype Arrow Flight SQL endpoint for exuvia's data API.

It serves the same thing as ``POST /api/data/query`` (exuvia/dataapi.py):
one read-only SELECT across the hub projects a data key reads, each project a
catalog, but over Flight SQL, so any Flight SQL client can connect with a
standard driver: ADBC (Python, Go, R, Java, C), the Flight SQL JDBC/ODBC
drivers, DBeaver, Superset, Tableau through JDBC.

It runs beside the real hub, in the same process: ``create_hub_app`` serves
HTTP on one port, and this server serves Flight on another, sharing the hub
(its data keys, sharing rules and loaded projects) and the same kind of
locked, cached DuckDB connection (``dataapi.QueryConnections``).

What it implements of Flight SQL, which is enough for ADBC's DB-API cursor,
its ``adbc_get_objects`` and DBeaver's schema browser:

* ``CommandStatementQuery``: a query. GetFlightInfo binds it (so a bad
  statement fails before any data moves) and returns its schema and a
  ticket; DoGet streams the batches.
* ``CommandGetCatalogs``, ``CommandGetDbSchemas``, ``CommandGetTables``,
  ``CommandGetTableTypes``: discovery, from DuckDB's information_schema over
  the key's projects.
* ``CommandGetSqlInfo``: the server's name and version, read-only.
* ``CreatePreparedStatement`` / ``CommandPreparedStatementQuery`` /
  ``ClosePreparedStatement``: ADBC's DB-API cursor prepares every query.
  Without parameters a prepared statement is the same stateless handle as a
  ticket.

Everything else (bound parameters, transactions, Substrait, DoPut) is
answered "not implemented". The protobuf messages are decoded by hand: they
are a handful of string, bool and repeated fields, and this keeps the
prototype free of generated code.

Auth: ``authorization: Bearer exd_...`` on every call, checked against the
hub's data keys and what the key's maker can still open, as the HTTP data API
does. A ticket carries the SQL and the projects it was planned with, and
DoGet checks the key again before running it.

Run it::

    uv run python docs/design/flight-sql/server.py <hub dir> --http 7420 --flight 7421
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from typing import Any

import pyarrow as pa
import pyarrow.flight as fl

from exuvia import __version__, auth
from exuvia.dataapi import RESERVED_CATALOGS, QueryConnections, check_select
from exuvia.datakeys import looks_like_key
from exuvia.sharing import at_least

SQL = "type.googleapis.com/arrow.flight.protocol.sql."
BATCH_ROWS = 64_000


# -- protobuf, by hand -----------------------------------------------------------------
def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = out = 0
    while True:
        b = buf[i]
        i += 1
        out |= (b & 0x7F) << shift
        if not b & 0x80:
            return out, i
        shift += 7


def decode(buf: bytes) -> dict[int, list[Any]]:
    """A protobuf message's fields: number -> values (ints, or bytes for
    length-delimited fields)."""
    fields: dict[int, list[Any]] = {}
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint(buf, i)
        elif wire == 2:
            n, i = _varint(buf, i)
            val, i = buf[i:i + n], i + n
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        fields.setdefault(num, []).append(val)
    return fields


def _enc_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field_bytes(num: int, data: bytes) -> bytes:
    return _enc_varint(num << 3 | 2) + _enc_varint(len(data)) + data


def pack_any(name: str, value: bytes) -> bytes:
    """A google.protobuf.Any holding the Flight SQL message ``name``."""
    return field_bytes(1, (SQL + name).encode()) + field_bytes(2, value)


def unpack_any(buf: bytes) -> tuple[str, dict[int, list[Any]]]:
    f = decode(buf)
    url = f.get(1, [b""])[0].decode()
    if not url.startswith(SQL):
        raise fl.FlightServerError(f"not a Flight SQL command: {url or 'no type'}")
    return url[len(SQL):], decode(f.get(2, [b""])[0])


def text(f: dict[int, list[Any]], num: int) -> str | None:
    return f[num][0].decode() if num in f else None


def like(pattern: str | None, value: str) -> bool:
    """Flight SQL filter patterns are SQL LIKE (% and _)."""
    if pattern is None:
        return True
    import re

    rx = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, value) is not None


# -- auth ----------------------------------------------------------------------------
class KeyMiddleware(fl.ServerMiddleware):
    def __init__(self, token: str):
        self.token = token


class KeyMiddlewareFactory(fl.ServerMiddlewareFactory):
    """Reads ``authorization: Bearer exd_...``; the call checks it (it needs the hub)."""

    def start_call(self, info: Any, headers: dict[str, list[str]]) -> KeyMiddleware:
        value = (headers.get("authorization") or [""])[0]
        token = value[7:].strip() if value[:7].lower() == "bearer " else ""
        if not looks_like_key(token):
            raise fl.FlightUnauthenticatedError("send a data key: authorization: Bearer exd_...")
        return KeyMiddleware(token)


# -- the server --------------------------------------------------------------------------
class DataFlightServer(fl.FlightServerBase):
    def __init__(self, location: str, hub: Any, loop: asyncio.AbstractEventLoop):
        super().__init__(location, middleware={"key": KeyMiddlewareFactory()})
        self.hub = hub
        self.loop = loop  # the HTTP server's loop, where workspaces live
        self.connections = QueryConnections()

    # Who is asking, and what they may read: dataapi.install's `readable`.
    def _projects(self, context: Any) -> tuple[str, ...]:
        token = context.get_middleware("key").token
        key = self.hub.data_keys.by_key(token)
        if key is None:
            raise fl.FlightUnauthenticatedError("unknown or revoked data key")
        ident = None
        if key.owner_id:
            users = self.hub.users
            user = users.get(key.owner_id) if users is not None else None
            if users is not None and (user is None or user.disabled):
                raise fl.FlightUnauthorizedError("the person who made this data key can no longer sign in")
            ident = auth.Identity(user=user.name if user else None, email=user.email if user else None,
                                  method="data_key", uid=key.owner_id, role=user.role if user else None)
        out = []
        for p in key.projects:
            if not self.hub.exists(p):
                continue
            role = self.hub.sharing.role(p, ident) if self.hub.sharing is not None else "owner"
            if at_least(role, "viewer"):
                out.append(p)
        return tuple(out)

    def _cursor(self, projects: list[str]) -> Any:
        workspaces = {
            pid: asyncio.run_coroutine_threadsafe(self.hub.get(pid), self.loop).result().ws for pid in projects
        }
        return self.connections.cursor(workspaces)

    def _chosen(self, context: Any, sql: str) -> list[str]:
        lowered = sql.lower()
        return [p for p in self._projects(context) if p.lower() in lowered and p.lower() not in RESERVED_CATALOGS]

    @staticmethod
    def _info(descriptor: Any, schema: pa.Schema, ticket: bytes) -> fl.FlightInfo:
        return fl.FlightInfo(schema, descriptor, [fl.FlightEndpoint(ticket, [])], -1, -1)

    # -- GetFlightInfo: plan ---------------------------------------------------------------
    def _plan(self, context: Any, sql: str) -> tuple[pa.Schema, bytes]:
        """Check and bind ``sql`` (a bad statement fails here, before any
        data moves): its result's schema, and a handle that names the SQL
        and the projects it reads. Handles are stateless: a prepared
        statement and a ticket are both just this handle."""
        projects = self._chosen(context, sql)
        cur = self._cursor(projects)
        try:
            check_select(cur, sql)
            rel = cur.sql(sql)
        except Exception as e:
            raise fl.FlightServerError(str(e) if type(e) is ValueError else f"{type(e).__name__}: {e}") from None
        # The schema without running the query: DuckDB answers LIMIT 0 without
        # reading anything (to_arrow_reader on `rel` itself would run it).
        schema = rel.limit(0).to_arrow_reader(1).schema if rel is not None else pa.schema([])
        return schema, json.dumps({"sql": sql, "projects": projects}).encode()

    def get_flight_info(self, context: Any, descriptor: Any) -> fl.FlightInfo:
        kind, f = unpack_any(descriptor.command)
        if kind in ("CommandStatementQuery", "CommandPreparedStatementQuery"):
            sql = text(f, 1) or "" if kind == "CommandStatementQuery" else json.loads(f[1][0])["sql"]
            schema, handle = self._plan(context, sql)
            return self._info(descriptor, schema, pack_any("TicketStatementQuery", field_bytes(1, handle)))
        if kind in META:
            return self._info(descriptor, META[kind], descriptor.command)
        raise fl.FlightServerError(f"not implemented in this prototype: {kind}")

    def get_schema(self, context: Any, descriptor: Any) -> fl.SchemaResult:
        return fl.SchemaResult(self.get_flight_info(context, descriptor).schema)

    # -- DoGet: run ----------------------------------------------------------------------
    def do_get(self, context: Any, ticket: Any) -> fl.FlightDataStream:
        kind, f = unpack_any(ticket.ticket)
        if kind == "TicketStatementQuery":
            plan = json.loads(f[1][0])
            allowed = set(self._projects(context))
            lost = [p for p in plan["projects"] if p not in allowed]
            if lost:
                raise fl.FlightUnauthorizedError(f"this key no longer reads {', '.join(lost)}")
            cur = self._cursor(plan["projects"])
            check_select(cur, plan["sql"])
            rel = cur.sql(plan["sql"])
            if rel is None:
                return fl.RecordBatchStream(pa.table({}))
            reader = rel.to_arrow_reader(BATCH_ROWS)

            def batches(cur: Any = cur, reader: Any = reader):  # holds the cursor until the stream ends
                yield from reader

            return fl.GeneratorStream(reader.schema, batches())
        if kind in META:
            return fl.RecordBatchStream(self._meta(context, kind, f))
        raise fl.FlightServerError(f"not implemented in this prototype: {kind}")

    # -- discovery ---------------------------------------------------------------------------
    def _tables(self, context: Any) -> list[tuple[str, str, str, str]]:
        """(catalog, schema, table, type) for every table the key can query."""
        projects = [p for p in self._projects(context) if p.lower() not in RESERVED_CATALOGS]
        if not projects:
            return []
        cur = self._cursor(projects)
        marks = ", ".join("?" * len(projects))
        return cur.execute(
            "select table_catalog, table_schema, table_name, table_type from information_schema.tables "
            f"where table_catalog in ({marks}) order by 1, 2, 3",
            projects,
        ).fetchall()

    def _meta(self, context: Any, kind: str, f: dict[int, list[Any]]) -> pa.Table:
        schema = META[kind]
        if kind == "CommandGetSqlInfo":
            return _sql_info()
        if kind == "CommandGetTableTypes":
            return pa.table({"table_type": ["VIEW"]}, schema=schema)
        rows = self._tables(context)
        if kind == "CommandGetCatalogs":
            cats = sorted({r[0] for r in rows})
            return pa.table({"catalog_name": cats}, schema=schema)
        catalog, schema_pat = text(f, 1), text(f, 2)
        rows = [r for r in rows if (catalog is None or r[0] == catalog) and like(schema_pat, r[1])]
        if kind == "CommandGetDbSchemas":
            pairs = sorted({(r[0], r[1]) for r in rows})
            return pa.table({"catalog_name": [p[0] for p in pairs], "db_schema_name": [p[1] for p in pairs]}, schema=schema)
        # CommandGetTables
        table_pat = text(f, 3)
        types = {t.decode() for t in f.get(4, [])}
        rows = [r for r in rows if like(table_pat, r[2]) and (not types or r[3] in types)]
        cols: dict[str, list[Any]] = {
            "catalog_name": [r[0] for r in rows], "db_schema_name": [r[1] for r in rows],
            "table_name": [r[2] for r in rows], "table_type": [r[3] for r in rows],
        }
        if f.get(5, [0])[0]:  # include_schema
            cur = self._cursor(sorted({r[0] for r in rows}))
            cols["table_schema"] = [
                cur.sql(f'select * from "{c}"."{s}"."{t}" limit 0').to_arrow_reader(1).schema.serialize().to_pybytes()
                for c, s, t, _ in rows
            ]
            return pa.table(cols, schema=TABLES_WITH_SCHEMA)
        return pa.table(cols, schema=schema)

    def list_actions(self, context: Any) -> list[tuple[str, str]]:
        return [("CreatePreparedStatement", "plan a query"), ("ClosePreparedStatement", "forget one (nothing to do)")]

    def do_action(self, context: Any, action: Any) -> Any:
        """Prepared statements, which ADBC's DB-API cursor always uses. With
        no parameters (exuvia's SQL has none) a prepared statement is the
        same stateless handle as a ticket, so closing one forgets nothing."""
        if action.type == "CreatePreparedStatement":
            _, f = unpack_any(action.body.to_pybytes())
            schema, handle = self._plan(context, text(f, 1) or "")
            result = field_bytes(1, handle) + field_bytes(2, schema.serialize().to_pybytes())
            return [fl.Result(pack_any("ActionCreatePreparedStatementResult", result))]
        if action.type == "ClosePreparedStatement":
            return []
        raise fl.FlightServerError(f"not implemented in this prototype: action {action.type}")


_text = pa.field
META: dict[str, pa.Schema] = {
    "CommandGetCatalogs": pa.schema([_text("catalog_name", pa.utf8(), False)]),
    "CommandGetDbSchemas": pa.schema([_text("catalog_name", pa.utf8()), _text("db_schema_name", pa.utf8(), False)]),
    "CommandGetTables": pa.schema([
        _text("catalog_name", pa.utf8()), _text("db_schema_name", pa.utf8()),
        _text("table_name", pa.utf8(), False), _text("table_type", pa.utf8(), False),
    ]),
    "CommandGetTableTypes": pa.schema([_text("table_type", pa.utf8(), False)]),
}
TABLES_WITH_SCHEMA = META["CommandGetTables"].append(pa.field("table_schema", pa.binary(), False))

# CommandGetSqlInfo's answer is a dense union (FlightSql.proto, "SqlInfo").
_INFO_VALUE = pa.dense_union([
    pa.field("string_value", pa.utf8()),
    pa.field("bool_value", pa.bool_()),
    pa.field("bigint_value", pa.int64()),
    pa.field("int32_bitmask", pa.int32()),
    pa.field("string_list", pa.list_(pa.utf8())),
    pa.field("int32_to_int32_list_map", pa.map_(pa.int32(), pa.list_(pa.int32()))),
])
META["CommandGetSqlInfo"] = pa.schema([pa.field("info_name", pa.uint32(), False), pa.field("value", _INFO_VALUE, False)])


def _sql_info() -> pa.Table:
    # FLIGHT_SQL_SERVER_NAME 0, _VERSION 1, _ARROW_VERSION 2, _READ_ONLY 3
    names = [0, 1, 2, 3]
    strings = pa.array(["exuvia", __version__, pa.__version__], pa.utf8())
    bools = pa.array([True], pa.bool_())
    empty = [pa.array([], t.type) for t in list(_INFO_VALUE)[2:]]
    value = pa.UnionArray.from_dense(
        pa.array([0, 0, 0, 1], pa.int8()), pa.array([0, 1, 2, 0], pa.int32()),
        [strings, bools, *empty], [f.name for f in _INFO_VALUE], [0, 1, 2, 3, 4, 5],
    )
    return pa.Table.from_arrays([pa.array(names, pa.uint32()), value], schema=META["CommandGetSqlInfo"])


# -- running it beside the hub -------------------------------------------------------------
def main() -> None:
    import uvicorn

    from exuvia.server import create_hub_app

    ap = argparse.ArgumentParser()
    ap.add_argument("hub")
    ap.add_argument("--http", type=int, default=7420)
    ap.add_argument("--flight", type=int, default=7421)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    app = create_hub_app(args.hub)
    http = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.http, log_level="warning"))

    async def run() -> None:
        serving = asyncio.create_task(http.serve())
        while not http.started:
            if serving.done():
                return await serving
            await asyncio.sleep(0.05)
        flight = DataFlightServer(f"grpc://{args.host}:{args.flight}", app.state.hub, asyncio.get_running_loop())
        threading.Thread(target=flight.serve, daemon=True).start()
        print(f"exuvia hub on http://{args.host}:{args.http}, Flight SQL on grpc://{args.host}:{args.flight}",
              file=sys.stderr, flush=True)
        try:
            await serving
        finally:
            await asyncio.to_thread(flight.shutdown)

    asyncio.run(run())


if __name__ == "__main__":
    main()
