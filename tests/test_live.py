"""Live nodes: re-run when a view that depends on them is opened, while the
view shows the output it has and updates when the new one lands."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from exuvia.project import Project
from exuvia.server import _needed_role, create_app
from exuvia.workspace import Workspace

from test_engine import py, run, write

# Each run of `ticks` returns a new value, as a quote feed would.
TICKS = "import time\n    return pd.DataFrame({'t': [time.time_ns()]})"


def view(root, name="board", reads=("ticks",)):
    d = root / "views" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "view.json").write_text(json.dumps({"reads": list(reads)}), encoding="utf-8")
    (d / "App.tsx").write_text("export default () => null;\n", encoding="utf-8")


def refresh(ws, name="board", params=None):
    """Start a refresh and wait for the runs it started (and their cascades)."""

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        res = ws.refresh_view(name, params)
        for _ in range(100):
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.gather(*pending)
        return res

    return asyncio.run(go())


def output(ws, name):
    return ws.status()[name]["artifact_id"]


@pytest.mark.parametrize(
    ("kw", "want"),
    [("live=True", 0.0), ("live=False", None), ("live=300", 300.0), ("", None)],
)
def test_python_live_option(tmp_path, kw, want):
    py(tmp_path, "ticks", kw=kw)
    node = Project(tmp_path).load().nodes["ticks"]
    assert node.parse_error is None
    assert node.live == want


@pytest.mark.parametrize(("header", "want"), [("true", 0.0), ("60", 60.0), ("false", None)])
def test_sql_live_header(tmp_path, header, want):
    write(tmp_path, "q.sql", f"-- quotes\n-- live: {header}\nselect 1 as x\n")
    node = Project(tmp_path).load().nodes["q"]
    assert (node.parse_error, node.live, node.description) == (None, want, "quotes")


@pytest.mark.parametrize("bad", ["live=-1", "live='often'", "live=1000000000"])
def test_a_bad_live_value_is_a_parse_error(tmp_path, bad):
    py(tmp_path, "ticks", kw=bad)
    assert "`live` must be" in Project(tmp_path).load().nodes["ticks"].parse_error


def test_a_push_node_cannot_be_live(tmp_path):
    py(tmp_path, "desk", kw="push=True, live=True")
    assert "can't be live" in Project(tmp_path).load().nodes["desk"].parse_error


def test_a_view_lists_the_live_nodes_it_depends_on(tmp_path):
    py(tmp_path, "ticks", body=TICKS, kw="live=True")
    py(tmp_path, "ref")
    py(tmp_path, "joined", params="ticks, ref")
    view(tmp_path, "board", reads=("joined",))
    view(tmp_path, "static", reads=("ref",))
    ws = Workspace(tmp_path)
    views = {v["name"]: v for v in ws.views()}
    assert views["board"]["live"] == ["ticks"]  # upstream of what it reads
    assert views["static"]["live"] == []


def test_opening_a_view_reruns_its_live_nodes_and_what_reads_them(tmp_path):
    py(tmp_path, "ticks", body=TICKS, kw="live=True")
    py(tmp_path, "ref")
    py(tmp_path, "joined", params="ticks, ref", body="return ticks.assign(n=len(ref))")
    view(tmp_path, "board", reads=("joined",))
    ws = Workspace(tmp_path)
    run(ws, ["joined"])
    before = {n: output(ws, n) for n in ("ticks", "ref", "joined")}

    res = refresh(ws)
    assert (res["live"], res["started"], res["skipped"]) == (["ticks"], ["ticks"], {})
    # The live node made a new output and what the view reads followed it;
    # the node that is not live was left alone.
    assert output(ws, "ticks") != before["ticks"]
    assert output(ws, "joined") != before["joined"]
    assert output(ws, "ref") == before["ref"]
    assert ws.store.last_run("ticks", "")["trigger"] == "live"
    assert {v["name"]: v["state"] for v in ws.views()}["board"] == "ready"


def test_a_view_with_no_output_yet_gets_one(tmp_path):
    py(tmp_path, "ticks", body=TICKS, kw="live=True")
    view(tmp_path)
    ws = Workspace(tmp_path)
    assert ws.views()[0]["state"] == "missing"
    refresh(ws)
    assert ws.views()[0]["state"] == "ready"


def test_same_output_leaves_the_readers_cached(tmp_path):
    py(tmp_path, "steady", kw="live=True")
    py(tmp_path, "reader", params="steady")
    view(tmp_path, reads=("reader",))
    ws = Workspace(tmp_path)
    run(ws, ["reader"])
    refresh(ws)
    assert ws.store.last_run("steady", "")["status"] == "success"
    assert ws.store.last_run("reader", "")["status"] == "cached"


def test_live_seconds_skip_a_recent_output(tmp_path):
    py(tmp_path, "ticks", body=TICKS, kw="live=3600")
    view(tmp_path)
    ws = Workspace(tmp_path)
    run(ws, ["ticks"])
    first = output(ws, "ticks")
    res = refresh(ws)
    assert res["started"] == [] and res["skipped"]["ticks"].startswith("refreshed")
    assert output(ws, "ticks") == first


def test_a_refresh_does_not_start_a_second_run_of_the_same_output(tmp_path):
    py(tmp_path, "ticks", body="import time\n    time.sleep(0.5)\n    return pd.DataFrame({'t': [time.time_ns()]})", kw="live=True")
    view(tmp_path)
    ws = Workspace(tmp_path)

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        a = ws.refresh_view("board")
        b = ws.refresh_view("board")  # before the first one's run has started
        await asyncio.sleep(0.05)
        c = ws.refresh_view("board")  # while it runs
        for _ in range(100):
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.gather(*pending)
        return a, b, c

    a, b, c = asyncio.run(go())
    assert a["started"] == ["ticks"]
    assert b["skipped"] == {"ticks": "already running"}
    assert c["skipped"] == {"ticks": "already running"}
    assert len(ws.store.runs_for("ticks")) == 1


def test_a_view_without_live_nodes_runs_nothing(tmp_path):
    py(tmp_path, "ticks", body=TICKS)
    view(tmp_path)
    ws = Workspace(tmp_path)
    assert refresh(ws) == {"live": [], "started": [], "skipped": {}, "params": {}}
    assert ws.store.runs_for("ticks") == []


def test_refresh_route(tmp_path):
    py(tmp_path, "ticks", body=TICKS, kw="live=True")
    view(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        res = client.post("/api/views/board/refresh", json={})
        assert res.status_code == 200 and res.json()["started"] == ["ticks"]
        assert client.get("/api/views").json()["views"][0]["live"] == ["ticks"]
        assert client.post("/api/views/nope/refresh", json={}).status_code == 404
        for _ in range(100):
            if client.get("/api/views").json()["views"][0]["state"] == "ready":
                break
            time.sleep(0.05)
        assert client.get("/api/views").json()["views"][0]["state"] == "ready"


def test_viewers_may_refresh_a_view():
    class Req:
        def __init__(self, method, path):
            self.method = method
            self.url = type("U", (), {"path": path})()

    assert _needed_role(Req("POST", "/p/x/api/views/board/refresh")) == "viewer"
    assert _needed_role(Req("POST", "/p/x/api/nodes/ticks/run")) == "editor"
    assert _needed_role(Req("POST", "/p/x/api/views/board/refresh/more")) == "editor"
