"""Install the native read-only policy (native/exuvia_policy.c, built by native/build.sh).

    s = connect(catalog, config=UNSIGNED)      # the extension is unsigned
    install(s, ext_path, [READER_TOKEN])
    s.execute("SELECT * FROM quack_serve(uri, token := WRITER_TOKEN)")
    lock_down(s)

Same contract as policy.py: the token's role is bound to the connection id at authentication,
reader statements are checked against the allowlist, a denial says "read-only token: <reason>".
"""
UNSIGNED = {"allow_unsigned_extensions": "true"}
PREFIX = "system.main.exuvia_policy"


def install(con, ext_path, reader_tokens):
    if len(reader_tokens) != 1:
        raise ValueError("the prototype takes one reader token")
    reserved = sorted({r[0].lower() for r in con.execute(
        "SELECT view_name FROM duckdb_views() UNION SELECT table_name FROM duckdb_tables()").fetchall()})
    con.execute(f"LOAD '{ext_path}'")
    con.execute("SELECT exuvia_policy_configure(?, ?)", [reader_tokens[0], ",".join(reserved)]).fetchall()
    con.execute(f"SET GLOBAL quack_authentication_function = '{PREFIX}_authn'")
    con.execute(f"SET GLOBAL quack_authorization_function = '{PREFIX}_authz'")


def role(con, connection_id):
    return con.execute("SELECT exuvia_policy_role(?)", [connection_id]).fetchone()[0]


def forget(con, live_connection_ids, grace_seconds=30.0):
    return con.execute("SELECT exuvia_policy_forget(?, ?)",
                       [",".join(live_connection_ids), grace_seconds]).fetchone()[0]
