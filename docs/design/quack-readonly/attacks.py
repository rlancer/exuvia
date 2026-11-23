"""Attack a served lake with the reader token. Usage: attacks.py PORT DATA_PATH [--expect-policy]

Classifies every attempt as POLICY (denied by the authorization hook), ERROR (got past the
hook but failed when run), or RAN (succeeded). With --expect-policy, anything but POLICY is
a failure. A writer setup and a final integrity check bracket the run.
"""
import os
import sys
import time

from common import READER_TOKEN, WRITER_TOKEN, connect, lake_client

port, data = int(sys.argv[1]), sys.argv[2]
expect_policy = "--expect-policy" in sys.argv
uri = f"quack://127.0.0.1:{port}"
here = os.getcwd().replace("\\", "/")
secret = f"{here}/secret.csv"
with open(secret, "w") as f:
    f.write("x\nTOPSECRET\n")

w = lake_client(port, WRITER_TOKEN, data)
w.execute("CREATE TABLE lake.prices AS SELECT DATE '2026-01-01' + i::INT AS d, i::DOUBLE AS px FROM range(100) t(i)")
w.execute("INSERT INTO lake.prices VALUES (DATE '2027-01-01', 1.0)")  # inlined: gives flush something to write
before = (w.execute("SELECT count(*) FROM lake.prices").fetchone()[0],
          w.execute("SELECT count(*) FROM lake.snapshots()").fetchone()[0])


def denied_by_hook(msg):
    # policy.py raises "read-only token: <reason>"; policy_sql.py's macro returns false
    return "read-only token" in msg or "Authorization failed" in msg


def classify(fn):
    try:
        fn()
        return "RAN", ""
    except Exception as e:
        msg = str(e).replace("\n", " ")
        return ("POLICY" if denied_by_hook(msg) else "ERROR"), msg[:150]


results = []


def attempt(category, label, fn):
    verdict, msg = classify(fn)
    results.append((category, label, verdict, msg))


# ---- 1. raw SQL sent straight to the server with the reader token ----
raw = connect()


def rq(sql):
    return lambda: raw.execute("SELECT * FROM quack_query(?, ?, token := ?)", [uri, sql, READER_TOKEN]).fetchall()


RAW = {
    "Writes to catalog tables": [
        "DELETE FROM ducklake_snapshot",
        "UPDATE ducklake_snapshot SET snapshot_time = now()",
        "INSERT INTO ducklake_snapshot VALUES (999, now(), 0, 0, 0)",
        "DROP TABLE ducklake_snapshot",
        "TRUNCATE ducklake_snapshot",
        "ALTER TABLE ducklake_snapshot ADD COLUMN z INT",
        "CREATE TABLE evil AS SELECT 1",
        "CREATE TEMP TABLE evil AS SELECT 1",
        "CREATE MACRO evil() AS 1",
        "CREATE VIEW evil AS SELECT 1",
        "WITH x AS (DELETE FROM ducklake_snapshot RETURNING *) SELECT * FROM x",
    ],
    "Statement smuggling": [
        "SELECT 1; DELETE FROM ducklake_snapshot",
        "SELECT 1 --\n; DELETE FROM ducklake_snapshot",
        "SELECT 1 /* ; */ ; DELETE FROM ducklake_snapshot",
        "SELECT ';'; DELETE FROM ducklake_snapshot",
        "SELECT * FROM ducklake_snapshot; SELECT * FROM ducklake_snapshot",
    ],
    "Session and server control": [
        "BEGIN TRANSACTION", "CHECKPOINT", "VACUUM", "USE system",
        "ATTACH 'evil.duckdb' AS evil",
        "SET enable_external_access = true", "RESET enable_external_access",
        "SET GLOBAL quack_authorization_function = 'quack_nop_authorization'",
        "SET VARIABLE v = 1", "PRAGMA version", "CALL pragma_version()",
        "INSTALL httpfs", "LOAD httpfs", "PREPARE p AS SELECT 1",
        f"COPY ducklake_snapshot TO '{here}/out.csv'",
        f"COPY (SELECT 1) TO '{here}/out2.csv'",
        f"EXPORT DATABASE '{here}/exported'",
        "DESCRIBE ducklake_snapshot", "SUMMARIZE ducklake_snapshot", "SHOW TABLES",
    ],
    "File and network access": [
        f"SELECT * FROM read_csv('{secret}')",
        f"SELECT * FROM read_text('{secret}')",
        f"SELECT * FROM read_blob('{secret}')",
        f"SELECT * FROM '{secret}'",
        "SELECT * FROM 'secret.csv'",
        "SELECT * FROM glob('*')",
        f"SELECT * FROM ducklake_snapshot WHERE snapshot_id IN (SELECT length(content) FROM read_text('{secret}'))",
        f"SELECT 1 UNION ALL SELECT length(content) FROM read_text('{secret}')",
        f"SELECT (SELECT content FROM read_text('{secret}'))",
        f"WITH \"{secret}\" AS (SELECT 1) SELECT * FROM \"{secret}\"",
        f"SELECT * FROM (WITH r AS (SELECT 1) SELECT 1), \"{secret}\"",
        f"WITH r AS (SELECT 1) SELECT * FROM (SELECT * FROM r), '{secret}'",
        "SELECT * FROM read_csv('http://127.0.0.1:9/x.csv')",
        f"SELECT * FROM quack_query('{uri}', 'DELETE FROM ducklake_snapshot', token := '{READER_TOKEN}')",
    ],
    "Settings, secrets, other sessions": [
        "SELECT * FROM pg_settings",
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM system.main.duckdb_settings()",
        "SELECT * FROM duckdb_secrets()",
        "SELECT current_setting('quack_authorization_function')",
        "SELECT getenv('PATH')",
        "SELECT * FROM quack_active_connections()",
        "SELECT * FROM duckdb_databases",
        "WITH pg_settings AS (SELECT 1) SELECT * FROM (SELECT 1), pg_settings",
        "WITH duckdb_databases AS (SELECT 1) SELECT * FROM duckdb_databases",
        "WITH ducklake_snapshot AS (SELECT 1) SELECT * FROM ducklake_snapshot",
        "SELECT * FROM information_schema.columns",
    ],
    "Dangerous or unlisted functions": [
        "SELECT * FROM query('DELETE FROM ducklake_snapshot')",
        "SELECT * FROM query_table('ducklake_snapshot')",
        "SELECT exuvia_policy_authn('x', 'writer-token-0123456789', '')",
        "SELECT exuvia_policy_authz('x', 'DELETE FROM ducklake_snapshot')",
        "SELECT list_transform([1], x -> x + 1)",
        "SELECT [x FOR x IN [1, 2]]",
        "SELECT COLUMNS(c -> true) FROM ducklake_snapshot",
        "SELECT \"read_text\"('x')",
        "SELECT READ_TEXT('x')",
        "SELECT main.max(1)",
        "SELECT system.main.max(1)",
        "SELECT max(snapshot_id) OVER () FROM ducklake_snapshot",
        "SELECT * FROM range(10)",
        "SELECT * FROM duckdb_tables('x')",
        "SELECT 'a' || 'b'",
    ],
    "Other catalogs and odd shapes": [
        "SELECT * FROM \"catalog\".main.ducklake_snapshot",
        "SELECT * FROM temp.main.x",
        "SELECT * FROM system.main.duckdb_tables",
        "SELECT * FROM exuvia_policy.main.anything",
        "SELECT * FROM ducklakе_snapshot",  # Cyrillic e
        "SELECT * FROM \"ducklake_snapshot.csv\"",
        "VALUES (1)",
        "SELECT $1",
        "PIVOT ducklake_snapshot ON schema_version",
    ],
}
for category, sqls in RAW.items():
    for sql in sqls:
        attempt(category, sql, rq(sql))

# ---- 2. DuckLake attached with the reader token but WITHOUT READ_ONLY ----
try:
    r = lake_client(port, READER_TOKEN, data, alias="lake")
except Exception as e:
    r = None
    results.append(("DuckLake writes (writable attach)", "attach itself", "ERROR", str(e)[:150]))
for label, sql in [] if r is None else [
    ("insert", "INSERT INTO lake.prices VALUES (DATE '2030-01-01', 1)"),
    ("update", "UPDATE lake.prices SET px = 0"),
    ("delete", "DELETE FROM lake.prices"),
    ("merge", "MERGE INTO lake.prices p USING (SELECT DATE '2026-01-01' d, 0.0 px) n ON p.d = n.d WHEN MATCHED THEN UPDATE SET px = n.px"),
    ("create table", "CREATE TABLE lake.evil (x INT)"),
    ("drop table", "DROP TABLE lake.prices"),
    ("alter table", "ALTER TABLE lake.prices ADD COLUMN z INT"),
    ("set_option", "CALL lake.set_option('data_inlining_row_limit', 0)"),
    ("expire snapshots", "CALL ducklake_expire_snapshots('lake', older_than => now())"),
    ("flush inlined", "CALL ducklake_flush_inlined_data('lake')"),
    ("merge files", "CALL ducklake_merge_adjacent_files('lake')"),
]:
    attempt("DuckLake writes (writable attach)", label, lambda sql=sql: r.execute(sql).fetchall())

# ---- 3. plain Quack attach with the reader token ----
p = connect()
p.execute(f"CREATE SECRET (TYPE quack, TOKEN '{READER_TOKEN}')")
try:
    p.execute(f"ATTACH 'quack:{uri}' AS raw")
    for label, sql in [("delete via quack attach", "DELETE FROM raw.ducklake_snapshot"),
                       ("create via quack attach", "CREATE TABLE raw.evil (x INT)"),
                       ("drop via quack attach", "DROP TABLE raw.ducklake_snapshot")]:
        attempt("Plain Quack attach", label, lambda sql=sql: p.execute(sql).fetchall())
except Exception as e:
    results.append(("Plain Quack attach", "attach itself", "POLICY" if denied_by_hook(str(e)) else "ERROR", str(e)[:150]))

# ---- 4. identity ----
def wrong_token():
    c = connect()
    return c.execute("SELECT * FROM quack_query(?, 'SELECT 1', token := 'not-a-token')", [uri]).fetchall()


def borrowed_client_id():
    c = connect()
    c.execute(f"CREATE SECRET (TYPE quack, TOKEN '{READER_TOKEN}')")
    c.execute(f"ATTACH 'quack:{uri}' AS ident (CLIENT_ID 'same-as-writer')")
    w2 = connect()
    w2.execute(f"CREATE SECRET (TYPE quack, TOKEN '{WRITER_TOKEN}')")
    w2.execute(f"ATTACH 'quack:{uri}' AS ident (CLIENT_ID 'same-as-writer')")
    return c.execute("DELETE FROM ident.ducklake_snapshot").fetchall()


ident = [("wrong token", wrong_token), ("reader reusing the writer's CLIENT_ID", borrowed_client_id)]
for label, fn in ident:
    verdict, msg = classify(fn)
    if verdict == "ERROR" and "Authentication failed" in msg:
        verdict = "POLICY"
    results.append(("Identity", label, verdict, msg))

# ---- 5. cost: a query the policy allows but that never ends ----
t = time.time()
verdict, msg = classify(rq("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n - 1 FROM r) SELECT count_star() FROM r"))
results.append(("Runaway query (allowed by policy)", "infinite recursive CTE", verdict, f"{time.time() - t:.1f}s: {msg}"))

# ---- 6. positive controls: plain reads with the raw reader token ----
for sql in ["SELECT * FROM ducklake_snapshot", "SELECT * FROM information_schema.tables",
            "SELECT * FROM duckdb_tables()", "SELECT max(snapshot_id) - 1 FROM ducklake_snapshot",
            "FROM ducklake_snapshot SELECT snapshot_id ORDER BY snapshot_id DESC LIMIT 1"]:
    verdict, msg = classify(rq(sql))
    results.append(("Allowed reads (should RAN)", sql, verdict, msg))

try:
    after = (w.execute("SELECT count(*) FROM lake.prices").fetchone()[0],
             w.execute("SELECT count(*) FROM lake.snapshots()").fetchone()[0])
except Exception as e:
    after = f"unreadable: {str(e)[:100]}"

# ---- 7. last, because they can take the server down ----
for sql in ['DETACH "catalog"', f"SELECT * FROM quack_stop('{uri}')"]:
    attempt("Server takedown (run last)", sql, rq(sql))

# ---- report ----
failures = 0
cat = None
for category, label, verdict, msg in results:
    if category != cat:
        print(f"\n## {category}")
        cat = category
    ok = (verdict == "RAN") if category.startswith("Allowed") else \
         (verdict == "ERROR" and "Interrupted" in msg) if category.startswith("Runaway") else \
         (verdict == "POLICY") if expect_policy else True
    failures += not ok
    flag = "  " if ok else "!!"
    shown = label.replace("\n", "\\n")
    print(f"{flag} {verdict:<6} {shown[:95]}" + (f"  | {msg[:110]}" if verdict != "POLICY" and verdict != "RAN" or not ok else ""))
print(f"\nintegrity: rows/snapshots before {before}, after {after}: {'OK' if before == after else 'CHANGED'}")
print(f"leaked secret: {'YES' if any('TOPSECRET' in m for *_, m in results) else 'no'}")
print(f"files written by attacks: {[f for f in ('out.csv', 'out2.csv', 'exported', 'evil.duckdb') if os.path.exists(f)]}")
print(f"{len(results)} attempts, {failures} unexpected")
os._exit(0)
