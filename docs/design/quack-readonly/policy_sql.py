"""The read-only policy as SQL macros: no Python in the hook path.

Same rules as policy.py, written against json_serialize_sql output with JSON
paths. Quack's docs list SQL macros as a supported callback, so this version
needs no undocumented behaviour. Two things change because a macro can't keep
state:

- Authentication can't bind a token to the connection id (Quack calls the
  authentication function before the connection exists). Either token gets a
  client in; a connection is a writer only if its client id hashes to one
  registered at startup. The writer's client id becomes the write credential,
  as secret as a token. Quack only keeps an HMAC of it, keyed per server, so
  the control plane learns its own hash once after quack_serve
  (register_writer) and every writer reconnect matches it.
- A denial is just `false`: Quack answers "Authorization failed" with no
  reason, and nothing records which rule fired.

    install(server_con, reader_tokens=[READER_TOKEN])
    server_con.execute("SELECT * FROM quack_serve(uri, token := WRITER_TOKEN)")
    register_writer(server_con, uri, WRITER_TOKEN, WRITER_CLIENT_ID)
    lock_down(server_con)
"""
import re

from policy import EXPRESSION_CLASSES, MAX_READER_SQL, RESERVED_PREFIXES, SCALAR_FUNCTIONS, TABLE_FUNCTIONS

CATALOG = "exuvia_policy"  # an attached in-memory database: readers can't name other catalogs

# Every "type" value anywhere in the tree: query nodes, table refs, modifiers and expression
# types. Logged from the DuckLake 1.1 reader corpus (policy.py checks query nodes and table refs
# structurally and expressions by class; a JSON path can't tell those apart, so it lists them all).
NODE_TYPES = {
    "SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE",
    "BASE_TABLE", "EMPTY", "JOIN", "SUBQUERY", "TABLE_FUNCTION",
    "ORDER_MODIFIER", "LIMIT_MODIFIER", "DISTINCT_MODIFIER", "ASCENDING", "DESCENDING", "ORDER_DEFAULT",
    "CASE_EXPR", "COLUMN_REF", "COMPARE_BETWEEN", "COMPARE_EQUAL", "COMPARE_GREATERTHAN",
    "COMPARE_GREATERTHANOREQUALTO", "COMPARE_IN", "COMPARE_LESSTHAN", "COMPARE_LESSTHANOREQUALTO",
    "CONJUNCTION_AND", "CONJUNCTION_OR", "FUNCTION", "OPERATOR_CAST", "OPERATOR_COALESCE",
    "OPERATOR_IS_NOT_NULL", "OPERATOR_IS_NULL", "OPERATOR_NOT", "STAR", "TYPE", "VALUE_CONSTANT",
}


def _list(values):
    return "[" + ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(values)) + "]"


def install(con, reader_tokens, prefix=CATALOG):
    """Create the policy catalog and macros and point Quack's hooks at them. Before quack_serve."""
    reserved = {r[0].lower() for r in con.execute(
        "SELECT view_name FROM duckdb_views() UNION SELECT table_name FROM duckdb_tables()").fetchall()}
    p = f"{prefix}.main"
    con.execute(f"ATTACH ':memory:' AS {prefix}")
    # hashes, not tokens: readers may list macro definitions (duckdb_functions() is on the allowlist)
    con.execute(f"CREATE TABLE {p}.reader_tokens (h VARCHAR)")
    con.execute(f"CREATE TABLE {p}.writer_clients (h VARCHAR)")
    for t in reader_tokens:
        con.execute(f"INSERT INTO {p}.reader_tokens VALUES (sha256(?))", [t])
    # a table, not a list literal: DuckDB binds every constant of a literal on every call
    # (a 150-item literal cost ~5-7 ms), and Quack prepares the hook afresh for each statement
    con.execute(f"CREATE TABLE {p}.reserved_names AS SELECT unnest(?::VARCHAR[]) AS name", [sorted(reserved)])

    prefixes = "^(" + "|".join(RESERVED_PREFIXES) + ")"
    # What the rules read from the tree, computed once in a subquery. (Tried and slower: the same
    # rules as one flat expression, ~40-90 ms, and as regexes over the JSON text, ~25 ms, because
    # DuckDB compiles each constant pattern per call.)
    parts = {
        "types": "json_extract_string(t, '$..type')",
        "classes": "json_extract_string(t, '$..class')",
        "fns": "list_transform(json_extract_string(t, '$..function_name'), lambda f: lower(f))",
        "tfns": "list_transform(json_extract_string(t, '$..function.function_name'), lambda f: lower(f))",
        "targs": "json_extract(t, '$..function.arguments')::VARCHAR[]",
        "quals": "list_concat(json_extract_string(t, '$..catalog'), json_extract_string(t, '$..schema'))",
        "paths": "list_transform(json_extract(t, '$..qualified_name.path'), lambda q: json_array_length(q))",
        "cats": "json_extract_string(t, '$..catalog_name')",
        "schemas": "json_extract_string(t, '$..schema_name')",
        "names": "list_transform(json_extract_string(t, '$..table_name'), lambda n: lower(n))",
        "ctes": "list_transform(list_concat(json_extract_string(t, '$..cte_map.map[*].key'), "
                "json_extract_string(t, '$..cte_name')), lambda n: lower(n))",
    }
    rules = f"""
        NOT json_extract(t, '$.error')::BOOLEAN
        AND json_array_length(t, '$.statements') = 1
        AND list_has_all({_list(NODE_TYPES)}, types)
        AND list_has_all({_list(EXPRESSION_CLASSES)}, classes)
        AND list_has_all({_list(SCALAR_FUNCTIONS | TABLE_FUNCTIONS)}, fns)
        AND list_has_all({_list(TABLE_FUNCTIONS)}, tfns)
        AND list_has_all(['[]'], targs)
        -- functions (and cast types) carry catalog/schema keys: all must be empty
        AND list_has_all([''], quals)
        -- base tables: one (catalog_name, schema_name, table_name) triple per BASE_TABLE node
        AND len(cats) = list_count(list_filter(types, lambda x: x = 'BASE_TABLE'))
        AND len(schemas) = len(cats) AND len(names) = len(cats)
        AND list_has_all([''], cats)
        AND list_bool_and(list_transform(list_zip(schemas, names), lambda sn:
              (sn[1] = '' AND list_contains(ctes, sn[2]))
              OR (sn[1] IN ('', 'main') AND regexp_full_match(sn[2], 'ducklake_[a-z0-9_]+'))
              OR (sn[1] = 'information_schema' AND sn[2] = 'tables'))) IS NOT false
        -- a qualified name longer than one part only for a schema-qualified table
        AND list_has_all([1, 2], paths)
        AND list_count(list_filter(paths, lambda n: n = 2)) = list_count(list_filter(schemas, lambda s: s <> ''))
        -- CTE names: plain, and never one that could shadow a server table or view
        AND list_bool_and(list_transform(ctes, lambda n:
              regexp_full_match(n, '[a-z][a-z0-9_]*') AND NOT regexp_matches(n, '{prefixes}'))) IS NOT false
        AND (len(ctes) = 0 OR NOT EXISTS (SELECT 1 FROM {p}.reserved_names WHERE list_contains(ctes, name)))
    """
    cols = ",\n        ".join(f"{expr} AS {name}" for name, expr in parts.items())
    con.execute(f"CREATE MACRO {p}.reader_tree_ok(t) AS (SELECT {rules} FROM (SELECT {cols}))")
    con.execute(f"""
    CREATE MACRO {p}.is_writer(cid) AS coalesce(
      (SELECT client_id_hash FROM system.main.quack_active_connections() WHERE connection_id = cid LIMIT 1)
        IN (SELECT h FROM {p}.writer_clients), false)
    """)
    # DuckDB evaluates both sides of OR and both arms of CASE, so a writer's statement is swapped
    # for a constant before the parse rather than skipped. The parse runs once, in an uncorrelated
    # subquery: a macro argument is substituted at every use, and the rules use `t` ~20 times.
    con.execute(f"""
    CREATE MACRO {p}.authz(cid, q) AS {p}.is_writer(cid) OR coalesce(
      (SELECT {p}.reader_tree_ok(t) FROM (SELECT json_serialize_sql(
         CASE WHEN {p}.is_writer(cid) OR length(q) > {MAX_READER_SQL} THEN 'SELECT 1' ELSE q END) AS t)),
      false) AND length(q) <= {MAX_READER_SQL}
    """)
    con.execute(f"""
    CREATE MACRO {p}.authn(cid, presented, server_token) AS
      presented = server_token OR sha256(presented) IN (SELECT h FROM {p}.reader_tokens)
    """)
    # qualified: an unqualified hook name could be shadowed by a macro a writer creates in main
    con.execute(f"SET GLOBAL quack_authentication_function = '{p}.authn'")
    con.execute(f"SET GLOBAL quack_authorization_function = '{p}.authz'")


def register_writer(con, uri, writer_token, client_id, prefix=CATALOG):
    """Learn the server's hash of the writer client id and record it. After quack_serve, before lock_down.

    Quack hashes client ids with HMAC-SHA256 under a random per-server key, so the hash can't be
    computed ahead of time: connect once with the client id and read it back.
    """
    from common import connect
    c = connect(exts=("quack",))
    c.execute(f"SET quack_default_client_id = '{client_id}'")
    c.execute(f"CREATE SECRET (TYPE quack, TOKEN '{writer_token}')")
    c.execute(f"ATTACH 'quack:{uri}' AS reg")
    cid = c.execute("SELECT quack_connection_id('reg')").fetchone()[0]
    h = con.execute("SELECT client_id_hash FROM quack_active_connections() WHERE connection_id = ?",
                    [cid]).fetchone()[0]
    con.execute(f"INSERT INTO {prefix}.main.writer_clients VALUES (?)", [h])
    c.execute("DETACH reg")
    c.close()
    return h
