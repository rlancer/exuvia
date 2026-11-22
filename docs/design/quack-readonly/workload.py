"""A realistic writer + reader workload against a served lake. Usage: workload.py PORT DATA_PATH"""
import sys

from common import READER_TOKEN, WRITER_TOKEN, lake_client

port, data = int(sys.argv[1]), sys.argv[2]

w = lake_client(port, WRITER_TOKEN, data)
w.execute("CREATE TABLE lake.prices (d DATE, ticker VARCHAR, px DOUBLE)")
w.execute("INSERT INTO lake.prices VALUES (DATE '2026-01-02', 'A', 1.0)")  # inlined
w.execute("INSERT INTO lake.prices SELECT DATE '2020-01-01' + (i % 1000)::INT, 'T' || (i % 50), i FROM range(20000) t(i)")  # parquet
w.execute("""MERGE INTO lake.prices p USING (SELECT DATE '2026-01-02' d, 'A' ticker, 2.0 px) n
             ON p.d = n.d AND p.ticker = n.ticker
             WHEN MATCHED THEN UPDATE SET px = n.px WHEN NOT MATCHED THEN INSERT *""")
w.execute("DELETE FROM lake.prices WHERE ticker = 'T7'")
w.execute("CREATE TABLE lake.other AS SELECT 1 AS x")
w.execute("CREATE SCHEMA lake.s2")
w.execute("CREATE TABLE lake.s2.t (y INT)")
w.execute("INSERT INTO lake.s2.t VALUES (1)")
w.execute("CREATE TABLE lake.part (d DATE, region VARCHAR, v DOUBLE)")
w.execute("ALTER TABLE lake.part SET PARTITIONED BY (region)")
w.execute("INSERT INTO lake.part SELECT DATE '2024-01-01' + (i % 300)::INT, ['eu','us','ap'][1 + i % 3], i FROM range(3000) t(i)")
w.execute("CREATE VIEW lake.v_prices AS SELECT ticker, max(px) AS hi FROM lake.prices GROUP BY ticker")
w.execute("COMMENT ON TABLE lake.prices IS 'daily closes'")
w.execute("CALL ducklake_flush_inlined_data('lake')")
w.execute("CALL ducklake_merge_adjacent_files('lake')")
print("writer done", flush=True)

r = lake_client(port, READER_TOKEN, data, read_only=True)
print("count", r.execute("SELECT count(*) FROM lake.prices").fetchone())
print("filter", r.execute("SELECT * FROM lake.prices WHERE ticker='A'").fetchall())
print("tt", r.execute("SELECT count(*) FROM lake.prices AT (VERSION => 2)").fetchone())
print("changes", r.execute("SELECT count(*) FROM lake.table_changes('prices', 3, 4)").fetchone())
print("snapshots", len(r.execute("SELECT * FROM lake.snapshots()").fetchall()))
print("table_info", r.execute("SELECT count(*) FROM lake.table_info()").fetchone())
print("files", r.execute("SELECT count(*) FROM ducklake_list_files('lake', 'prices')").fetchone())
print("s2", r.execute("SELECT * FROM lake.s2.t").fetchall())
print("show", len(r.execute("SHOW ALL TABLES").fetchall()))
print("describe", len(r.execute("DESCRIBE lake.prices").fetchall()))
print("options", len(r.execute("SELECT * FROM lake.options()").fetchall()))
print("part filter", r.execute("SELECT count(*) FROM lake.part WHERE region = 'eu' AND d > DATE '2024-06-01'").fetchone())
print("view", len(r.execute("SELECT * FROM lake.v_prices").fetchall()))
print("tt ts", r.execute("SELECT count(*) FROM lake.prices AT (TIMESTAMP => now() - INTERVAL 1 SECOND)").fetchone())
cur = r.execute("SELECT id FROM lake.current_snapshot()").fetchone()[0]
print("insertions", r.execute(f"SELECT count(*) FROM lake.table_insertions('prices', 0, {cur})").fetchone())
print("deletions", r.execute(f"SELECT count(*) FROM lake.table_deletions('prices', 0, {cur})").fetchone())
print("files at v", r.execute("SELECT count(*) FROM ducklake_list_files('lake', 'prices', snapshot_version => 3)").fetchone())
print("snapshots fn", len(r.execute("SELECT * FROM ducklake_snapshots('lake')").fetchall()))
print("current snap", r.execute("SELECT * FROM lake.current_snapshot()").fetchone())
print("stats filter", r.execute("SELECT count(*) FROM lake.prices WHERE px BETWEEN 100 AND 200 AND ticker LIKE 'T1%'").fetchone())
print("join", r.execute("SELECT count(*) FROM lake.prices p JOIN lake.part q ON p.d = q.d").fetchone())
r2 = lake_client(port, READER_TOKEN, data, read_only=True, alias="lake")  # second reader, fresh attach
print("r2", r2.execute("SELECT count(*) FROM lake.s2.t").fetchone())
# a long-lived reader sees the writer's new rows
w.execute("INSERT INTO lake.prices VALUES (DATE '2026-01-05', 'B', 3.0)")
print("fresh", r.execute("SELECT count(*) FROM lake.prices WHERE ticker='B'").fetchone())
print("reader done", flush=True)
