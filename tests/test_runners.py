"""Runners: runs_on nodes run on a person's own machine (a desktop with a
Bloomberg Terminal, say), which takes them from the hub over HTTPS, runs
only code its owner approved there, and sends the output back."""

import socket
import threading
import time
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from exuvia.desktop import Approvals, DesktopRunner, approve_pending, diff, hub_url
from exuvia.executor import ExecRequest
from exuvia.project import Project
from exuvia.runners import Broker, RunnerError, files_hash, used_env
from exuvia.server import create_app, create_hub_app
from exuvia.workspace import Workspace

from test_engine import run, write

TOKEN = "runner-test-token"
BBG = '''import os

import pandas as pd

import exuvia
from lib.fx import RATE


@exuvia.node(runs_on="desk")
def bbg():
    return pd.DataFrame({{"px": [1.0, 2.0], "rate": [RATE, RATE], "key": [os.environ["BBG_KEY"]] * 2,
                         "other": [os.environ.get("OTH" + "ER")] * 2, "v": [{v}] * 2}})
'''
DOUBLED = '''import exuvia


@exuvia.node()
def doubled(bbg):
    return bbg.assign(px=bbg.px * 2)
'''


def project(root: Path, v: int = 1) -> None:
    write(root, "bbg.py", BBG.format(v=v))
    write(root, "doubled.py", DOUBLED)
    (root / "lib").mkdir(exist_ok=True)
    (root / "lib" / "fx.py").write_text("RATE = 1.5\n", encoding="utf-8")


# -- the node header ------------------------------------------------------------
def test_runs_on_parses_and_takes_no_upstreams(tmp_path):
    project(tmp_path)
    write(tmp_path, "sq.sql", "-- from the desk\n-- runs_on: desk\nselect 1 as x\n")
    write(tmp_path, "bad.py", "import exuvia\n\n\n@exuvia.node(runs_on='desk')\ndef bad(doubled):\n    return doubled\n")
    write(tmp_path, "pushed.py", "import exuvia\n\n\n@exuvia.node(runs_on='desk', push=True)\ndef pushed():\n    pass\n")
    write(tmp_path, "lively.py", "import exuvia\n\n\n@exuvia.node(runs_on='desk', live=True)\ndef lively():\n    pass\n")
    write(tmp_path, "label.py", "import exuvia\n\n\n@exuvia.node(runs_on='a b')\ndef label():\n    pass\n")
    g = Project(tmp_path).load()
    assert g.nodes["bbg"].runs_on == "desk" and g.nodes["bbg"].parse_error is None
    assert g.nodes["bbg"].helpers == ["lib/fx.py"]
    assert g.nodes["sq"].runs_on == "desk" and g.nodes["sq"].description == "from the desk"
    assert "takes no upstream nodes" in g.nodes["bad"].parse_error
    assert "push node can't have `runs_on`" in g.nodes["pushed"].parse_error
    assert "can't be live" in g.nodes["lively"].parse_error
    assert "names the label" in g.nodes["label"].parse_error
    assert g.nodes["doubled"].runs_on is None


def test_a_project_nothing_serves_cannot_hand_it_to_a_runner(tmp_path):
    project(tmp_path)
    ws = Workspace(tmp_path)
    done = run(ws, ["bbg"])["bbg"]
    assert done["status"] == "failed" and "only a served project" in done["error"]


# -- the broker -------------------------------------------------------------------
def test_only_the_variables_the_code_names_go_to_the_runner():
    files = {"nodes/a.py": "os.environ['API_KEY']\nAPI_KEY_2 = 1\n"}
    assert used_env({"API_KEY": "1", "API": "2", "KEY": "3", "API_KEY_2": "4", "XAPI_KEY": "5"}, files) == {
        "API_KEY": "1", "API_KEY_2": "4",
    }
    assert files_hash(files) == files_hash(dict(files)) != files_hash({"nodes/a.py": "x"})


def test_the_broker_keeps_only_output_that_matches_its_hash(tmp_path):
    broker = Broker()
    logs: list[str] = []
    req = ExecRequest(run_id="r1", node="n", kind="python", path="nodes/n.py", root=tmp_path, out_dir=tmp_path / "out")
    got: dict = {}
    t = threading.Thread(target=lambda: got.update(res=broker.execute("p", "desk", req, {"nodes/n.py": "x"}, logs.append)))
    t.start()
    me = broker.heard("ada", "u1", "laptop", ["desk"])
    deadline = time.monotonic() + 5
    job = None
    while job is None and time.monotonic() < deadline:
        job = broker.try_claim(me, lambda p: p == "p")
    assert job and job["id"] == "r1" and job["files"] == {"nodes/n.py": "x"}
    other = broker.heard("bob", "u2", "laptop", ["desk"])
    with pytest.raises(RunnerError) as e:
        broker.log("r1", job["token"], other.key, "hi")
    assert e.value.status == 403
    data = b"some bytes"
    src = tmp_path / "upload"
    src.write_bytes(data)
    with pytest.raises(RunnerError) as e:
        broker.output("r1", job["token"], me.key, "json", "0" * 16, src)
    assert e.value.status == 422
    with pytest.raises(RunnerError, match="send the output"):
        broker.finish("r1", job["token"], me.key, {"ok": True, "kind": "json", "artifact_id": "0" * 16})
    import hashlib

    aid = hashlib.sha256(data).hexdigest()[:16]
    broker.output("r1", job["token"], me.key, "json", aid, src)
    broker.finish("r1", job["token"], me.key, {"ok": True, "kind": "json", "artifact_id": aid, "rows": None})
    t.join(5)
    assert got["res"].ok and Path(got["res"].artifact_path).read_bytes() == data
    assert "waiting for a runner labelled desk" in "".join(logs) and "taken by the runner laptop" in "".join(logs)
    with pytest.raises(RunnerError) as e:  # the run is over: no longer the runner's
        broker.log("r1", job["token"], me.key, "")
    assert e.value.status == 410


def test_a_run_nobody_takes_fails_after_the_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_RUNNER_WAIT", "1")
    req = ExecRequest(run_id="r1", node="n", kind="python", path="nodes/n.py", root=tmp_path, out_dir=tmp_path)
    res = Broker().execute("", "desk", req, {}, lambda _: None)
    assert not res.ok and "no runner labelled desk took the run" in res.error


# -- end to end: `exuvia serve` and a runner --------------------------------------
@pytest.fixture
def served(tmp_path, monkeypatch):
    import uvicorn

    monkeypatch.setenv("EXUVIA_API_TOKEN", TOKEN)
    root = tmp_path / "p"
    project(root)
    app = create_app(root)
    ws = app.state.ws
    ws.set_env_var("BBG_KEY", "k-123", True)
    ws.set_env_var("OTHER", "not for the runner", False)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    monkeypatch.delenv("EXUVIA_API_TOKEN")
    yield f"http://127.0.0.1:{port}", ws
    server.should_exit = True
    thread.join(timeout=10)


def _api(url):
    import httpx

    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)


def _latest(ws, node, done=True, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        last = ws.store.last_run(node)
        if last and (not done or last["status"] in ("success", "failed")):
            return ws.store.run(last["id"])
        time.sleep(0.1)
    raise AssertionError(f"no finished run of {node}")


def test_a_runner_runs_approved_code_and_sends_the_output_back(served, tmp_path):
    url, ws = served
    api = _api(url)
    lines: list[str] = []
    runner = DesktopRunner(url + "/p/ignored", ["desk"], name="laptop", folder=tmp_path / "home", token=TOKEN,
                           interactive=False, log=lines.append)
    try:
        assert runner.run_once(wait=0) is None  # nothing waits yet
        assert api.get("/api/runners").json()["runners"][0] | {"last_seen": 0} == {
            "name": "laptop", "owner": "operator", "labels": ["desk"], "last_seen": 0, "online": True, "busy": None,
            "version": runner.version,
        }

        # New code: not run, kept for its owner to approve.
        assert api.post("/api/nodes/bbg/run", json={}).json()["ok"]
        res = runner.run_once(wait=10)
        assert not res["ok"] and "exuvia runner approve" in res["error"]
        first = _latest(ws, "bbg")
        assert first["status"] == "failed" and "exuvia runner approve" in first["error"]
        assert "taken by the runner laptop (operator)" in first["log"]
        approvals = Approvals(tmp_path / "home")
        [(_, item)] = approvals.pending()
        assert sorted(item["files"]) == ["lib/fx.py", "nodes/bbg.py"]
        shown: list[str] = []
        assert approve_pending(approvals, shown.append, lambda _: True) == 1
        assert "+def bbg():" in "".join(shown) and approvals.pending() == []

        # Approved: it runs here, the output goes back and cascades downstream.
        assert api.post("/api/nodes/bbg/run", json={}).json()["ok"]
        res = runner.run_once(wait=10)
        assert res["ok"], res
        run_ = _latest(ws, "bbg")
        assert run_["status"] == "success" and run_["trigger"] != "push"
        df = pd.read_parquet(ws.project.root / run_["artifact_path"])
        assert df.px.tolist() == [1.0, 2.0] and df.rate.tolist() == [1.5, 1.5]
        assert df.key.tolist() == ["k-123"] * 2  # a variable the code names
        assert df.other.isna().all()  # one it doesn't: not sent
        assert "k-123" not in run_["log"]
        assert _latest(ws, "doubled")["status"] == "success"
        assert any("run " in line and "done" in line for line in lines)

        # Changed code asks again: a no in a terminal fails the run, a yes runs it.
        project(ws.project.root, v=2)
        ws.reload()
        runner.interactive = True
        runner.confirm = lambda _: False
        assert api.post("/api/nodes/bbg/run", json={}).json()["ok"]
        assert "not approved" in runner.run_once(wait=10)["error"]
        shown_diff = "".join(lines)
        assert '-                         "other": [os.environ.get("OTH" + "ER")] * 2, "v": [1] * 2})' in shown_diff
        assert '+                         "other": [os.environ.get("OTH" + "ER")] * 2, "v": [2] * 2})' in shown_diff
        runner.confirm = lambda _: True
        assert api.post("/api/nodes/bbg/run", json={}).json()["ok"]
        assert runner.run_once(wait=10)["ok"]
        assert pd.read_parquet(ws.project.root / _latest(ws, "bbg")["artifact_path"]).v.tolist() == [2, 2]

        # A run cancelled on the hub while it waits for a runner ends at once.
        assert api.post("/api/nodes/bbg/run", json={}).json()["ok"]
        waiting = _latest(ws, "bbg", done=False)
        deadline = time.monotonic() + 10
        while not api.get("/api/runners").json()["runs"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert api.get("/api/runners").json()["runs"][0]["node"] == "bbg"
        assert api.post(f"/api/runs/{waiting['id']}/cancel").json()["ok"]
        assert _latest(ws, "bbg")["error"] == "cancelled"
        assert runner.run_once(wait=0) is None
    finally:
        runner.close()


def test_the_diff_shows_what_changed():
    assert diff(None, {"a.py": "x = 1\n"}) == "--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n"
    assert "-x = 1\n+x = 2\n" in diff({"a.py": "x = 1\n"}, {"a.py": "x = 2\n"})
    assert diff({"a.py": "x"}, {"a.py": "x"}) == ""
    assert hub_url("https://hub.example.com/p/sales/") == "https://hub.example.com"
    assert hub_url("https://h.example.com/exuvia/p/sales") == "https://h.example.com/exuvia"


# -- a hub: a runner takes runs only from projects its owner can edit -------------
def test_on_a_hub_a_runner_takes_only_its_owners_projects(tmp_path, monkeypatch):
    from exuvia.db import close_shared

    for name in ("EXUVIA_API_TOKEN", "EXUVIA_DB", "EXUVIA_DB_SCOPE", "EXUVIA_DEFAULT_ACCESS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXUVIA_AUTH", "headers")
    close_shared()
    alice, bob = {"X-Forwarded-User": "alice"}, {"X-Forwarded-User": "bob"}
    try:
        with TestClient(create_hub_app(tmp_path / "hub")) as c:
            c.get("/api/me", headers=bob)
            assert c.post("/api/projects", json={"id": "desk"}, headers=alice).status_code == 200
            src = BBG.format(v=1).replace("from lib.fx import RATE\n", "RATE = 1.5\n")
            assert c.put("/p/desk/api/nodes/bbg", json={"source": src}, headers=alice).status_code == 200
            assert c.post("/p/desk/api/nodes/bbg/run", json={}, headers=alice).json()["ok"]
            claim = {"labels": ["desk"], "name": "box", "wait": 2}
            assert c.post("/api/runners/claim", json=claim, headers=bob).json()["job"] is None
            job = c.post("/api/runners/claim", json=claim, headers=alice).json()["job"]
            assert job["project"] == "desk" and job["node"] == "bbg" and job["env"] == {}
            box = {"X-Exuvia-Runner": "box"}
            log = {"token": job["token"], "text": "hello from the desk\n"}
            assert c.post(f"/api/runners/jobs/{job['id']}/log", json=log, headers=bob | box).status_code == 403
            assert c.post(f"/api/runners/jobs/{job['id']}/log", json=log, headers=alice | box).status_code == 200
            done = {"token": job["token"], "result": {"ok": False, "error": "terminal is logged out"}}
            assert c.post(f"/api/runners/jobs/{job['id']}/result", json=done, headers=alice | box).status_code == 200
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                r = c.get(f"/p/desk/api/runs/{job['id']}", headers=alice).json()
                if r["status"] == "failed":
                    break
                time.sleep(0.05)
            assert r["error"] == "terminal is logged out" and "hello from the desk" in r["log"]
            # Each sees their own runners.
            assert [x["name"] for x in c.get("/api/runners", headers=alice).json()["runners"]] == ["box"]
            assert [x["name"] for x in c.get("/api/runners", headers=bob).json()["runners"]] == ["box"]
            assert c.get("/api/runners", headers=bob).json()["runs"] == []
    finally:
        close_shared()
