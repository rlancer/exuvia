"""Replay logged statements through the installed hook. Usage: bench.py LOG.jsonl

Every logged reader statement must pass (regression check), and the full hook cost
(json_serialize_sql in the serving DB + the Python check) is timed with the cache off and on.
"""
import json
import statistics
import sys
import time

from common import READER_TOKEN, WRITER_TOKEN, connect
from policy import ReadOnlyPolicy

rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
reader_sql = [r["sql"] for r in rows if r["role"] == "reader"]
writer_sql = [r["sql"] for r in rows if r["role"] == "writer"]

con = connect()
policy = ReadOnlyPolicy({WRITER_TOKEN: "writer", READER_TOKEN: "reader"})
policy.install(con)
policy.roles["R"] = "reader"
policy.roles["W"] = "writer"


def run(cid, sqls, cache):
    times, denied = [], []
    for sql in sqls:
        if not cache:
            policy.cache.clear()
        t = time.perf_counter()
        try:
            con.execute("SELECT exuvia_policy_authz(?, ?)", [cid, sql]).fetchall()
        except Exception as e:
            denied.append((str(e)[:160], sql[:120]))
        times.append((time.perf_counter() - t) * 1000)
    return times, denied


for label, cid, sqls, cache in [("reader, no cache", "R", reader_sql, False),
                                ("reader, cache", "R", reader_sql, True),
                                ("writer (no check)", "W", writer_sql, False)]:
    times, denied = run(cid, sqls, cache)
    q = statistics.quantiles(times, n=20)
    print(f"{label:<18} n={len(times):<4} mean={statistics.mean(times):.2f} ms  p95={q[-1]:.2f} ms  max={max(times):.2f} ms  denied={len(denied)}")
    for d in denied[:5]:
        print("   DENIED:", d)
print(f"distinct reader statements: {len(set(reader_sql))}")

# floor: the cheapest possible hook (a Python UDF that returns true), same call shape
con.create_function("floor_hook", lambda cid, q: True, ["VARCHAR", "VARCHAR"], "BOOLEAN")
times = []
for sql in reader_sql:
    t = time.perf_counter()
    con.execute("SELECT floor_hook(?, ?)", ["R", sql]).fetchall()
    times.append((time.perf_counter() - t) * 1000)
print(f"{'floor (no-op hook)':<18} n={len(times):<4} mean={statistics.mean(times):.2f} ms  max={max(times):.2f} ms")
times = []
for sql in reader_sql:
    t = time.perf_counter()
    con.execute("SELECT json_serialize_sql(?)", [sql]).fetchall()
    times.append((time.perf_counter() - t) * 1000)
print(f"{'parse only':<18} n={len(times):<4} mean={statistics.mean(times):.2f} ms  max={max(times):.2f} ms")
