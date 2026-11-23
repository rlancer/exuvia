"""Compare the hook implementations on logged statements. Usage: bench_hooks.py LOG.jsonl PORT [NATIVE_EXT]

Installs every policy in one serving instance (Python UDF, SQL macro, the native extension if
given, and a no-op macro as the floor), connects a writer and a reader so each policy has real
connection ids to recognise, then calls each authorization hook the way Quack does,
`SELECT hook(?, ?)`, over every logged statement, plus a 3,000-column writer statement (a hook
that parses writer statements pays for it there). Reports CPU time per call, which load from
other processes doesn't distort, and how many logged statements each hook denied (should be 0).
"""
import json
import sys
import time

import policy_sql
from common import READER_TOKEN, WRITER_CLIENT_ID, WRITER_TOKEN, connect
from policy import ReadOnlyPolicy

rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
port = int(sys.argv[2])
native_ext = sys.argv[3] if len(sys.argv) > 3 else None
reader_sql = [r["sql"] for r in rows if r["role"] == "reader"]
writer_sql = [r["sql"] for r in rows if r["role"] == "writer"]
wide = "SELECT " + ", ".join(f"{i} AS c{i}" for i in range(3000))

con = connect(config={"allow_unsigned_extensions": "true"})
pypol = ReadOnlyPolicy({WRITER_TOKEN: "writer", READER_TOKEN: "reader"})
pypol.install(con, prefix="exuvia_pyudf")
pypol.roles.update({"R": "reader", "W": "writer"})
con.execute("CREATE MACRO floor_hook(cid, q) AS true")
policy_sql.install(con, [READER_TOKEN])
if native_ext:  # installed last: its authentication hook binds the clients' roles below
    import policy_native
    policy_native.install(con, native_ext, [READER_TOKEN])
uri = f"quack://127.0.0.1:{port}"
con.execute(f"SELECT * FROM quack_serve('{uri}', token := '{WRITER_TOKEN}')").fetchall()
policy_sql.register_writer(con, uri, WRITER_TOKEN, WRITER_CLIENT_ID)


def client(token, client_id=None):
    c = connect(exts=("quack",))
    if client_id:
        c.execute(f"SET quack_default_client_id = '{client_id}'")
    c.execute(f"CREATE SECRET (TYPE quack, TOKEN '{token}')")
    c.execute(f"ATTACH 'quack:{uri}' AS srv")
    return c, c.execute("SELECT quack_connection_id('srv')").fetchone()[0]


wc, wid = client(WRITER_TOKEN, WRITER_CLIENT_ID)
rc, rid = client(READER_TOKEN)
hooks = {"floor (macro true)": ("floor_hook", "W", "R"),
         "python udf": ("system.main.exuvia_pyudf_authz", "W", "R"),
         "sql macro": ("exuvia_policy.main.authz", wid, rid)}
if native_ext:
    hooks["native (C)"] = ("system.main.exuvia_policy_authz", wid, rid)

def batch(fn, cid, sqls):
    """CPU ms per call (this process, all threads: unaffected by other processes' load) and denials."""
    cur, denied = con.cursor(), 0
    c0 = time.process_time()
    for sql in sqls:
        pypol.cache.clear()  # no hook gets a cache
        try:
            denied += not cur.execute(f"SELECT {fn}(?, ?)", [cid, sql]).fetchone()[0]
        except Exception:
            denied += 1
    return 1000 * (time.process_time() - c0) / len(sqls), denied


print(f"{'hook':<20} {'reader':>8} {'writer':>8} {'wide':>8}   CPU ms per call; denials")
for label, (fn, w, r) in hooks.items():
    cols, notes = [], []
    for who, sqls, cid in [("reader", reader_sql * 3, r), ("writer", writer_sql * 2, w), ("wide", [wide] * 10, w)]:
        ms, denied = batch(fn, cid, sqls)
        cols.append(f"{ms:>8.2f}")
        notes.append(f"{who} {denied}")
    print(f"{label:<20} {' '.join(cols)}   {', '.join(notes)}")
import os
os._exit(0)  # the Quack server threads keep the process alive otherwise
