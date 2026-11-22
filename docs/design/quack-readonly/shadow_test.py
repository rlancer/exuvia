"""Can a writer-token client shadow the hook with a same-named macro? Does qualifying the hook help?"""
import os
import sys

from common import READER_TOKEN, WRITER_TOKEN, connect
from policy import ReadOnlyPolicy

work = sys.argv[1]
os.makedirs(work)
os.chdir(work)
for qualified, port in ((False, 9561), (True, 9562)):
    s = connect(f"catalog{port}.duckdb")
    s.execute("CREATE TABLE ducklake_snapshot AS SELECT 1 AS snapshot_id")
    pol = ReadOnlyPolicy({WRITER_TOKEN: "writer", READER_TOKEN: "reader"})
    pol.install(s)
    if not qualified:  # policy.install sets the qualified names; undo that to show the hole
        s.execute("SET GLOBAL quack_authorization_function = 'exuvia_policy_authz'")
        s.execute("SET GLOBAL quack_authentication_function = 'exuvia_policy_authn'")
    uri = f"quack://127.0.0.1:{port}"
    s.execute(f"SELECT * FROM quack_serve('{uri}', token := '{WRITER_TOKEN}')").fetchall()
    c = connect()
    q = lambda sql, tok: c.execute("SELECT * FROM quack_query(?, ?, token := ?)", [uri, sql, tok]).fetchall()
    label = "qualified system.main hook" if qualified else "unqualified hook"
    try:
        print(label, "| reader read:", q("SELECT count_star() FROM ducklake_snapshot", READER_TOKEN))
    except Exception as e:
        print(label, "| reader read FAILED:", str(e)[:150])
    try:
        q("CREATE MACRO exuvia_policy_authz(cid, q) AS true", WRITER_TOKEN)
        print(label, "| writer created a shadowing macro in main")
    except Exception as e:
        print(label, "| writer macro failed:", str(e)[:120])
    try:
        q("DELETE FROM ducklake_snapshot", READER_TOKEN)
        print(label, "| reader DELETE afterwards: RAN  <-- policy bypassed")
    except Exception as e:
        print(label, "| reader DELETE afterwards: denied:", "read-only token" in str(e))
os._exit(0)
