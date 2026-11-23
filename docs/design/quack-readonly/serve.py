"""Serve a DuckLake catalog over Quack with the read-only policy.

Usage: serve.py CATALOG PORT STATS_JSON [--no-policy | --sql-policy | --native-policy EXT]
                [--no-lockdown] [--timeout SECONDS]

--sql-policy uses policy_sql.py (SQL macros), --native-policy policy_native.py (a C extension
at path EXT), instead of policy.py (Python UDFs). Neither can be timed from here, so their
stats carry no check timings (bench_hooks.py measures all three).
"""
import json
import sys
import threading
import time

import policy_sql
from common import READER_TOKEN, WRITER_CLIENT_ID, WRITER_TOKEN, connect
from policy import ReadOnlyPolicy, lock_down, self_test

catalog, port, stats_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
use_policy = "--no-policy" not in sys.argv
native_ext = sys.argv[sys.argv.index("--native-policy") + 1] if "--native-policy" in sys.argv else None
by_client_id = "--sql-policy" in sys.argv  # policy_sql: writer = registered client id
use_lockdown = "--no-lockdown" not in sys.argv
timeout = float(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 5.0

s = connect(catalog, config={"allow_unsigned_extensions": "true"} if native_ext else None)
policy = ReadOnlyPolicy({WRITER_TOKEN: "writer", READER_TOKEN: "reader"})
timings = []
writer_hashes = set()
if native_ext:
    import policy_native
    policy_native.install(s, native_ext, [READER_TOKEN])
elif by_client_id:
    policy_sql.install(s, [READER_TOKEN])
elif use_policy:
    original = policy._authz

    def timed(connection_id, sql):  # explicit arity: create_function inspects it
        t = time.perf_counter()
        try:
            return original(connection_id, sql)
        finally:
            timings.append(time.perf_counter() - t)

    policy._authz = timed
    policy.install(s)
else:
    # lockdown-only comparison: both tokens still authenticate, no statement check
    s.create_function("tokens_only_authn", policy._authn, ["VARCHAR", "VARCHAR", "VARCHAR"], "BOOLEAN",
                      side_effects=True)
    s.execute("SET GLOBAL quack_authentication_function = 'tokens_only_authn'")
uri = f"quack://127.0.0.1:{port}"
s.execute(f"SELECT * FROM quack_serve('{uri}', token := '{WRITER_TOKEN}')").fetchall()
if by_client_id:
    writer_hashes.add(policy_sql.register_writer(s, uri, WRITER_TOKEN, WRITER_CLIENT_ID))
if use_lockdown:
    lock_down(s)
if use_policy:
    self_test(connect(), uri, READER_TOKEN)  # refuse to run if the hook isn't enforcing

# Watchdog: cancel reader statements that run too long, forget closed connections.
# quack_cancel is a client-side call, so the watchdog attaches the server as a writer client
# from a separate in-memory instance (the serving instance is locked down).
cancelled = []


def watchdog():
    wd = connect()
    wd.execute(f"CREATE SECRET (TYPE quack, TOKEN '{WRITER_TOKEN}')")
    wd.execute(f"SET quack_default_client_id = '{WRITER_CLIENT_ID}'")
    wd.execute(f"ATTACH 'quack:{uri}' AS self")
    cur = s.cursor()
    while True:
        time.sleep(0.5)
        now_us = time.time() * 1e6
        rows = cur.execute(
            "SELECT connection_id, state, epoch_us(query_started_at), client_id_hash FROM quack_active_connections()"
        ).fetchall()
        if native_ext:
            policy_native.forget(cur, [r[0] for r in rows])
        elif use_policy and not by_client_id:
            policy.forget({r[0] for r in rows})
        for cid, state, started, client_hash in rows:
            if native_ext:
                is_reader = policy_native.role(cur, cid) == "reader"
            elif by_client_id:
                is_reader = client_hash not in writer_hashes
            else:
                is_reader = policy.roles.get(cid) == "reader" or not use_policy
            if is_reader and str(state).lower() == "active" and started and now_us - started > timeout * 1e6:
                wd.execute(f"SELECT * FROM quack_cancel('self', '{cid}')").fetchall()
                cancelled.append(cid)
        ts = sorted(timings)
        with open(stats_path, "w") as f:
            json.dump({
                "checks": len(ts),
                "mean_ms": 1000 * sum(ts) / len(ts) if ts else None,
                "p95_ms": 1000 * ts[int(0.95 * (len(ts) - 1))] if ts else None,
                "max_ms": 1000 * ts[-1] if ts else None,
                "roles_bound": len(policy.roles),
                "denials": len(policy.denials),
                "cancelled": cancelled,
                "states": sorted({r[1] for r in rows}),
            }, f)


threading.Thread(target=watchdog, daemon=True).start()
print("serving", flush=True)
while True:
    time.sleep(1)
