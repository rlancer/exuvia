"""Run parameters: per-value outputs, as-of replays, backfills, history views."""

import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from exuvia.project import Project
from exuvia.workspace import Workspace, params_key


def write(root: Path, name: str, body: str) -> None:
    (root / "nodes").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / name).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def config(root: Path, **cfg) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "exuvia.json").write_text(json.dumps(cfg), encoding="utf-8")


AS_OF = {"params": {"as_of": {"type": "date", "default": "today"}}}

# `universe` depends on no parameter; `prices` reads as_of; `signal` inherits it.
UNIVERSE = '''
    import exuvia

    @exuvia.node()
    def universe():
        import pandas as pd
        return pd.DataFrame({"ticker": ["A", "B"]})
'''
PRICES = '''
    import exuvia

    @exuvia.node(params=["as_of"])
    def prices(universe):
        day = exuvia.param("as_of")
        return universe.assign(as_of=day, px=int(day[-2:]))
'''
SIGNAL = "-- doubles the price\nselect ticker, px * 2 as sig from prices\n"


def project(root: Path) -> Workspace:
    config(root, **AS_OF)
    write(root, "universe.py", UNIVERSE)
    write(root, "prices.py", PRICES)
    write(root, "signal.sql", SIGNAL)
    return Workspace(root)


def go(ws, coro_fn):
    async def main():
        ws.bus.bind(asyncio.get_running_loop())
        res = await coro_fn()
        for _ in range(200):  # let cascades and backfills finish
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
        return res

    return asyncio.run(main())


def run(ws, *a, **k):
    return go(ws, lambda: ws.run(*a, **k))


def test_declared_params_parse_and_propagate(tmp_path):
    ws = project(tmp_path)
    g = ws.graph
    assert g.nodes["prices"].params == ["as_of"]
    assert g.effective_params("universe") == []
    assert g.effective_params("signal") == ["as_of"]  # inherited from prices


def test_sql_header_declares_params(tmp_path):
    config(tmp_path, **AS_OF)
    write(tmp_path, "day.sql", "-- one row\n-- params: as_of\nselect $as_of as d\n")
    ws = Workspace(tmp_path)
    assert ws.graph.nodes["day"].params == ["as_of"]
    run(ws, ["day"], params={"as_of": "2026-03-02"})
    assert ws.preview("day", params={"as_of": "2026-03-02"})["rows"] == [["2026-03-02"]]


def test_undeclared_project_param_is_a_parse_error(tmp_path):
    write(tmp_path, "a.py", PRICES.replace("def prices(universe)", "def a()"))
    err = Project(tmp_path).load().nodes["a"].parse_error
    assert err and "as_of" in err


def test_today_default_and_validation(tmp_path):
    ws = project(tmp_path)
    assert ws.resolve_params() == {"as_of": ws.project.today()}
    assert ws.resolve_params({"as_of": "2026-03-02"}) == {"as_of": "2026-03-02"}
    with pytest.raises(ValueError, match="expects a date"):
        ws.resolve_params({"as_of": "yesterday-ish"})
    with pytest.raises(ValueError, match="unknown parameter"):
        ws.resolve_params({"nope": 1})


def test_outputs_are_kept_per_value_and_replays_are_cached(tmp_path):
    ws = project(tmp_path)
    d1, d2 = {"as_of": "2026-03-02"}, {"as_of": "2026-03-05"}

    first = run(ws, ["signal"], params=d1)
    assert {n: r["status"] for n, r in first.items()} == {"universe": "success", "prices": "success", "signal": "success"}

    second = run(ws, ["signal"], params=d2)
    assert set(second) == {"prices", "signal"}  # universe has no parameters: shared, not re-run

    # Each date keeps its own output; neither overwrote the other.
    assert ws.preview("signal", params=d1)["rows"] == [["A", 4], ["B", 4]]
    assert ws.preview("signal", params=d2)["rows"] == [["A", 10], ["B", 10]]
    assert ws.status(d1)["signal"]["state"] == "fresh"
    assert ws.status(d2)["signal"]["run_params"] == d2

    assert run(ws, ["signal"], params=d1) == {}  # replaying a date is a cache hit
    assert ws.status({"as_of": "2026-03-09"})["prices"]["state"] == "new"


def test_reading_an_undeclared_param_fails_the_run(tmp_path):
    config(tmp_path, **AS_OF)
    write(tmp_path, "sneaky.py", PRICES.replace('params=["as_of"]', "").replace("def prices(universe)", "def sneaky()"))
    ws = Workspace(tmp_path)
    finals = run(ws, ["sneaky"])
    assert finals["sneaky"]["status"] == "failed"
    assert "without declaring it" in finals["sneaky"]["error"]


def test_code_change_makes_every_date_stale(tmp_path):
    ws = project(tmp_path)
    d1 = {"as_of": "2026-03-02"}
    run(ws, ["signal"], params=d1)
    write(tmp_path, "signal.sql", "select ticker, px * 3 as sig from prices\n")
    ws.reload()
    assert ws.status(d1)["signal"]["reason"] == "code changed"
    run(ws, ["signal"], params=d1)
    assert ws.preview("signal", params=d1)["rows"] == [["A", 6], ["B", 6]]


def test_history_and_runs_view_compare_dates(tmp_path):
    ws = project(tmp_path)
    for day in ("2026-03-02", "2026-03-03", "2026-03-04"):
        run(ws, ["signal"], params={"as_of": day})

    hist = ws.history("signal")
    assert [h["run_params"]["as_of"] for h in hist] == ["2026-03-04", "2026-03-03", "2026-03-02"]
    assert all(h["code_current"] for h in hist)
    assert ws.history("universe")[0]["run_params"] == {}
    assert ws.param_values() == {"as_of": ["2026-03-04", "2026-03-03", "2026-03-02"]}

    # One specific historical run can be opened directly.
    assert ws.preview("signal", run_id=hist[-1]["run_id"])["rows"] == [["A", 4], ["B", 4]]

    out = ws.query('select "_as_of", sum(sig) as s from signal__runs group by 1 order by 1')
    assert out["rows"] == [["2026-03-02", 8], ["2026-03-03", 12], ["2026-03-04", 16]]
    # The plain view is one parameter set.
    assert ws.query("select sum(sig) from signal", params={"as_of": "2026-03-03"})["rows"] == [[12]]


def test_backfill_replays_each_value_in_order(tmp_path):
    ws = project(tmp_path)
    days = ["2026-03-02", "2026-03-03", "2026-03-04"]
    events: list[dict] = []

    async def backfill():
        q = ws.bus.subscribe()
        started = ws.start_backfill("as_of", days + [days[0]])  # duplicates collapse
        assert started["values"] == days
        with pytest.raises(RuntimeError, match="already running"):
            ws.start_backfill("as_of", days)
        while ws.busy:
            await asyncio.sleep(0.05)
        while not q.empty():
            events.append(q.get_nowait())

    go(ws, backfill)
    status = ws.backfill_status()
    assert status["state"] == "done" and status["done"] == 3 and status["failed"] == []
    progress = [e["backfill"]["current"] for e in events if e["type"] == "backfill"]
    assert [c for c in progress if c] == days  # one value at a time, in order
    assert {h["run_params"]["as_of"] for h in ws.history("signal")} == set(days)
    assert ws.store.runs_for("signal")[0]["trigger"] == "backfill"


def test_backfill_rejects_bad_input(tmp_path):
    ws = project(tmp_path)

    async def bad():
        with pytest.raises(ValueError, match="unknown parameter"):
            ws.start_backfill("nope", ["x"])
        with pytest.raises(ValueError, match="expects a date"):
            ws.start_backfill("as_of", ["not-a-date"])

    go(ws, bad)
    assert ws.backfill_status() is None


def test_artifacts_survive_moving_the_project(tmp_path):
    import shutil

    src = tmp_path / "a"
    ws = project(src)
    run(ws, ["signal"], params={"as_of": "2026-03-02"})
    stored = ws.store.last_success("signal", ws.node_params("signal", {"as_of": "2026-03-02"})[1])
    assert not Path(stored["artifact_path"]).is_absolute()  # relative to the project root
    ws.store.close()

    dst = tmp_path / "moved"
    shutil.copytree(src, dst)
    shutil.rmtree(src / ".exuvia" / "artifacts")  # the old location is gone
    moved = Workspace(dst)
    assert moved.preview("signal", params={"as_of": "2026-03-02"})["rows"] == [["A", 4], ["B", 4]]
    assert moved.status({"as_of": "2026-03-02"})["signal"]["state"] == "fresh"


def test_params_key_is_stable_and_order_independent():
    assert params_key({}) == ""
    assert params_key({"a": 1, "b": "x"}) == params_key({"b": "x", "a": 1})
    assert params_key({"a": 1}) != params_key({"a": 2})
