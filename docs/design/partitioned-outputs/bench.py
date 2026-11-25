"""Measure a manifest-of-chunks output against one file. Usage: bench.py WORKDIR [OUT_JSON] [--days N] [--tickers T]

Builds a daily node's history (one partition per day, T rows a day) with one
backfill commit, then times what exuvia would do per request, each in a fresh
DuckDB connection: build the sandbox (allowed_paths = the files, a view, the
lock) and run three queries. Layouts, for the first 1, 100, 1,000 and every day:

  one file    the whole history in one Parquet file (today's "full history" node)
  union       one read_parquet per file joined with UNION ALL BY NAME (today's `<node>__runs`)
  list        one read_parquet over the manifest's file list
  yearly      the manifest compacted to one file per year

Then: a daily commit on top of each layout, a correction to a compacted day,
identical rewrites, the logical id across compaction, a sandbox escape, and
storage against the full-history node.
"""
import datetime
import json
import os
import statistics
import sys
import time

import duckdb

import manifest as mf

args = sys.argv[1:]
opt = lambda name, default: int(args[args.index(name) + 1]) if name in args else default
DAYS, TICKERS = opt("--days", 10000), opt("--tickers", 500)
work = args[0].replace("\\", "/")
out_json = args[1] if len(args) > 1 and not args[1].startswith("--") else None
REPS = 3
results: dict = {"days": DAYS, "tickers": TICKERS}


def rows(first_day: int, n_days: int, tickers: int = TICKERS, salt: int = 0) -> str:
    return (f"SELECT (DATE '2000-01-03' + d.k::INTEGER)::DATE AS date, 'T' || lpad(t.i::VARCHAR, 3, '0') AS ticker, "
            f"((hash(d.k, t.i, {salt}) % 100000) / 100.0)::DECIMAL(12, 2) AS close, hash(t.i, d.k, {salt}) % 1000000 AS volume "
            f"FROM range({first_day}, {first_day + n_days}) d(k), range({tickers}) t(i)")


def day(k: int) -> str:
    return (datetime.date(2000, 1, 3) + datetime.timedelta(days=k)).isoformat()


def timed(fn, reps=None):
    reps = reps or REPS
    ms = []
    for _ in range(reps):
        t = time.perf_counter()
        out = fn()
        ms.append((time.perf_counter() - t) * 1000)
    return round(statistics.median(ms), 1), out


def lit(s):
    return "'" + s + "'"


def sandbox(files: list[str], select: str):
    """exuvia's query sandbox (Workspace._outputs_db): allowed files, a view, external access off, locked."""
    con = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
    con.execute("SET allowed_paths = ?", [sorted(files)])
    con.execute(f"CREATE VIEW prices AS {select}")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


QUERIES = {
    "count, sum": "SELECT count(*), sum(volume) FROM prices",
    "avg by ticker": "SELECT ticker, avg(close) FROM prices GROUP BY ticker ORDER BY ticker LIMIT 3",
    "last 30 days": None,  # filled per size: WHERE date >= the 30th day from the end
}


def measure(label, files, select, last30, pruned=None):
    """Sandbox setup, then each query in it; `pruned` = (files, select) exuvia would hand a date-filtered read."""
    global REPS
    REPS = 1 if len(files) > 2000 else 3  # 10,000 files take tens of seconds a query
    setup_ms, _ = timed(lambda: sandbox(files, select).close())
    r = {"files": len(files), "setup ms": setup_ms}
    con = sandbox(files, select)
    answers = {}
    for q, sql in QUERIES.items():
        sql = sql or f"SELECT count(*), sum(close) FROM prices WHERE date >= DATE {lit(last30)}"
        r[q], answers[q] = timed(lambda: con.execute(sql).fetchall())
    con.close()
    if pruned:
        pf, ps = pruned
        sql = f"SELECT count(*), sum(close) FROM prices WHERE date >= DATE {lit(last30)}"
        r["last 30, pruned files"] = len(pf)
        r["last 30 pruned ms"], got = timed(lambda: (lambda c: c.execute(sql).fetchall())(sandbox(pf, ps)))
        assert got == answers["last 30 days"], (got, answers["last 30 days"])
    print(f"  {label:<10} {json.dumps(r)}", flush=True)
    return r, answers


# ---- build: one backfill commit of every day
s = mf.Store(f"{work}/store")
t = time.perf_counter()
m = mf.replace(s, mf.empty("date"), rows(0, DAYS))
results["backfill commit s"] = round(time.perf_counter() - t, 1)
results["chunk bytes"] = sum(os.path.getsize(p) for p in s.files(m))
blob = json.dumps(m)
results["manifest json bytes"] = len(blob)
results["manifest parse ms"], _ = timed(lambda: json.loads(blob))
print(f"backfill of {DAYS} days x {TICKERS}: {results['backfill commit s']} s, {len(m['files'])} files, "
      f"{results['chunk bytes'] / 1e6:.1f} MB", flush=True)

t = time.perf_counter()
yearly = mf.compact(s, m, lambda d: d[:4])
results["compaction s"] = round(time.perf_counter() - t, 1)
results["yearly bytes"] = sum(os.path.getsize(p) for p in s.files(yearly))
assert mf.logical_id(yearly) == mf.logical_id(m) and mf.physical_id(yearly) != mf.physical_id(m)
print(f"compacted to {len(yearly['files'])} yearly files in {results['compaction s']} s, "
      f"{results['yearly bytes'] / 1e6:.1f} MB; logical id unchanged", flush=True)

# ---- reads by size
results["reads"] = {}
for n in sorted({1, 100, 1000, DAYS} & set(range(1, DAYS + 1))):
    cutoff = day(n)          # first day not included
    last30 = day(max(0, n - 30))
    sub = mf.prune(m, lambda p: p < cutoff)
    sub_y = {**mf.prune(yearly, lambda p: p < cutoff)}
    print(f"first {n} days ({n * TICKERS} rows)", flush=True)
    one = f"{work}/one-{n}.parquet"
    if not os.path.exists(one):
        s.con.execute(f"COPY ({rows(0, n)} ORDER BY ALL) TO '{one}' (FORMAT parquet, COMPRESSION zstd)")
    out, ans = {}, {}
    out["one file"], ans["one file"] = measure("one file", [one], f"SELECT * FROM read_parquet('{one}')", last30)
    files = s.files(sub)
    if n <= 1000:
        union = " UNION ALL BY NAME ".join(f"SELECT * FROM read_parquet({lit(f)})" for f in files)
        out["union"], ans["union"] = measure("union", files, union, last30)
    recent = mf.prune(sub, lambda p: p >= last30)
    out["list"], ans["list"] = measure("list", files, mf.select_sql(s, sub), last30,
                                       pruned=(s.files(recent), mf.select_sql(s, recent)))
    # the yearly files hold whole years: for a prefix, filter the rows of a partial last year
    sel_y = f"SELECT * FROM ({mf.select_sql(s, sub_y)}) WHERE date < DATE {lit(cutoff)}"
    out["yearly"], ans["yearly"] = measure("yearly", s.files(sub_y), sel_y, last30)
    for k, a in ans.items():
        assert {q: sorted(map(str, v)) for q, v in a.items()} == \
               {q: sorted(map(str, v)) for q, v in ans["one file"].items()}, f"{k} disagrees at {n}"
    results["reads"][n] = out
    results["reads"][n]["one file bytes"] = os.path.getsize(one)

# ---- commits on top of the full history
c = {}
c["daily commit, list ms"], m2 = timed(lambda: mf.replace(s, m, rows(DAYS, 1)), reps=3)
c["daily commit, yearly ms"], y2 = timed(lambda: mf.replace(s, yearly, rows(DAYS, 1)), reps=3)
assert len(m2["files"]) == len(m["files"]) + 1 and len(y2["files"]) == len(yearly["files"]) + 1
same = mf.replace(s, m2, rows(DAYS, 1))
c["identical daily rewrite: same manifest"] = same is m2
c["identical rewrite of a backfilled day: same manifest"] = mf.replace(s, m, rows(5, 1)) is m
fix_day = DAYS // 2
c["correct one day in a compacted year ms"], y3 = timed(lambda: mf.replace(s, yearly, rows(fix_day, 1, salt=1)), reps=3)
c["correction: logical id changed"] = mf.logical_id(y3) != mf.logical_id(yearly)
c["correction: files rewritten"] = len({f["file"] for f in y3["files"]} - {f["file"] for f in yearly["files"]})
chk = duckdb.connect()
before, after = (chk.execute(f"SELECT count(*), sum(close) FROM ({mf.select_sql(s, x)})").fetchone() for x in (yearly, y3))
c["correction: rows equal, sum changed"] = before[0] == after[0] and before[1] != after[1]
want = chk.execute(f"SELECT count(*), sum(close) FROM ({rows(0, DAYS)}) WHERE date <> DATE {lit(day(fix_day))}").fetchone()
got = chk.execute(f"SELECT count(*), sum(close) FROM ({mf.select_sql(s, y3)}) WHERE date <> DATE {lit(day(fix_day))}").fetchone()
c["correction: other days untouched"] = want == got
# compaction kept every row: the two layouts hold the same multiset
diff = chk.execute(f"SELECT count(*) FROM (({mf.select_sql(s, m)}) EXCEPT ALL ({mf.select_sql(s, yearly)}))").fetchone()[0]
c["compaction: rows identical"] = diff == 0
results["commits"] = c
print(json.dumps(c, indent=1), flush=True)

# ---- the sandbox only reads the manifest's files
recent = mf.prune(m, lambda p: p >= day(DAYS - 30))
sb = sandbox(s.files(recent), mf.select_sql(s, recent))
other = s.files(m)[0]
try:
    sb.execute(f"SELECT count(*) FROM read_parquet('{other}')").fetchall()
    results["sandbox: a chunk outside the manifest"] = "READ (escape!)"
except duckdb.Error as e:
    results["sandbox: a chunk outside the manifest"] = "refused: " + str(e).split("\n")[0][:80]
print("sandbox:", results["sandbox: a chunk outside the manifest"])

# ---- storage against the full-history node (one new full file per daily run)
one_full = results["reads"][DAYS]["one file bytes"]
results["full-history node, every daily file kept (estimate) bytes"] = int(one_full * (DAYS + 1) / 2)
print(f"storage: chunks {results['chunk bytes'] / 1e6:.0f} MB, yearly {results['yearly bytes'] / 1e6:.0f} MB, "
      f"one file {one_full / 1e6:.0f} MB, full-history node over {DAYS} daily runs about "
      f"{results['full-history node, every daily file kept (estimate) bytes'] / 1e9:.0f} GB")
if out_json:
    with open(out_json, "w") as f:
        json.dump(results, f, indent=1)
os._exit(0)
