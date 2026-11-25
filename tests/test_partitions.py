"""Partitioned outputs (partition_by): daily runs, backfills, corrections,
compaction, downstream nodes, the query sandbox, full refreshes and the
data routes."""

import asyncio
import json
import textwrap
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from exuvia import partitioned as partitions
from exuvia.workspace import Workspace

AS_OF = {"params": {"as_of": {"type": "date", "default": "2026-03-02"}}}

# A daily source: each run returns the as_of day's rows, and the days it
# already has (exuvia.partitions()) in a column, so tests can see them.
# data/<day>.json, when present, overrides that day's values (a correction);
# data/range.json makes one run return many days (a backfill).
DAILY = '''
    import json, os
    import exuvia

    @exuvia.node(params=["as_of"], partition_by="date")
    def prices():
        import pandas as pd
        have = exuvia.partitions()
        if os.path.exists("data/range.json"):
            days = json.load(open("data/range.json"))
        else:
            days = [exuvia.param("as_of")]
        rows = []
        for day in days:
            px = int(day[-2:])
            if os.path.exists(f"data/{day}.json"):
                px = json.load(open(f"data/{day}.json"))
            rows += [{"date": day, "ticker": t, "px": px + i} for i, t in enumerate(["A", "B"])]
        print("have", json.dumps(have))
        return pd.DataFrame(rows)
'''
# Reads the whole history.
TOTAL = '''
    import exuvia

    @exuvia.node()
    def total(prices):
        return prices.groupby("ticker", as_index=False)["px"].sum().sort_values("ticker")
'''
COUNT_SQL = "select count(*) as n, count(distinct date) as days from prices\n"


def write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def project(root: Path) -> Workspace:
    root.mkdir(parents=True, exist_ok=True)
    (root / "exuvia.json").write_text(json.dumps(AS_OF), encoding="utf-8")
    write(root, "nodes/prices.py", DAILY)
    write(root, "nodes/total.py", TOTAL)
    write(root, "nodes/days.sql", COUNT_SQL)
    (root / "data").mkdir(exist_ok=True)
    return Workspace(root)


def go(ws, coro_fn):
    async def main():
        ws.bus.bind(asyncio.get_running_loop())
        res = await coro_fn()
        for _ in range(200):  # let cascades finish
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
        return res

    return asyncio.run(main())


def run(ws, targets, **kw):
    return go(ws, lambda: ws.run(targets, **kw))


def day(ws, d, **kw):
    finals = run(ws, ["prices"], params={"as_of": d}, force={"prices"}, **kw)
    assert finals["prices"]["status"] == "success", finals["prices"]["error"]
    return finals["prices"]


def manifest(ws):
    return ws.partitions_of("prices")


def log_have(ws, run_id):
    line = [ln for ln in ws.store.run(run_id)["log"].splitlines() if ln.startswith("have ")][-1]
    return json.loads(line[5:])


def test_daily_runs_build_one_history(tmp_path):
    ws = project(tmp_path)
    g = ws.graph
    assert g.nodes["prices"].partition_by == "date"
    assert g.effective_params("prices") == []  # as_of fills one output, it doesn't split it
    assert g.effective_params("total") == []

    r1 = day(ws, "2026-03-02")
    assert log_have(ws, r1["id"]) == []
    r2 = day(ws, "2026-03-03")
    assert log_have(ws, r2["id"]) == ["2026-03-02"]
    r3 = day(ws, "2026-03-04")
    assert log_have(ws, r3["id"]) == ["2026-03-02", "2026-03-03"]

    m = manifest(ws)
    assert [p["value"] for p in m["partitions"]] == ["2026-03-02", "2026-03-03", "2026-03-04"]
    assert len(m["files"]) == 3 and m["rows"] == 6
    assert r1["artifact_id"] != r2["artifact_id"] != r3["artifact_id"]
    # Chunks live where every output does, named by their bytes.
    for f in m["files"]:
        assert (tmp_path / ".exuvia" / "artifacts" / "prices" / f"{f['artifact_id']}.parquet").is_file()
    assert not (tmp_path / ".exuvia" / "staging").exists() or not any((tmp_path / ".exuvia" / "staging").iterdir())

    out = ws.preview("prices", limit=10)
    assert out["kind"] == "table" and out["total"] == 6 and len(out["rows"]) == 6
    # An older version stays readable by its run.
    assert ws.preview("prices", run_id=r1["id"])["total"] == 2


def test_downstream_reads_the_whole_history(tmp_path):
    ws = project(tmp_path)
    for d in ("2026-03-02", "2026-03-03"):
        day(ws, d)  # each new day cascades into the Python and the SQL node
    assert ws.store.last_success("total")["inputs"] == {"prices": ws.status()["prices"]["artifact_id"]}
    assert ws.preview("total")["rows"] == [["A", 5], ["B", 7]]
    assert ws.preview("days")["rows"] == [[4, 2]]
    st = ws.status()
    assert st["total"]["state"] == "fresh" and st["days"]["state"] == "fresh"

    # A new day cascades into both.
    day(ws, "2026-03-04")
    assert ws.preview("total")["rows"] == [["A", 9], ["B", 12]]
    assert ws.preview("days")["rows"] == [[6, 3]]


def test_identical_rows_write_nothing_and_stop_the_cascade(tmp_path):
    ws = project(tmp_path)
    first = day(ws, "2026-03-02")
    run(ws, ["total"])
    files = sorted((tmp_path / ".exuvia" / "artifacts" / "prices").iterdir())
    total_runs = len(ws.store.runs_for("total"))
    again = day(ws, "2026-03-02")
    assert again["artifact_id"] == first["artifact_id"] and again["version"] == first["version"]
    assert sorted((tmp_path / ".exuvia" / "artifacts" / "prices").iterdir()) == files
    assert "no partition changed" in ws.store.run(again["id"])["log"]
    assert len(ws.store.runs_for("total")) == total_runs  # nothing cascaded
    assert ws.status()["total"]["state"] == "fresh"


def test_backfill_writes_every_day_in_one_run_and_corrections_replace_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(partitions, "COMPACT_AFTER", 4)
    ws = project(tmp_path)
    days = [f"2025-12-{d:02d}" for d in range(20, 32)] + [f"2026-01-{d:02d}" for d in range(1, 11)]
    (tmp_path / "data" / "range.json").write_text(json.dumps(days), encoding="utf-8")
    backfill = day(ws, "2026-01-10")
    (tmp_path / "data" / "range.json").unlink()
    m = manifest(ws)
    assert len(m["partitions"]) == 22 and m["rows"] == 44
    # 22 day chunks were folded into one file per year in the same commit.
    assert [f["partitions"][0][:4] for f in m["files"]] in (["2025", "2026"], ["2026", "2025"])
    assert len(list((tmp_path / ".exuvia" / "artifacts" / "prices").glob("*.parquet"))) == 2

    run(ws, ["total"])
    # A day the backfill wrote, rewritten with identical rows: nothing moves.
    same = day(ws, "2026-01-05")
    assert same["artifact_id"] == backfill["artifact_id"]

    # A correction to a day inside a compacted year rewrites that year
    # without the day, adds the day's chunk, and changes only that day.
    before = {p["value"]: p["digest"] for p in m["partitions"]}
    (tmp_path / "data" / "2025-12-24.json").write_text("100", encoding="utf-8")
    fixed = day(ws, "2025-12-24")
    assert fixed["artifact_id"] != backfill["artifact_id"]
    after = {p["value"]: p["digest"] for p in manifest(ws)["partitions"]}
    assert {v for v in before if before[v] != after[v]} == {"2025-12-24"}
    files = manifest(ws)["files"]
    assert len(files) == 3
    assert any(f["partitions"] == ["2025-12-24"] for f in files)
    assert ws.query("select px from prices where date = '2025-12-24' order by px")["rows"] == [[100], [101]]
    assert ws.query("select count(*) from prices")["rows"] == [[44]]
    # The correction cascaded to the downstream total.
    assert ws.status()["total"]["state"] == "fresh"
    assert ws.preview("total")["rows"][0][1] == sum(int(d[-2:]) for d in days) - 24 + 100


def test_compaction_never_changes_the_output_id(tmp_path, monkeypatch):
    """The same days, committed with and without compaction, have the same
    id, so compacting can never make a downstream node stale."""
    ids = {}
    for name, limit in (("loose", 1000), ("tight", 2)):
        monkeypatch.setattr(partitions, "COMPACT_AFTER", limit)
        ws = project(tmp_path / name)
        for d in range(2, 9):
            day(ws, f"2026-03-{d:02d}")
            if d == 3:
                run(ws, ["total"])
        ids[name] = (ws.status()["prices"]["artifact_id"], len(manifest(ws)["files"]))
        assert ws.status()["total"]["state"] == "fresh"
        assert ws.query("select count(*) from prices")["rows"] == [[14]]
    assert ids["loose"][0] == ids["tight"][0]
    assert ids["loose"][1] == 7 and ids["tight"][1] < 7


def test_compaction_keeps_every_row(tmp_path):
    root = tmp_path
    out = root / ".exuvia" / "artifacts" / "n"
    w = partitions.Writer(out, lambda p: Path(p).relative_to(root).as_posix(), lambda rel: str(root / rel))
    m = partitions.Manifest("d")
    for i, extra in enumerate([None, None, "x"]):  # a column added on the third day
        staged = root / f"s{i}.parquet"
        df = pd.DataFrame({"d": [f"2026-01-0{i + 1}"] * 2, "v": [i, i + 10]})
        if extra:
            df["e"] = extra
        df.to_parquet(staged)
        m = partitions.commit(m, str(staged), w).manifest
    loose = [str(root / f["file"]) for f in m.files]
    tight = partitions.compact(m, w, force=True)
    assert tight.logical_id == m.logical_id and len(tight.files) == 1
    import duckdb

    con = duckdb.connect()
    a = f"select * from read_parquet({loose}, union_by_name = true)"
    b = f"select * from read_parquet({[str(root / f['file']) for f in tight.files]}, union_by_name = true)"
    assert con.execute(f"select count(*) from ({a} except all {b})").fetchone() == (0,)
    assert con.execute(f"select count(*) from ({b} except all {a})").fetchone() == (0,)
    assert con.execute(f"select count(*) from ({b})").fetchone() == (6,)


def test_digests_hash_rows_not_bytes(tmp_path):
    """One day written alone and the same day written by a many-day pass
    give different files but the same digest."""
    root = tmp_path
    w = partitions.Writer(root / "out", lambda p: Path(p).relative_to(root).as_posix(), lambda rel: str(root / rel))
    one = root / "one.parquet"
    many = root / "many.parquet"
    pd.DataFrame({"d": ["a", "a"], "v": [2, 1]}).to_parquet(one)
    pd.DataFrame({"d": ["a", "b", "a"], "v": [1, 5, 2]}).to_parquet(many)
    m1 = partitions.commit(partitions.Manifest("d"), str(one), w).manifest
    m2 = partitions.commit(partitions.Manifest("d"), str(many), w).manifest
    assert m1.partitions["a"] == m2.partitions["a"]
    # Values that would make bad folder names still split cleanly.
    odd = root / "odd.parquet"
    pd.DataFrame({"d": ["..", "a/b", "x=y", ".."], "v": [1, 2, 3, 4]}).to_parquet(odd)
    m3 = partitions.commit(partitions.Manifest("d"), str(odd), w).manifest
    assert sorted(m3.partitions) == ["..", "a/b", "x=y"] and m3.rows == 4


def test_the_sandbox_reads_exactly_the_manifest(tmp_path):
    ws = project(tmp_path)
    day(ws, "2026-03-02")
    old = manifest(ws)["files"][0]["artifact_id"]
    (tmp_path / "data" / "2026-03-02.json").write_text("50", encoding="utf-8")
    day(ws, "2026-03-02")  # a correction: the old chunk is no longer in the manifest
    current = manifest(ws)["files"][0]["artifact_id"]
    assert current != old
    base = tmp_path / ".exuvia" / "artifacts" / "prices"
    ok = ws.query(f"select count(*) from read_parquet('{(base / f'{current}.parquet').as_posix()}')")
    assert ok["rows"] == [[2]]
    with pytest.raises(Exception, match="(?i)permission|not allowed|disabled"):
        ws.query(f"select * from read_parquet('{(base / f'{old}.parquet').as_posix()}')")
    assert ws.query("select px from prices order by px")["rows"] == [[50], [51]]


def test_full_refresh_starts_from_nothing(tmp_path):
    ws = project(tmp_path)
    for d in ("2026-03-02", "2026-03-03"):
        day(ws, d)
    with pytest.raises(ValueError, match="partitioned"):
        run(ws, ["total"], full_refresh={"total"})
    r = day(ws, "2026-03-04", full_refresh={"prices"})
    assert log_have(ws, r["id"]) == []  # the node is told it has nothing
    assert [p["value"] for p in manifest(ws)["partitions"]] == ["2026-03-04"]
    assert ws.preview("prices")["total"] == 2


def test_partitioned_sql_node_and_last_partition(tmp_path):
    ws = project(tmp_path)
    write(tmp_path, "nodes/src.py", '''
        import exuvia

        @exuvia.node()
        def src():
            import pandas as pd
            return pd.DataFrame({"d": ["2026-01-01", "2026-01-02", "2026-01-03"], "v": [1, 2, 3]})
    ''')
    write(tmp_path, "nodes/inc.sql", '''
        -- copies what is new in src
        -- partition_by: d
        select * from src where $last_partition is null or d > $last_partition
    ''')
    ws.reload()
    assert ws.graph.nodes["inc"].partition_by == "d"
    finals = run(ws, ["inc"])
    assert finals["inc"]["status"] == "success", finals["inc"]["error"]
    assert ws.preview("inc")["total"] == 3
    again = run(ws, ["inc"], force={"inc"})["inc"]
    assert again["status"] == "success" and again["artifact_id"] == finals["inc"]["artifact_id"]


def test_bad_partitioned_returns_fail_the_run(tmp_path):
    ws = project(tmp_path)
    write(tmp_path, "nodes/nokey.py", '''
        import exuvia

        @exuvia.node(partition_by="date")
        def nokey():
            import pandas as pd
            return pd.DataFrame({"x": [1]})
    ''')
    write(tmp_path, "nodes/nulls.py", '''
        import exuvia

        @exuvia.node(partition_by="date")
        def nulls():
            import pandas as pd
            return pd.DataFrame({"date": [None, "2026-01-01"], "x": [1, 2]})
    ''')
    write(tmp_path, "nodes/notable.py", '''
        import exuvia

        @exuvia.node(partition_by="date")
        def notable():
            return {"date": "2026-01-01"}
    ''')
    write(tmp_path, "nodes/nothing.py", '''
        import exuvia

        @exuvia.node(partition_by="date")
        def nothing():
            return None
    ''')
    ws.reload()
    finals = run(ws, ["nokey", "nulls", "notable", "nothing"])
    assert "no `date` column" in finals["nokey"]["error"]
    assert "1 row returned has no `date`" in finals["nulls"]["error"]
    assert "returns one table" in finals["notable"]["error"]
    assert finals["nothing"]["status"] == "success" and finals["nothing"]["rows"] == 0
    assert ws.preview("nothing")["rows"] == []


def test_data_routes_prune_and_list_partitions(tmp_path):
    from exuvia.server import create_app

    root = tmp_path / "p"
    project(root)
    with TestClient(create_app(root)) as c:
        for d in ("2026-03-02", "2026-03-03", "2026-03-04"):
            res = c.post("/api/nodes/prices/run", json={"wait": 120, "params": {"as_of": d}})
            assert res.json()["ok"], res.text
        m = c.get("/api/nodes/prices/partitions").json()
        assert [p["value"] for p in m["partitions"]] == ["2026-03-02", "2026-03-03", "2026-03-04"]
        out = c.get("/api/nodes/prices/data", params={"partitions": "2026-03-03.."}).json()
        assert out["total"] == 4 and {r[0] for r in out["rows"]} == {"2026-03-03", "2026-03-04"}
        out = c.get("/api/nodes/prices/data", params={"partitions": "2026-03-02"}).json()
        assert out["total"] == 2 and {r[0] for r in out["rows"]} == {"2026-03-02"}
        csv = c.get("/api/nodes/prices/export", params={"format": "csv", "partitions": "..2026-03-03"})
        assert csv.status_code == 200 and csv.text.count("\n") == 5  # header and four rows
        chunk = c.get(f"/api/artifacts/prices/{m['files'][0]['artifact_id']}.parquet")
        assert chunk.status_code == 200 and chunk.content[:4] == b"PAR1"
        # The output's own id names no single file.
        assert c.get(f"/api/artifacts/prices/{m['artifact_id']}.parquet").status_code == 404
        assert c.get("/api/nodes/total/partitions").status_code == 404
        bad = c.post("/api/nodes/total/run", json={"full_refresh": True})
        assert bad.status_code == 400
        # A late day for an earlier date: rows and files still come in partition order.
        res = c.post("/api/nodes/prices/run", json={"wait": 120, "params": {"as_of": "2026-02-27"}})
        assert res.json()["ok"], res.text
        days = [r[0] for r in c.get("/api/nodes/prices/data").json()["rows"]]
        assert days == sorted(days) and days[0] == "2026-02-27"
        m = c.get("/api/nodes/prices/partitions").json()
        assert [f["partitions"][0] for f in m["files"]] == ["2026-02-27", "2026-03-02", "2026-03-03", "2026-03-04"]
        # A range with no partitions has no rows, but the output's columns.
        out = c.get("/api/nodes/prices/data", params={"partitions": "2030-01-01.."}).json()
        assert out["total"] == 0 and out["rows"] == [] and [col["name"] for col in out["columns"]] == ["date", "ticker", "px"]
        csv = c.get("/api/nodes/prices/export", params={"format": "csv", "partitions": "2030-01-01.."})
        assert csv.status_code == 200 and csv.text.strip() == '"date","ticker","px"'
        # partitions= on a node that isn't partitioned is refused, not ignored.
        assert c.get("/api/nodes/total/data", params={"partitions": "2026-03-02"}).status_code == 400
        assert c.get("/api/nodes/total/export", params={"format": "csv", "partitions": "x"}).status_code == 400
        res = c.post("/api/nodes/prices/run", json={"wait": 120, "full_refresh": True, "params": {"as_of": "2026-03-09"}})
        assert res.json()["ok"], res.text
        assert [p["value"] for p in c.get("/api/nodes/prices/partitions").json()["partitions"]] == ["2026-03-09"]


def test_push_replaces_the_partitions_it_holds(tmp_path):
    ws = project(tmp_path)
    write(tmp_path, "nodes/feed.py", '''
        import exuvia

        @exuvia.node(push=True, partition_by="date")
        def feed():
            ...
    ''')
    ws.reload()

    def push(rows):
        path = tmp_path / "push.parquet"
        pd.DataFrame(rows).to_parquet(path)
        return go(ws, lambda: ws.push("feed", path, "parquet"))

    first = push({"date": ["2026-01-01", "2026-01-02"], "v": [1, 2]})
    assert first["status"] == "success", first["error"]
    second = push({"date": ["2026-01-02", "2026-01-03"], "v": [20, 3]})
    assert second["status"] == "success" and second["artifact_id"] != first["artifact_id"]
    assert ws.query("select date, v from feed order by 1")["rows"] == [
        ["2026-01-01", 1], ["2026-01-02", 20], ["2026-01-03", 3]
    ]


def test_hub_export_carries_chunks_and_manifests(tmp_path):
    """A restored copy of the project reads the same partitioned output."""
    import shutil

    ws = project(tmp_path / "a")
    for d in ("2026-03-02", "2026-03-03"):
        day(ws, d)
    aid = ws.status()["prices"]["artifact_id"]
    copy = tmp_path / "b"
    copy.mkdir()
    ws.store.backup_to(copy / "exuvia.db")
    ws.store.close()
    shutil.copytree(tmp_path / "a", copy / "proj", ignore=shutil.ignore_patterns("exuvia.db*"))
    (copy / "exuvia.db").replace(copy / "proj" / ".exuvia" / "exuvia.db")
    ws2 = Workspace(copy / "proj")
    assert ws2.status()["prices"]["artifact_id"] == aid
    assert ws2.query("select count(*) from prices")["rows"] == [[4]]


def test_changing_the_partition_column_needs_a_full_refresh(tmp_path):
    ws = project(tmp_path)
    for d in ("2026-03-02", "2026-03-03"):
        day(ws, d)
    write(tmp_path, "nodes/prices.py", DAILY.replace('partition_by="date"', 'partition_by="ticker"'))
    ws.reload()
    finals = run(ws, ["prices"], params={"as_of": "2026-03-04"}, force={"prices"})
    assert finals["prices"]["status"] == "failed" and "full refresh" in finals["prices"]["error"]
    day(ws, "2026-03-04", full_refresh={"prices"})
    m = manifest(ws)
    assert m["partition_by"] == "ticker" and [p["value"] for p in m["partitions"]] == ["A", "B"]
    assert ws.query("select distinct date from prices")["rows"] == [["2026-03-04"]]
