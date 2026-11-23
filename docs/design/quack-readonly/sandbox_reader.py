"""A DuckLake reader inside exuvia's query sandbox. Usage: sandbox_reader.py PORT DATA_PATH

exuvia's query/view sandbox (Workspace.query_connection in exuvia/workspace.py) turns off
extension autoloading, limits file reads to a list (allowed_paths), then sets
enable_external_access = false and lock_configuration = true. This checks whether a DuckLake
reader attached over Quack with the reader token still works under that lock, and whether the
lock still holds with the lake attached:

  1. order: what has to happen before the lock (loading extensions, ATTACH)
  2. reads under the lock, including commits and tables made after it
  3. allowed_paths with the files that exist at lock time vs allowed_directories
  4. escapes from the locked connection
"""
import os
import sys

import duckdb

from common import READER_TOKEN, WRITER_TOKEN, ext_path, lake_client

port, data = int(sys.argv[1]), sys.argv[2]
uri = f"quack://127.0.0.1:{port}"
here = os.getcwd().replace("\\", "/")
secret = f"{here}/secret.csv"
with open(secret, "w") as f:
    f.write("x\nTOPSECRET\n")

w = lake_client(port, WRITER_TOKEN, data)
w.execute("CREATE TABLE lake.t AS SELECT i AS x FROM range(100) r(i)")   # a data file
w.execute("INSERT INTO lake.t VALUES (1000), (1001)")                   # inlined rows
v1 = w.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]

results = []


def check(section, label, fn, expect):
    """expect: 'ok' (must succeed), 'fail' (must raise) or a value the result must equal."""
    try:
        got = fn()
        ok = expect == "ok" or (expect != "fail" and got == expect)
        detail = repr(got)[:110]
    except Exception as e:
        ok = expect == "fail"
        detail = str(e).replace("\n", " ")[:110]
    results.append((section, label, ok, detail))
    return ok


def data_files():
    out = []
    for root, _, names in os.walk(data):
        out += [os.path.join(root, n).replace("\\", "/") for n in names]
    return out


def sandbox(order="attach-then-lock", paths=None, directories=False, url=False, pin=None):
    """A connection set up the way exuvia's sandbox is, with the lake attached as a reader.

    paths: allowed_paths (files); directories: allow DATA_PATH as a directory; url: allow the
    catalog server's URL; pin: attach at this snapshot version.
    """
    c = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
    for n in ("httpfs", "ducklake", "quack"):
        c.execute(f"LOAD '{ext_path(n)}'")
    c.execute(f"CREATE SECRET (TYPE quack, TOKEN '{READER_TOKEN}')")

    def attach():
        opts = f"DATA_PATH '{data}', READ_ONLY" + (f", SNAPSHOT_VERSION {pin}" if pin is not None else "")
        c.execute(f"ATTACH 'ducklake:quack:{uri}' AS lake ({opts})")

    def lock():
        allowed = list(paths or []) + ([f"http://127.0.0.1:{port}/quack"] if url else [])
        if allowed:
            c.execute("SET allowed_paths = ?", [allowed])
        if directories:
            c.execute("SET allowed_directories = ?", [[data]])
        c.execute("SET enable_external_access = false")
        c.execute("SET lock_configuration = true")

    if order == "attach-then-lock":
        attach()
        lock()
    else:
        lock()
        attach()
    return c


def snapshot():
    return w.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]


q = lambda c, sql: (lambda: c.execute(sql).fetchall())


def contents():
    """Row counts and sums of every table, read through the data files by a fresh client."""
    c = lake_client(port, WRITER_TOKEN, data, read_only=True)
    try:
        return c.execute("SELECT (SELECT count(*) || ':' || sum(x) FROM lake.t), "
                         "(SELECT count(*) || ':' || sum(y) FROM lake.u)").fetchone()
    finally:
        c.close()


def escapes(S, c, overwrite_target):
    before = contents()
    for label, sql in [
        ("read a file outside DATA_PATH", f"SELECT * FROM read_csv('{secret}')"),
        ("ATTACH another database", f"ATTACH '{here}/evil.duckdb' AS evil"),
        ("COPY to a file", f"COPY (SELECT 1) TO '{here}/out.csv'"),
        ("undo the lock", "SET enable_external_access = true"),
        ("another URL on the catalog server's host", f"SELECT * FROM read_csv('http://127.0.0.1:{port}/x.csv')"),
        ("write the lake", "INSERT INTO lake.t VALUES (1)"),
        ("write the metadata catalog", "DELETE FROM __ducklake_metadata_lake.ducklake_snapshot"),
        ("CREATE in the lake", "CREATE TABLE lake.evil AS SELECT 1"),
        ("raw SQL to the catalog server: read a file", f"SELECT * FROM quack_query('{uri}', 'SELECT * FROM read_text(''{secret}'')')"),
        ("raw SQL to the catalog server: a write", f"SELECT * FROM quack_query('{uri}', 'DELETE FROM ducklake_snapshot')"),
        ("INSTALL an extension", "INSTALL spatial"),
        ("COPY a new file into DATA_PATH", f"COPY (SELECT 1) TO '{data}evil.csv'"),
        ("COPY over an existing data file", f"COPY (SELECT 1 AS x) TO '{overwrite_target}' (FORMAT parquet)"),
    ]:
        check(S, label, q(c, sql), "fail")
    # attaching only reads, so a writable re-attach is allowed; writes through it must still fail
    check(S, "re-ATTACH the lake writable", q(c, f"ATTACH 'ducklake:quack:{uri}' AS lake2 (DATA_PATH '{data}')"), "ok")
    check(S, "write 100 rows through that attach", q(c, "INSERT INTO lake2.t SELECT i FROM range(100) r(i)"), "fail")
    check(S, "lake contents unchanged (read back by a fresh client)", contents, before)


def first_data_file():
    return sorted(f for f in data_files() if f.endswith(".parquet"))[0]


# ---- 1. as exuvia locks today ----
S = "1. exuvia's lock as it is, plus allowed_directories = DATA_PATH"
check(S, "lock first, then ATTACH the lake", lambda: sandbox(order="lock-then-attach", directories=True), "fail")
r = sandbox(directories=True)
check(S, "read after the lock", q(r, "SELECT count(*) FROM lake.t"), [(102,)])
r.close()
w.execute("CREATE TABLE lake.u AS SELECT i AS y FROM range(50) r(i)")

# ---- 2. fix B: pin the snapshot, allow exactly its files and the catalog URL ----
S = "2. Fix B: pinned snapshot + allowed_paths = files on disk + the catalog URL"
pin = snapshot()
b = sandbox(paths=data_files(), url=True, pin=pin)
check(S, f"count at the pinned snapshot {pin}", q(b, "SELECT count(*) FROM lake.t"), [(102,)])
check(S, "other table at the pin", q(b, "SELECT count(*) FROM lake.u"), [(50,)])
check(S, f"time travel to an older version ({v1})", q(b, f"SELECT count(*) FROM lake.t AT (VERSION => {v1})"), [(102,)])
check(S, "table_changes", q(b, f"SELECT count(*) FROM lake.table_changes('t', 0, {v1})"), [(102,)])
w.execute("INSERT INTO lake.t SELECT i FROM range(200, 300) r(i)")   # a new data file the list doesn't name
w.execute("INSERT INTO lake.t VALUES (2000)")                       # a new inlined row
check(S, "a commit after the lock: still the pinned count", q(b, "SELECT count(*) FROM lake.t"), [(102,)])
b.close()
b2 = sandbox(paths=data_files(), url=True, pin=snapshot())
check(S, "the next request (new pin, new file list) sees it", q(b2, "SELECT count(*) FROM lake.t"), [(203,)])
escapes("2b. Fix B: escapes (all must fail)", b2, first_data_file())
secrets = b2.execute("SELECT * FROM duckdb_secrets()").fetchall()
results.append(("2c. Token exposure", "duckdb_secrets() hides the reader token",
                not any(READER_TOKEN in str(row) for row in secrets), str(secrets)[:100]))
b2.close()

# ---- 3. fix A: allow the catalog URL and DATA_PATH as a directory (last: it can damage the lake) ----
S = "3. Fix A: the catalog URL + allowed_directories = DATA_PATH, no pin"
a = sandbox(directories=True, url=True)
check(S, "count", q(a, "SELECT count(*) FROM lake.t"), [(203,)])
w.execute("INSERT INTO lake.t SELECT i FROM range(300, 400) r(i)")
w.execute("CREATE TABLE lake.v AS SELECT 1 AS z")
check(S, "a commit made after the lock", q(a, "SELECT count(*) FROM lake.t"), [(303,)])
check(S, "a table created after the lock", q(a, "SELECT count(*) FROM lake.v"), [(1,)])
escapes("3b. Fix A: escapes (all must fail)", a, first_data_file())

# ---- report ----
failures, cat = 0, None
for section, label, ok, detail in results:
    if section != cat:
        print(f"\n## {section}")
        cat = section
    failures += not ok
    print(f"{'  ' if ok else '!!'} {label:<58} | {detail}")
print(f"\n{len(results)} checks, {failures} not as expected")
os._exit(0)
