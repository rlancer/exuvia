"""Unique keys (key=, on_duplicate=): every output exuvia would store holds
its node's key, or the run fails and the previous output stays."""

import asyncio
import json
import textwrap
from pathlib import Path

import pandas as pd
import pytest

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


def run(ws, name):
    return go(ws, lambda: ws.run([name], force={name}, cascade=False))[name]


def rows(ws, sql):
    return ws.query(sql)["rows"]


def test_repeated_keys_fail_the_run_and_keep_the_output(tmp_path):
    feed(tmp_path, "px", 'key=["date", "ticker"]')
    write(tmp_path, "nodes/total.sql", "select count(*) n from px\n")
    send(tmp_path, "px", [{"date": "2026-01-02", "ticker": "A", "close": 1.0}])
    ws = Workspace(tmp_path)
    assert run(ws, "px")["status"] == "success"
    first = ws.store.last_success("px", "")["artifact_id"]

    send(tmp_path, "px", [
        {"date": "2026-01-02", "ticker": "A", "close": 1.0},
        {"date": "2026-01-02", "ticker": "A", "close": 1.5},
        {"date": "2026-01-02", "ticker": "B", "close": 2.0},
        {"date": "2026-01-02", "ticker": "B", "close": 2.0},
        {"date": "2026-01-02", "ticker": "B", "close": 2.0},
    ])
    out = run(ws, "px")
    assert out["status"] == "failed"
    assert "3 rows repeat a key (date, ticker)" in out["error"]
    assert "(2026-01-02, B) ×3" in out["error"] and "(2026-01-02, A) ×2" in out["error"]
    assert "on_duplicate" in out["error"]
    assert ws.store.last_success("px", "")["artifact_id"] == first  # the previous output stays
    assert rows(ws, "select count(*) from px") == [[1]]


def test_nulls_and_missing_key_columns_fail(tmp_path):
    feed(tmp_path, "px", 'key=["date", "ticker"], on_duplicate="last"')
    ws = Workspace(tmp_path)
    send(tmp_path, "px", [{"date": "2026-01-02", "ticker": None}, {"date": "2026-01-02", "ticker": "A"}])
    out = run(ws, "px")
    assert out["status"] == "failed" and "1 row in the table the node returned has no `ticker`" in out["error"]
    send(tmp_path, "px", [{"date": "2026-01-02", "close": 1}])
    out = run(ws, "px")
    assert out["status"] == "failed" and "has no `ticker` column" in out["error"]


def test_on_duplicate_last_keeps_the_last_row_per_key(tmp_path):
    feed(tmp_path, "px", 'key=["date", "ticker"], on_duplicate="last"')
    send(tmp_path, "px", [
        {"date": "2026-01-02", "ticker": "B", "close": 1.0},
        {"date": "2026-01-02", "ticker": "A", "close": 1.0},
        {"date": "2026-01-02", "ticker": "B", "close": 2.0},
        {"date": "2026-01-03", "ticker": "A", "close": 3.0},
        {"date": "2026-01-02", "ticker": "B", "close": 9.0},
    ])
    ws = Workspace(tmp_path)
    out = run(ws, "px")
    assert out["status"] == "success", out["error"]
    assert out["rows"] == 3
    # The last row for each key, in the order the rows came.
    assert rows(ws, "select date, ticker, close from px") == [
        ["2026-01-02", "A", 1.0], ["2026-01-03", "A", 3.0], ["2026-01-02", "B", 9.0]
    ]
    assert "dropped 2 rows that repeated one" in ws.store.run(out["id"])["log"]


def test_a_sql_node_declares_its_key_in_the_header(tmp_path):
    write(tmp_path, "nodes/pairs.sql", """
        -- Pairs, one of them twice.
        -- key: a, b
        select * from (values (1, 'x'), (1, 'y'), (1, 'x')) t(a, b)
    """)
    ws = Workspace(tmp_path)
    assert ws.graph.nodes["pairs"].key == ["a", "b"]
    assert ws.graph.nodes["pairs"].description == "Pairs, one of them twice."
    out = run(ws, "pairs")
    assert out["status"] == "failed" and "(1, x) ×2" in out["error"]


def test_a_key_needs_one_table(tmp_path):
    write(tmp_path, "nodes/meta.py", '''
        import exuvia

        @exuvia.node(key="id")
        def meta():
            return {"id": 1}
    ''')
    ws = Workspace(tmp_path)
    out = run(ws, "meta")
    assert out["status"] == "failed" and "its output is json, not one table" in out["error"]


def test_node_options_are_checked(tmp_path):
    write(tmp_path, "nodes/bad.py", '''
        import exuvia

        @exuvia.node(on_duplicate="last")
        def bad():
            return None
    ''')
    write(tmp_path, "nodes/worse.py", '''
        import exuvia

        @exuvia.node(key=["a", "a"])
        def worse():
            return None
    ''')
    write(tmp_path, "nodes/odd.sql", "-- key: a\n-- on_duplicate: first\nselect 1 a\n")
    write(tmp_path, "nodes/one.py", '''
        import exuvia

        @exuvia.node(key="id")
        def one():
            return None
    ''')
    p = Project(tmp_path)
    g = p.load()
    assert "declare the `key` too" in g.nodes["bad"].parse_error
    assert "each column once" in g.nodes["worse"].parse_error
    assert '"error" (the default) or "last"' in g.nodes["odd"].parse_error
    assert g.nodes["one"].key == ["id"] and g.nodes["one"].parse_error is None


# -- partitioned nodes: the key holds across the whole output ------------------
def test_a_partitioned_key_holds_across_partitions(tmp_path):
    feed(tmp_path, "trades", 'partition_by="date", key="trade_id"')
    write(tmp_path, "nodes/book.sql", "select count(*) n, sum(qty) qty from trades\n")
    ws = Workspace(tmp_path)
    send(tmp_path, "trades", [
        {"date": "2026-11-03", "trade_id": 1, "qty": 10},
        {"date": "2026-11-03", "trade_id": 2, "qty": 20},
    ])
    first = run(ws, "trades")
    assert first["status"] == "success", first["error"]

    # Trade 2 is re-dated to the 4th, and only the 4th is sent: the old copy
    # would stay in the 3rd, so the run fails and the output stays.
    send(tmp_path, "trades", [{"date": "2026-11-04", "trade_id": 2, "qty": 20}])
    out = run(ws, "trades")
    assert out["status"] == "failed"
    assert "trade_id" in out["error"] and "2 returned in 2026-11-04 and stored in 2026-11-03" in out["error"]
    assert ws.store.last_success("trades", "")["artifact_id"] == first["artifact_id"]
    assert rows(ws, "select count(*) from trades") == [[2]]
    # Nothing the failed commit wrote is left behind.
    chunks = {p.name for p in (tmp_path / ".exuvia" / "artifacts" / "trades").glob("*.parquet")}
    assert chunks == {Path(f["file"]).name for f in ws.store.manifest_files("trades", "", first["version"])}

    # Sending the 3rd without it as well moves the trade.
    send(tmp_path, "trades", [
        {"date": "2026-11-03", "trade_id": 1, "qty": 10},
        {"date": "2026-11-04", "trade_id": 2, "qty": 20},
    ])
    out = run(ws, "trades")
    assert out["status"] == "success", out["error"]
    assert rows(ws, "select date, trade_id from trades order by 2") == [["2026-11-03", 1], ["2026-11-04", 2]]


def test_partitioned_rows_repeating_a_key(tmp_path):
    feed(tmp_path, "px", 'partition_by="date", key=["date", "ticker"]')
    ws = Workspace(tmp_path)
    send(tmp_path, "px", [{"date": "2026-01-02", "ticker": "A", "v": 1}, {"date": "2026-01-02", "ticker": "A", "v": 2}])
    out = run(ws, "px")
    assert out["status"] == "failed" and "1 row repeats a key (date, ticker)" in out["error"]
    assert ws.store.last_success("px", "") is None

    feed(tmp_path, "px", 'partition_by="date", key=["date", "ticker"], on_duplicate="last"')
    ws.reload()
    out = run(ws, "px")
    assert out["status"] == "success", out["error"]
    assert rows(ws, "select ticker, v from px") == [["A", 2]]
    assert "dropped 1 row that repeated one" in ws.store.run(out["id"])["log"]


def test_a_key_declared_later_checks_what_is_stored(tmp_path):
    feed(tmp_path, "px", 'partition_by="date"')
    ws = Workspace(tmp_path)
    send(tmp_path, "px", [{"date": "2026-01-02", "ticker": "A"}, {"date": "2026-01-02", "ticker": "A"}])
    assert run(ws, "px")["status"] == "success"
    # The key comes later, and the next run sends the same rows (no change):
    # the stored output still breaks it, so the run says so.
    feed(tmp_path, "px", 'partition_by="date", key=["date", "ticker"]')
    send(tmp_path, "px", [])
    ws.reload()
    out = run(ws, "px")
    assert out["status"] == "failed" and "(2026-01-02, A) ×2 in 2026-01-02" in out["error"]


def test_pushed_outputs_hold_the_key(tmp_path):
    write(tmp_path, "nodes/feed.py", '''
        import exuvia

        @exuvia.node(push=True, key="id")
        def feed():
            ...
    ''')
    ws = Workspace(tmp_path)

    def push(frame):
        path = tmp_path / "push.parquet"
        frame.to_parquet(path)
        return go(ws, lambda: ws.push("feed", path, "parquet"))

    assert push(pd.DataFrame({"id": [1, 2]}))["status"] == "success"
    bad = push(pd.DataFrame({"id": [1, 1]}))
    assert bad["status"] == "failed" and "KeyViolation" in bad["error"]
    assert rows(ws, "select count(*) from feed") == [[2]]


@pytest.mark.parametrize("on_duplicate", ["error", "last"])
def test_the_key_shows_in_the_project(tmp_path, on_duplicate):
    feed(tmp_path, "px", f'key=["date", "ticker"], on_duplicate="{on_duplicate}"')
    d = Workspace(tmp_path).graph.nodes["px"].to_dict()
    assert d["key"] == ["date", "ticker"] and d["on_duplicate"] == on_duplicate


def test_a_changed_key_checks_the_whole_output_again(tmp_path):
    feed(tmp_path, "px", 'partition_by="date", key=["date", "ticker"]')
    ws = Workspace(tmp_path)
    for day in ("2026-01-02", "2026-01-05"):
        send(tmp_path, "px", [{"date": day, "ticker": "A"}])
        assert run(ws, "px")["status"] == "success"
    # ticker alone repeats across the two days already stored.
    feed(tmp_path, "px", 'partition_by="date", key="ticker"')
    send(tmp_path, "px", [])
    ws.reload()
    out = run(ws, "px")
    assert out["status"] == "failed" and "A ×2 in 2026-01-02, 2026-01-05" in out["error"]
