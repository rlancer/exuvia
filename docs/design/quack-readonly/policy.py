"""Read-only tokens for a DuckLake catalog served over Quack.

Prototype for docs/design/incremental-nodes-and-ducklake.md. Install on the
serving connection *before* quack_serve:

    policy = ReadOnlyPolicy({writer_token: "writer", reader_token: "reader"})
    policy.install(server_con)
    server_con.execute("SELECT * FROM quack_serve(...)")
    lock_down(server_con)

Authentication maps the presented token to a role and binds it to the
server-issued connection id. Authorization lets writer statements through
and checks each reader statement's parse tree against an allowlist of what
a DuckLake 1.1 reader sends. The parse tree comes from json_serialize_sql,
run in a private in-memory instance of the same DuckDB build. Anything the
checker does not recognise is denied, so a DuckLake upgrade that sends new
SQL fails closed.
"""
import json
import re
import threading
import time
from collections import OrderedDict

import duckdb

# --- What a DuckLake 1.1 reader sends (logged from DuckDB 2.0.0, ducklake 2.0.0 wheel) ---
QUERY_NODES = {"SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE"}
TABLE_REFS = {"BASE_TABLE", "EMPTY", "JOIN", "SUBQUERY", "TABLE_FUNCTION"}
EXPRESSION_CLASSES = {
    "BETWEEN", "CASE", "CAST", "COLUMN_REF", "COMPARISON", "CONJUNCTION",
    "CONSTANT", "FUNCTION", "OPERATOR", "STAR", "SUBQUERY", "TYPE",
}
# other dicts carrying a "type": modifiers and order-by entries
AUX_TYPES = {
    "ORDER_MODIFIER", "LIMIT_MODIFIER", "DISTINCT_MODIFIER",
    "ASCENDING", "DESCENDING", "ORDER_DEFAULT",
}
SCALAR_FUNCTIONS = {
    "-", "count_star", "current_database", "length", "list", "list_append",
    "list_value", "max", "min", "struct_pack", "sum", "array_agg",
}
TABLE_FUNCTIONS = {"duckdb_functions", "duckdb_schemas", "duckdb_tables", "duckdb_views"}
METADATA_TABLE = re.compile(r"ducklake_[a-z0-9_]+")
SAFE_CTE_NAME = re.compile(r"[a-z][a-z0-9_]*")
RESERVED_PREFIXES = ("ducklake_", "duckdb_", "pragma_", "sqlite_", "pg_")


class Denied(Exception):
    pass


MAX_READER_SQL = 64 * 1024  # DuckLake reader statements seen: median 245 bytes, max 3.5 KB


class ReadOnlyPolicy:
    def __init__(self, tokens, cache_size=4096):
        self.tokens = dict(tokens)          # token -> "writer" | "reader"
        self.roles = {}                     # connection_id -> role
        self.bound_at = {}                  # connection_id -> time.monotonic() at authentication
        self.lock = threading.Lock()
        self.reserved_names = set()         # every table/view name the server could resolve
        self.cache = OrderedDict()          # sql -> None (allowed) | reason (denied)
        self.cache_size = cache_size
        self.denials = []                   # (connection_id, reason, sql) for auditing
        # Parsing happens in a private in-memory instance of the same DuckDB build: the core
        # parser is identical to the serving one. (A SQL macro parsing in the serving DB was
        # tried first: DuckDB evaluated json_serialize_sql even for writers and cached
        # statements, because CASE didn't skip the ELSE branch.)
        self._parser = duckdb.connect()
        self._local = threading.local()

    # ---- installation ----
    def install(self, con, prefix="exuvia_policy"):
        rows = con.execute(
            "SELECT view_name FROM duckdb_views() UNION SELECT table_name FROM duckdb_tables()"
        ).fetchall()
        self.reserved_names = {r[0].lower() for r in rows}
        con.create_function(f"{prefix}_authn", self._authn, ["VARCHAR", "VARCHAR", "VARCHAR"], "BOOLEAN",
                            side_effects=True)
        con.create_function(f"{prefix}_authz", self._authz, ["VARCHAR", "VARCHAR"], "BOOLEAN",
                            side_effects=True)
        # Qualified: an unqualified name resolves through the search path, so a client that can
        # CREATE MACRO in the lake's main schema could shadow the hook (tested: the policy was
        # bypassed). Python UDFs live in system.main, which no client can write.
        con.execute(f"SET GLOBAL quack_authentication_function = 'system.main.{prefix}_authn'")
        con.execute(f"SET GLOBAL quack_authorization_function = 'system.main.{prefix}_authz'")

    # ---- hooks ----
    def _authn(self, connection_id, presented, _server_token):
        role = self.tokens.get(presented)
        if role is None:
            return False
        with self.lock:
            if connection_id in self.roles:     # server ids are random; a repeat is a bug
                return False
            self.roles[connection_id] = role
            self.bound_at[connection_id] = time.monotonic()
        return True

    def _authz(self, connection_id, sql):
        role = self.roles.get(connection_id)
        if role == "writer":
            return True
        if role != "reader":
            with self.lock:
                self.denials.append((connection_id, "unknown connection", sql))
            raise Denied("unknown connection")
        reason = self.verdict(sql)
        if reason is not None:
            with self.lock:
                self.denials.append((connection_id, reason, sql))
            raise Denied(f"read-only token: {reason}")
        return True

    def forget(self, live_connection_ids, grace_seconds=30.0):
        """Drop roles of connections that are gone (feed it quack_active_connections()).

        A connection authenticates before it shows up in a snapshot the caller took
        earlier, so only connections bound more than `grace_seconds` ago are dropped.
        (Without the grace period the concurrency test lost a reader's role mid-attach.)
        """
        cutoff = time.monotonic() - grace_seconds
        with self.lock:
            for cid in list(self.roles):
                if cid not in live_connection_ids and self.bound_at.get(cid, 0) < cutoff:
                    del self.roles[cid]
                    self.bound_at.pop(cid, None)

    # ---- the checker ----
    def parse(self, sql):
        cur = getattr(self._local, "cur", None)
        if cur is None:
            cur = self._local.cur = self._parser.cursor()
        return json.loads(cur.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])

    def verdict(self, sql):
        """None if a reader may run `sql`, else the reason it is denied."""
        with self.lock:
            if sql in self.cache:
                self.cache.move_to_end(sql)
                return self.cache[sql]
        if len(sql) > MAX_READER_SQL:
            return "statement too long"
        try:
            reason = check_tree(self.parse(sql), self.reserved_names)
        except Denied as e:
            reason = str(e)
        except Exception as e:  # unparseable or unexpected tree: fail closed
            reason = f"checker error: {type(e).__name__}"
        with self.lock:
            self.cache[sql] = reason
            if len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
        return reason


def check_tree(tree, reserved_names):
    """Return None if every statement is an allowed read, else raise Denied."""
    if tree.get("error"):
        raise Denied("not a SELECT")  # json_serialize_sql only serializes SELECT statements
    statements = tree["statements"]
    if len(statements) != 1:
        raise Denied("one statement per request")
    ctes = set()
    _collect_ctes(statements, ctes)
    for name in ctes:
        if not SAFE_CTE_NAME.fullmatch(name) or name.startswith(RESERVED_PREFIXES) or name in reserved_names:
            raise Denied(f"CTE name {name!r}")
    _walk(statements[0]["node"], ctes)
    return None


def _collect_ctes(node, out):
    if isinstance(node, dict):
        if "cte_map" in node:
            for entry in node["cte_map"]["map"]:
                out.add(entry["key"].lower())
        if node.get("type") == "RECURSIVE_CTE_NODE":
            out.add(node["cte_name"].lower())
        for v in node.values():
            _collect_ctes(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_ctes(v, out)


def _walk(node, ctes):
    if isinstance(node, list):
        for v in node:
            _walk(v, ctes)
        return
    if not isinstance(node, dict):
        return
    if "class" in node:
        cls = node["class"]
        if cls not in EXPRESSION_CLASSES:
            raise Denied(f"expression {cls}")
        if cls == "FUNCTION":
            _check_function(node, SCALAR_FUNCTIONS, "function")
    elif "type" in node and isinstance(node["type"], str):
        t = node["type"]
        if t.endswith("_NODE"):
            if t not in QUERY_NODES:
                raise Denied(f"query node {t}")
        elif t in TABLE_REFS:
            if t == "BASE_TABLE":
                _check_table(node, ctes)
            elif t == "TABLE_FUNCTION":
                fn = node["function"]
                if fn.get("class") != "FUNCTION":
                    raise Denied("table function form")
                _check_function(fn, TABLE_FUNCTIONS, "table function")
                if fn["arguments"]:
                    raise Denied("table function arguments")
                # the call itself is checked; walk the rest of the ref, not the call again
                for k, v in node.items():
                    if k != "function":
                        _walk(v, ctes)
                return
        elif t not in AUX_TYPES:
            raise Denied(f"node type {t}")
    for v in node.values():
        _walk(v, ctes)


def _check_function(fn, allowed, what):
    name = fn["function_name"]
    if fn.get("catalog") or fn.get("schema"):
        raise Denied(f"qualified {what} {name}")
    path = fn.get("qualified_name", {}).get("path", [name])
    if path != [name]:
        raise Denied(f"qualified {what} {name}")
    if name.lower() not in allowed:
        raise Denied(f"{what} {name}")


def _check_table(ref, ctes):
    catalog, schema, name = ref["catalog_name"], ref["schema_name"], ref["table_name"].lower()
    if catalog:
        raise Denied(f"catalog {catalog}")
    if schema == "" and name in ctes:
        return
    if schema in ("", "main") and METADATA_TABLE.fullmatch(name):
        return
    if schema == "information_schema" and name == "tables":
        return
    raise Denied(f"table {schema + '.' if schema else ''}{name}")


def lock_down(con):
    """Second layer, after quack_serve: no file or network access, no more SET."""
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")


def self_test(client_con, uri, reader_token):
    """Fail closed if the hook isn't enforcing. Run after quack_serve, from a separate instance.

    Quack's docs say Python UDFs are connection-scoped and cannot be callbacks; on DuckDB 2.0.0
    they are called and enforced. This turns that undocumented behaviour into a startup check.
    """
    q = "SELECT * FROM quack_query(?, ?, token := ?)"
    try:
        client_con.execute(q, [uri, "SELECT * FROM read_text('self-test')", reader_token]).fetchall()
    except Exception as e:
        if "read-only token" not in str(e):
            raise RuntimeError(f"read-only policy is not enforcing: {e}") from None
    else:
        raise RuntimeError("read-only policy is not enforcing: a forbidden reader statement ran")
    client_con.execute(q, [uri, "SELECT count_star() FROM information_schema.tables", reader_token]).fetchall()
