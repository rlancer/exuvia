import asyncio
import textwrap
from pathlib import Path

import pytest

from exuvia.project import Project, sql_deps
from exuvia.workspace import Workspace


def write(root: Path, name: str, body: str) -> None:
    (root / "nodes").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / name).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


PY = '''
import exuvia

@exuvia.node({kw})
def {name}({params}):
    import pandas as pd
    {body}
'''


def py(root, name, params="", body="return pd.DataFrame({'x': [1, 2]})", kw=""):
    write(root, f"{name}.py", PY.format(name=name, params=params, body=body, kw=kw))


def run(ws, *a, **k):
    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        res = await ws.run(*a, **k)
        # let cascade batches spawned by run() finish
        for _ in range(100):
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.gather(*pending)
        return res

    return asyncio.run(go())


def test_static_parse_and_deps(tmp_path):
    py(tmp_path, "a")
    py(tmp_path, "b", params="a", kw='schedule="*/5 * * * *", cascade=False')
    write(tmp_path, "c.sql", "-- totals\nselect count(*) from b join a using (x)\n")
    g = Project(tmp_path).load()
    assert g.nodes["b"].deps == ["a"]
    assert g.nodes["b"].schedule == "*/5 * * * *"
    assert g.nodes["b"].cascade is False
    assert sorted(g.nodes["c"].deps) == ["a", "b"]
    assert g.nodes["c"].description == "totals"
    assert g.topo_order() == ["a", "b", "c"]


def test_parse_errors_do_not_hide_nodes(tmp_path):
    write(tmp_path, "broken.py", "import exuvia\n@exuvia.node()\ndef broken(:\n")
    py(tmp_path, "orphan", params="missing")
    g = Project(tmp_path).load()
    assert "SyntaxError" in g.nodes["broken"].parse_error
    assert "missing" in g.nodes["orphan"].parse_error


def test_cycle_detected(tmp_path):
    py(tmp_path, "a", params="b")
    py(tmp_path, "b", params="a")
    g = Project(tmp_path).load()
    assert g.nodes["a"].parse_error and "cycle" in g.nodes["a"].parse_error.lower()


def test_sql_deps_ignore_strings_and_comments():
    assert sql_deps("select 'prices' -- returns\n from universe", {"prices", "returns", "universe"}) == ["universe"]


def test_run_pulls_stale_upstream_and_becomes_fresh(tmp_path):
    py(tmp_path, "a")
    py(tmp_path, "b", params="a", body="return a.assign(y=a.x * 2)")
    ws = Workspace(tmp_path)
    finals = run(ws, ["b"])
    assert {n: r["status"] for n, r in finals.items()} == {"a": "success", "b": "success"}
    assert ws.status()["b"]["state"] == "fresh"
    assert run(ws, ["b"]) == {}  # nothing to do


def test_code_change_marks_downstream_stale(tmp_path):
    py(tmp_path, "a")
    py(tmp_path, "b", params="a", body="return a")
    ws = Workspace(tmp_path)
    run(ws, ["b"])
    py(tmp_path, "a", body="return pd.DataFrame({'x': [9]})")
    ws.reload()
    st = ws.status()
    assert st["a"]["state"] == "stale" and st["a"]["reason"] == "code changed"
    assert st["b"]["state"] == "stale"


def test_cascade_on_changed_output_and_stop_on_same(tmp_path):
    # `a` reads a counter file so a forced re-run can change its output
    (tmp_path / "n.txt").write_text("1")
    py(tmp_path, "a", body="return pd.DataFrame({'x': [int(open('n.txt').read())]})")
    py(tmp_path, "b", params="a", body="return a")
    py(tmp_path, "c", params="a", body="return a", kw="cascade=False")
    ws = Workspace(tmp_path)
    run(ws, ["b", "c"])
    b_runs = len(ws.store.runs_for("b"))

    run(ws, ["a"], force={"a"})  # same output -> no cascade
    assert len(ws.store.runs_for("b")) == b_runs

    (tmp_path / "n.txt").write_text("2")
    run(ws, ["a"], force={"a"})  # new output -> b cascades, c (cascade=False) does not
    assert ws.store.runs_for("b")[0]["trigger"] == "cascade"
    st = ws.status()
    assert st["b"]["state"] == "fresh"
    assert st["c"]["state"] == "stale"


def test_failure_skips_downstream_and_reports_error(tmp_path):
    py(tmp_path, "a", body="raise ValueError('boom')")
    py(tmp_path, "b", params="a", body="return a")
    ws = Workspace(tmp_path)
    finals = run(ws, ["b"])
    assert finals["a"]["status"] == "failed" and "boom" in finals["a"]["error"]
    assert finals["b"]["status"] == "skipped"
    assert "boom" in ws.store.run(finals["a"]["id"])["log"]
    assert ws.status()["a"]["state"] == "failed"


def test_failed_upstream_with_a_good_output_does_not_block_downstream(tmp_path):
    # `src` is a slow source that fetches (here: reads a file) on its own
    # schedule; `fast` runs often and reads it. When a refresh of `src`
    # fails, `fast` keeps reading the last good output instead of retrying
    # `src` on every run and being skipped.
    (tmp_path / "feed.txt").write_text("1")
    py(tmp_path, "src", body="v = open('feed.txt').read()\n    assert v != 'down', 'feed is down'\n    return pd.DataFrame({'x': [int(v)]})")
    py(tmp_path, "fast", params="src", body="return src.assign(y=src.x + 1)", kw="cascade=False")
    ws = Workspace(tmp_path)
    run(ws, ["fast"])
    good = ws.status()["src"]["artifact_id"]

    (tmp_path / "feed.txt").write_text("down")
    assert run(ws, ["src"], force={"src"})["src"]["status"] == "failed"  # its scheduled refresh
    st = ws.status()
    assert st["src"]["state"] == "failed" and st["src"]["output_current"]
    assert st["fast"]["state"] == "fresh"

    finals = run(ws, ["fast"], force={"fast"})  # fast's scheduled run
    assert list(finals) == ["fast"] and finals["fast"]["status"] == "success"
    assert finals["fast"]["inputs"]["src"] == good

    # Run by name (forced, as the workbench and its schedule run it), it
    # still retries, and recovers once the feed is back.
    assert run(ws, ["src"], force={"src"})["src"]["status"] == "failed"
    (tmp_path / "feed.txt").write_text("2")
    assert run(ws, ["src"], force={"src"})["src"]["status"] == "success"
    assert ws.status()["src"]["state"] == "fresh"


def test_failed_upstream_whose_output_is_out_of_date_still_runs_first(tmp_path):
    py(tmp_path, "src")
    py(tmp_path, "fast", params="src", body="return src")
    ws = Workspace(tmp_path)
    run(ws, ["fast"])
    py(tmp_path, "src", body="raise ValueError('boom')")  # new code, so the old output is no longer current
    ws.reload()
    run(ws, ["src"])
    st = ws.status()
    assert st["src"]["state"] == "failed" and not st["src"]["output_current"]
    finals = run(ws, ["fast"], force={"fast"})
    assert finals["src"]["status"] == "failed" and finals["fast"]["status"] == "skipped"


def test_a_failed_node_retries_when_run_unforced_instead_of_reading_as_cached(tmp_path):
    # "Refresh everything stale" (POST /api/run-stale, `exuvia run`) runs
    # failed nodes without forcing them. A failed node whose last good output
    # is still current must run again, not finish as "cached" and drop its
    # failure without having been retried.
    (tmp_path / "feed.txt").write_text("1")
    py(tmp_path, "src", body="v = open('feed.txt').read()\n    assert v != 'down', 'feed is down'\n    return pd.DataFrame({'x': [int(v)]})")
    ws = Workspace(tmp_path)
    run(ws, ["src"])
    (tmp_path / "feed.txt").write_text("down")
    run(ws, ["src"], force={"src"})
    assert ws.status()["src"]["state"] == "failed"

    finals = run(ws, ["src"])  # still down: it tries, and stays failed
    assert finals["src"]["status"] == "failed"
    assert ws.status()["src"]["state"] == "failed"

    (tmp_path / "feed.txt").write_text("2")
    assert run(ws, ["src"])["src"]["status"] == "success"
    assert ws.status()["src"]["state"] == "fresh"


def test_a_failure_stands_through_runs_that_did_not_try(tmp_path):
    # A node that failed, then was skipped because its upstream failed, still
    # reads as failed: the skipped run never ran its code.
    (tmp_path / "flag.txt").write_text("ok")
    py(tmp_path, "a", body="assert open('flag.txt').read() == 'ok', 'a is down'\n    return pd.DataFrame({'x': [1]})")
    py(tmp_path, "b", params="a", body="raise ValueError('b is broken')")
    ws = Workspace(tmp_path)
    run(ws, ["b"])
    assert ws.status()["b"]["state"] == "failed"
    (tmp_path / "flag.txt").write_text("down")
    finals = run(ws, ["a", "b"], force={"a", "b"})
    assert finals["a"]["status"] == "failed" and finals["b"]["status"] == "skipped"
    st = ws.status()["b"]
    assert st["state"] == "failed" and "b is broken" in st["reason"]
    assert st["last_run"]["status"] == "skipped"  # the latest run is still what it was


def test_sql_node_and_query(tmp_path):
    py(tmp_path, "a", body="return pd.DataFrame({'x': [1, 2, 3]})")
    write(tmp_path, "total.sql", "select sum(x) as s from a\n")
    ws = Workspace(tmp_path)
    run(ws, ["total"])
    assert ws.preview("total")["rows"] == [[6]]
    assert ws.query("select count(*) from a join total on true")["rows"] == [[3]]


def test_json_output(tmp_path):
    py(tmp_path, "cfg", body="return {'window': 20}")
    ws = Workspace(tmp_path)
    run(ws, ["cfg"])
    out = ws.preview("cfg")
    assert out["kind"] == "json" and out["value"] == {"window": 20}
    assert out["run_id"] and out["run_params"] == {}


@pytest.mark.parametrize("cron,ok", [("0 17 * * 1-5", True), ("nope", False)])
def test_schedule_validation(tmp_path, cron, ok):
    py(tmp_path, "a", kw=f'schedule="{cron}"')
    err = Project(tmp_path).load().nodes["a"].parse_error
    assert (err is None) == ok


def test_non_table_outputs_are_json_never_pickle(tmp_path):
    body = (
        "import datetime, decimal, numpy as np\n"
        "    return {'n': np.int64(3), 'v': np.array([1.5, 2.5]), 'day': datetime.date(2026, 3, 2), "
        "'d': decimal.Decimal('1.25'), 'tags': {'b', 'a'}}"
    )
    py(tmp_path, "summary", body=body)
    py(tmp_path, "uses", params="summary", body="return pd.DataFrame({'n': [summary['n']], 'day': [summary['day']]})")
    py(tmp_path, "model", body="class Fit: pass\n    return Fit()")
    ws = Workspace(tmp_path)
    finals = run(ws, ["uses", "model"])
    assert finals["summary"]["status"] == "success" and finals["uses"]["status"] == "success"
    assert ws.preview("summary")["value"] == {"n": 3, "v": [1.5, 2.5], "day": "2026-03-02", "d": 1.25, "tags": ["a", "b"]}
    assert ws.preview("uses")["rows"] == [[3, "2026-03-02"]]
    # Anything else fails clearly instead of being pickled or stringified.
    assert finals["model"]["status"] == "failed"
    assert "must return a DataFrame" in finals["model"]["error"] and "Fit" in finals["model"]["error"]
    assert not list(tmp_path.rglob("*.pkl"))


def test_a_pickled_output_from_an_older_version_is_rerun_not_read(tmp_path):
    py(tmp_path, "a", body="return {'x': 1}")
    py(tmp_path, "b", params="a", body="return pd.DataFrame({'x': [a['x']]})")
    ws = Workspace(tmp_path)
    run(ws, ["b"])
    # Rewrite `a`'s output as an old pickle row: never loaded, and stale.
    ls = ws.store.last_success("a")
    ws.store.update_run(ls["id"], kind="pickle", artifact_path=".exuvia/artifacts/a/old.pkl")
    st = ws.status()
    assert st["a"]["state"] == "stale" and "pickle" in st["a"]["reason"]
    assert st["b"]["state"] == "stale"
    assert ws.preview("a")["kind"] == "none"
    finals = run(ws, ["b"])
    assert finals["a"]["status"] == "success" and ws.status()["b"]["state"] == "fresh"


def test_table_outputs_use_the_current_parquet_format(tmp_path):
    import pyarrow.parquet as pq

    py(tmp_path, "a")
    ws = Workspace(tmp_path)
    run(ws, ["a"])
    [path] = (tmp_path / ".exuvia" / "artifacts" / "a").glob("*.parquet")
    f = pq.ParquetFile(path)
    col = f.metadata.row_group(0).column(0)
    assert f.metadata.format_version == "2.6"
    assert col.compression == "ZSTD"
    assert col.has_column_index and col.has_offset_index
    assert any(enc in col.encodings for enc in ("RLE_DICTIONARY", "PLAIN"))
    # Data page v2 is what DuckDB (used by previews and SQL nodes) reads back too.
    assert ws.preview("a")["rows"] == [[1], [2]]
