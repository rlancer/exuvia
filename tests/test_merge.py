"""Merging by key (merge=True): a partitioned node's runs update or insert
rows by key into the partitions they hold, and stored rows they don't name
stay."""

import asyncio
import json
import textwrap
from pathlib import Path

import pandas as pd

from exuvia import partitioned as partitions
from exuvia.project import Project
from exuvia.workspace import Workspace

# Returns the rows in data/<node>.json, so a test can change what a run sends.
FEED = '''
import json
import exuvia

@exuvia.node({options})
def {name}():
    import pandas as pd
    rows = json.load(open("data/{name}.json"))
    return pd.DataFrame(rows) if rows else None
'''
MERGE = 'partition_by="date", key=["date", "ticker"], merge=True'


def write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def feed(root: Path, name: str, options: str) -> None:
    write(root, f"nodes/{name}.py", FEED.format(name=name, options=options))


def send(root: Path, name: str, rows: list[dict]) -> None:
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")


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


def run(ws, name, **kw):
    out = go(ws, lambda: ws.run([name], force={name}, **kw))[name]
    return out


def ok(ws, name, **kw):
    out = run(ws, name, **kw)
    assert out["status"] == "success", out["error"]
    return out


def rows(ws, sql):
    return ws.query(sql)["rows"]


def px(day, ticker, close):
    return {"date": day, "ticker": ticker, "close": close}


def test_a_partial_day_merges_in_and_untouched_rows_stay(tmp_path):
    feed(tmp_path, "px", MERGE)
    ws = Workspace(tmp_path)
    send(tmp_path, "px", [px("2026-11-02", "A", 1.0), px("2026-11-03", "A", 2.0), px("2026-11-03", "B", 3.0)])
    first = ok(ws, "px")

    # The afternoon's rows for the 3rd: one new ticker. A and B stay, and so
    # does the 2nd, which the run didn't touch.
    send(tmp_path, "px", [px("2026-11-03", "C", 4.0)])
    out = ok(ws, "px")
    assert out["artifact_id"] != first["artifact_id"]
    assert rows(ws, "select date, ticker, close from px order by 1, 2") == [
        ["2026-11-02", "A", 1.0], ["2026-11-03", "A", 2.0], ["2026-11-03", "B", 3.0], ["2026-11-03", "C", 4.0],
    ]
    assert out["rows"] == 4
    log = ws.store.run(out["id"])["log"]
    assert "merged into the stored rows of 1 partition by key (date, ticker)" in log
    assert "1 partition(s) written (2026-11-03)" in log
    parts = {p["value"]: p["rows"] for p in ws.partitions_of("px")["partitions"]}
    assert parts == {"2026-11-02": 1, "2026-11-03": 3}


def test_a_corrected_row_replaces_its_old_one(tmp_path):
    feed(tmp_path, "px", MERGE)
    ws = Workspace(tmp_path)
    send(tmp_path, "px", [px("2026-11-03", "A", 2.0), px("2026-11-03", "B", 3.0)])
    ok(ws, "px")
    send(tmp_path, "px", [px("2026-11-03", "B", 3.5), px("2026-11-04", "A", 9.0)])
    ok(ws, "px")
    assert rows(ws, "select date, ticker, close from px order by 1, 2") == [
        ["2026-11-03", "A", 2.0], ["2026-11-03", "B", 3.5], ["2026-11-04", "A", 9.0],
    ]


def test_an_identical_merge_writes_nothing_and_does_not_cascade(tmp_path):
    feed(tmp_path, "px", MERGE)
    write(tmp_path, "nodes/total.sql", "select count(*) n, sum(close) s from px\n")
    ws = Workspace(tmp_path)
    send(tmp_path, "px", [px("2026-11-03", "A", 2.0), px("2026-11-03", "B", 3.0), px("2026-11-04", "A", 1.0)])
    first = ok(ws, "px")
    assert ws.status()["total"]["state"] == "fresh"
    total_runs = len(ws.store.runs_for("total"))
    chunks = sorted((tmp_path / ".exuvia" / "artifacts" / "px").iterdir())

    # Part of the 3rd again, unchanged: the merged day is the day stored.
    send(tmp_path, "px", [px("2026-11-03", "B", 3.0)])
    again = ok(ws, "px")
    assert again["artifact_id"] == first["artifact_id"] and again["version"] == first["version"]
    assert "no partition changed" in ws.store.run(again["id"])["log"]
    assert sorted((tmp_path / ".exuvia" / "artifacts" / "px").iterdir()) == chunks
    assert len(ws.store.runs_for("total")) == total_runs
    assert ws.status()["total"]["state"] == "fresh"

    # A real change does cascade.
    send(tmp_path, "px", [px("2026-11-03", "B", 4.0)])
    ok(ws, "px")
    assert len(ws.store.runs_for("total")) == total_runs + 1
    assert ws.preview("total")["rows"] == [[3, 7.0]]


def test_a_merge_into_a_day_inside_a_compacted_year(tmp_path, monkeypatch):
    monkeypatch.setattr(partitions, "COMPACT_AFTER", 4)
    feed(tmp_path, "px", MERGE)
    ws = Workspace(tmp_path)
    days = [f"2025-12-{d:02d}" for d in range(20, 32)] + [f"2026-01-{d:02d}" for d in range(1, 11)]
    send(tmp_path, "px", [px(d, t, float(i)) for i, d in enumerate(days) for t in ("A", "B")])
    backfill = ok(ws, "px")
    m = ws.partitions_of("px")
    assert len(m["files"]) == 2 and m["rows"] == 44  # one file per year
    before = {p["value"]: p["digest"] for p in m["partitions"]}

    # One corrected row and one new row for a day inside 2025's file.
    send(tmp_path, "px", [px("2025-12-24", "B", 100.0), px("2025-12-24", "C", 7.0)])
    out = ok(ws, "px")
    assert out["artifact_id"] != backfill["artifact_id"]
    m = ws.partitions_of("px")
    after = {p["value"]: p["digest"] for p in m["partitions"]}
    assert {v for v in after if before.get(v) != after[v]} == {"2025-12-24"}
    assert any(f["partitions"] == ["2025-12-24"] for f in m["files"])
    assert m["rows"] == 45
    assert rows(ws, "select ticker, close from px where date = '2025-12-24' order by 1") == [
        ["A", 4.0], ["B", 100.0], ["C", 7.0],
    ]
    assert rows(ws, "select count(*) from px") == [[45]]


def test_a_full_refresh_replaces_instead_of_merging(tmp_path):
    feed(tmp_path, "px", MERGE)
    ws = Workspace(tmp_path)
    send(tmp_path, "px", [px("2026-11-02", "A", 1.0), px("2026-11-03", "A", 2.0), px("2026-11-03", "B", 3.0)])
    ok(ws, "px")
    # A delete: B is gone from the 3rd. Merging can't remove it, a full refresh can.
    send(tmp_path, "px", [px("2026-11-03", "A", 2.0)])
    ok(ws, "px")
    assert rows(ws, "select count(*) from px") == [[3]]
    out = ok(ws, "px", full_refresh={"px"})
    assert rows(ws, "select date, ticker from px") == [["2026-11-03", "A"]]
    assert "merged" not in ws.store.run(out["id"])["log"]


def test_on_duplicate_last_applies_before_the_merge(tmp_path):
    feed(tmp_path, "px", MERGE + ', on_duplicate="last"')
    ws = Workspace(tmp_path)
    send(tmp_path, "px", [px("2026-11-03", "A", 1.0), px("2026-11-03", "B", 2.0)])
    ok(ws, "px")
    send(tmp_path, "px", [px("2026-11-03", "B", 5.0), px("2026-11-03", "B", 6.0)])
    out = ok(ws, "px")
    assert rows(ws, "select ticker, close from px order by 1") == [["A", 1.0], ["B", 6.0]]
    assert "dropped 1 row that repeated one" in ws.store.run(out["id"])["log"]


def test_a_merge_cannot_move_a_row_to_another_partition(tmp_path):
    feed(tmp_path, "trades", 'partition_by="date", key="trade_id", merge=True')
    ws = Workspace(tmp_path)
    send(tmp_path, "trades", [{"date": "2026-11-03", "trade_id": 1}, {"date": "2026-11-03", "trade_id": 2}])
    first = ok(ws, "trades")
    for sent in (
        [{"date": "2026-11-04", "trade_id": 2}],  # the 3rd is not touched
        [{"date": "2026-11-03", "trade_id": 1}, {"date": "2026-11-04", "trade_id": 2}],  # it is
    ):
        send(tmp_path, "trades", sent)
        out = run(ws, "trades")
        assert out["status"] == "failed"
        assert "2 " in out["error"] and "A merge never removes a row" in out["error"] and "full refresh" in out["error"]
        assert ws.store.last_success("trades", "")["artifact_id"] == first["artifact_id"]
    ok(ws, "trades", full_refresh={"trades"})
    assert rows(ws, "select date, trade_id from trades order by 2") == [["2026-11-03", 1], ["2026-11-04", 2]]


def test_merge_needs_a_key_and_a_partition_column(tmp_path):
    write(tmp_path, "nodes/nokey.py", '''
        import exuvia

        @exuvia.node(partition_by="date", merge=True)
        def nokey():
            return None
    ''')
    write(tmp_path, "nodes/nopart.py", '''
        import exuvia

        @exuvia.node(key="id", merge=True)
        def nopart():
            return None
    ''')
    write(tmp_path, "nodes/odd.py", '''
        import exuvia

        @exuvia.node(partition_by="date", key="id", merge="sometimes")
        def odd():
            return None
    ''')
    write(tmp_path, "nodes/nokey_sql.sql", "-- partition_by: date\n-- merge: true\nselect 1 a\n")
    g = Project(tmp_path).load()
    assert "declare the `key` too" in g.nodes["nokey"].parse_error
    assert "declare `partition_by` too" in g.nodes["nopart"].parse_error
    assert "`merge` must be True or False" in g.nodes["odd"].parse_error
    assert "declare the `key` too" in g.nodes["nokey_sql"].parse_error


def test_a_sql_node_declares_merge_in_the_header(tmp_path):
    feed(tmp_path, "src", "")
    write(tmp_path, "nodes/inc.sql", """
        -- Merges what src sends.
        -- partition_by: date
        -- key: date, ticker
        -- merge: true
        select * from src
    """)
    write(tmp_path, "nodes/plain.sql", "-- partition_by: date\n-- key: date\n-- merge: false\nselect 1 a\n")
    send(tmp_path, "src", [px("2026-11-03", "A", 1.0)])
    ws = Workspace(tmp_path)
    node = ws.graph.nodes["inc"]
    assert node.merge is True and node.description == "Merges what src sends."
    assert node.to_dict()["merge"] is True
    assert ws.graph.nodes["plain"].merge is False and ws.graph.nodes["plain"].parse_error is None
    ok(ws, "src")
    ok(ws, "inc")
    send(tmp_path, "src", [px("2026-11-03", "B", 2.0)])
    ok(ws, "src")
    ok(ws, "inc")
    assert rows(ws, "select ticker, close from inc order by 1") == [["A", 1.0], ["B", 2.0]]


def test_a_push_node_merges(tmp_path):
    write(tmp_path, "nodes/feed.py", '''
        import exuvia

        @exuvia.node(push=True, partition_by="date", key=["date", "ticker"], merge=True)
        def feed():
            ...
    ''')
    ws = Workspace(tmp_path)

    def push(frame):
        path = tmp_path / "push.parquet"
        frame.to_parquet(path)
        out = go(ws, lambda: ws.push("feed", path, "parquet"))
        assert out["status"] == "success", out["error"]
        return out

    push(pd.DataFrame({"date": ["2026-11-03", "2026-11-03"], "ticker": ["A", "B"], "close": [1.0, 2.0]}))
    second = push(pd.DataFrame({"date": ["2026-11-03"], "ticker": ["B"], "close": [2.5]}))
    assert rows(ws, "select ticker, close from feed order by 1") == [["A", 1.0], ["B", 2.5]]
    same = push(pd.DataFrame({"date": ["2026-11-03"], "ticker": ["A"], "close": [1.0]}))
    assert same["artifact_id"] == second["artifact_id"]
