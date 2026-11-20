"""Hub mode: many projects, one tick, export/restore, and the HTTP surface a
host app drives (parameters, as-of runs, backfill, schedules set from the UI)."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exuvia.server import create_app, create_hub_app

PRICES = '''import exuvia


@exuvia.node(params=["as_of"])
def prices():
    """One row per day."""
    import pandas as pd

    day = exuvia.param("as_of")
    return pd.DataFrame({"as_of": [day], "px": [int(day[-2:])]})
'''


def template(root: Path) -> Path:
    t = root / "templates" / "dated"
    (t / "nodes").mkdir(parents=True)
    (t / "exuvia.json").write_text(
        json.dumps({"name": "template", "timezone": "America/New_York",
                    "params": {"as_of": {"type": "date", "default": "today"}}}),
        encoding="utf-8",
    )
    (t / "nodes" / "prices.py").write_text(PRICES, encoding="utf-8")
    (t / "AGENTS.md").write_text("Prices come from the test fixture.", encoding="utf-8")
    return root / "templates"


def wait_idle(client: TestClient, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get("/api/hub/state").json()
        if not state["busy"]:
            return state
        time.sleep(0.1)
    raise AssertionError("hub stayed busy")


@pytest.fixture
def hub(tmp_path):
    app = create_hub_app(tmp_path / "hub", template(tmp_path))
    with TestClient(app) as client:
        yield client


def test_create_from_template_list_delete(hub):
    assert hub.get("/api/projects").json() == {"projects": [], "templates": ["dated"]}
    r = hub.post("/api/projects", json={"id": "wf_1", "name": "Put selling", "template": "dated"})
    assert r.status_code == 200 and r.json() == {"id": "wf_1", "name": "Put selling"}
    assert hub.post("/api/projects", json={"id": "wf_1"}).status_code == 409
    assert hub.post("/api/projects", json={"id": "../x"}).status_code == 400
    assert hub.post("/api/projects", json={"id": "wf_2", "template": "nope"}).status_code == 400

    snap = hub.get("/p/wf_1/api/project").json()
    assert snap["name"] == "Put selling" and snap["timezone"] == "America/New_York"
    assert snap["params"][0]["name"] == "as_of" and snap["params"][0]["value"]
    assert [n["name"] for n in snap["nodes"]] == ["prices"]
    assert hub.get("/p/nope/api/project").status_code == 404

    assert hub.delete("/api/projects/wf_1").json() == {"ok": True}
    assert hub.get("/api/projects").json()["projects"] == []


def test_apply_template_replaces_code_and_keeps_history(tmp_path, hub):
    hub.post("/api/projects", json={"id": "wf", "name": "Kept name", "template": "dated"})
    base = "/p/wf/api"
    d1 = json.dumps({"as_of": "2026-03-02"})
    hub.post(f"{base}/run", json={"params": {"as_of": "2026-03-02"}})
    wait_idle(hub)
    # A node made in the project, and a newer template: one node changed, one added.
    hub.post(f"{base}/nodes", json={"name": "scratch", "kind": "sql", "deps": ["prices"], "description": "x"})
    nodes = tmp_path / "templates" / "dated" / "nodes"
    (nodes / "prices.py").write_text(PRICES.replace("int(day[-2:])", "int(day[-2:]) * 10"), encoding="utf-8")
    (nodes / "doubled.sql").write_text("-- Twice the price.\nselect px * 2 as px2 from prices\n", encoding="utf-8")

    r = hub.put("/api/projects/wf/template", json={"template": "dated"})
    assert r.status_code == 200 and r.json() == {"id": "wf", "name": "Kept name", "nodes": ["doubled", "prices"]}
    assert hub.put("/api/projects/wf/template", json={"template": "nope"}).status_code == 400
    assert hub.put("/api/projects/missing/template", json={"template": "dated"}).status_code == 404

    snap = hub.get(f"{base}/project", params={"params": d1}).json()
    assert snap["name"] == "Kept name"
    states = {n["name"]: n["state"] for n in snap["nodes"]}
    assert set(states) == {"doubled", "prices"} and states["prices"] != "fresh"  # the code changed
    # The earlier run is still there, and the new code runs for the same date.
    assert [h["run_params"]["as_of"] for h in hub.get(f"{base}/nodes/prices/history").json()] == ["2026-03-02"]
    hub.post(f"{base}/run", json={"params": {"as_of": "2026-03-02"}})
    wait_idle(hub)
    assert hub.get(f"{base}/nodes/doubled/data", params={"params": d1}).json()["rows"] == [[40]]


def test_as_of_runs_history_and_backfill_over_http(hub):
    hub.post("/api/projects", json={"id": "wf", "template": "dated"})
    base = "/p/wf/api"
    d1 = json.dumps({"as_of": "2026-03-02"})

    r = hub.post(f"{base}/run", json={"params": {"as_of": "2026-03-02"}})
    assert r.json()["targets"] == ["prices"] and r.json()["params"] == {"as_of": "2026-03-02"}
    wait_idle(hub)
    assert hub.get(f"{base}/nodes/prices/data", params={"params": d1}).json()["rows"] == [["2026-03-02", 2]]
    assert hub.get(f"{base}/project", params={"params": d1}).json()["nodes"][0]["state"] == "fresh"
    assert hub.post(f"{base}/run", json={"params": {"as_of": "2026-03-02"}}).json()["targets"] == []

    assert hub.post(f"{base}/run", json={"params": {"as_of": "soon"}}).status_code == 400
    assert hub.get(f"{base}/project", params={"params": "[1]"}).status_code == 400

    # A date range (weekdays only): 2026-03-06 is a Friday, 03-09 a Monday.
    r = hub.post(f"{base}/backfill", json={"param": "as_of", "start": "2026-03-06", "end": "2026-03-09"})
    assert r.json()["values"] == ["2026-03-06", "2026-03-09"]
    wait_idle(hub)
    assert hub.get(f"{base}/backfill").json()["backfill"]["state"] == "done"

    hist = hub.get(f"{base}/nodes/prices/history").json()
    assert sorted(h["run_params"]["as_of"] for h in hist) == ["2026-03-02", "2026-03-06", "2026-03-09"]
    seen = hub.get(f"{base}/params").json()["params"][0]["values"]
    assert seen == ["2026-03-09", "2026-03-06", "2026-03-02"]

    q = hub.post(f"{base}/query", json={"sql": 'select "_as_of", px from prices__runs order by 1'})
    assert q.json()["rows"] == [["2026-03-02", 2], ["2026-03-06", 6], ["2026-03-09", 9]]
    assert hub.post(f"{base}/backfill", json={"param": "as_of"}).status_code == 400


def test_schedule_set_from_outside_the_code_and_hub_tick(hub):
    hub.post("/api/projects", json={"id": "wf", "template": "dated"})
    base = "/p/wf/api"
    assert hub.post("/api/schedule/tick").json() == {"started": {}, "next": None}

    r = hub.post(f"{base}/nodes/prices/schedule", json={"cron": "30 16 * * 1-5"})
    assert r.json()["schedule"]["cron"] == "30 16 * * 1-5" and r.json()["schedule"]["source"] == "config"
    assert hub.post(f"{base}/nodes/prices/schedule", json={"cron": "nope"}).status_code == 400

    # The first tick only starts the clock; `next` tells the host when to come back.
    first = hub.post("/api/schedule/tick").json()
    assert first["started"] == {} and first["next"] > time.time()
    node = hub.get(f"{base}/project").json()["nodes"][0]
    assert node["schedule"]["next"] == first["next"]

    # Pause keeps the cron; clearing the override removes the schedule entirely.
    hub.post(f"{base}/nodes/prices/schedule", json={"paused": True})
    assert hub.post("/api/schedule/tick").json()["next"] is None
    hub.post(f"{base}/nodes/prices/schedule", json={"cron": None, "paused": False})
    assert hub.get(f"{base}/project").json()["nodes"][0]["schedule"] is None


def test_one_broken_project_does_not_stop_the_hub_tick(hub, monkeypatch):
    hub.post("/api/projects", json={"id": "good", "template": "dated"})
    hub.post("/api/projects", json={"id": "broken", "template": "dated"})
    hub.post("/p/good/api/nodes/prices/schedule", json={"cron": "30 16 * * 1-5"})
    h = hub.app.state.hub
    get = h.get

    async def get_or_fail(pid):
        if pid == "broken":
            raise RuntimeError("cannot load")
        return await get(pid)

    monkeypatch.setattr(h, "get", get_or_fail)
    r = hub.post("/api/schedule/tick")
    assert r.status_code == 200
    body = r.json()
    # The healthy project still schedules; the broken one is named, not fatal.
    assert body["next"] is not None and body["next"] > time.time()
    assert body["errors"] == {"broken": "RuntimeError: cannot load"}


def test_export_and_restore_round_trip(tmp_path, hub):
    hub.post("/api/projects", json={"id": "wf", "name": "Kept", "template": "dated"})
    v0 = hub.get("/api/hub/state").json()["version"]
    hub.post("/p/wf/api/run", json={"params": {"as_of": "2026-03-02"}})
    state = wait_idle(hub)
    assert state["version"] > v0  # the host can see there is something to persist
    blob = hub.get("/api/hub/export").content

    # A brand-new hub on a different disk, as after a container restart.
    with TestClient(create_hub_app(tmp_path / "elsewhere")) as fresh:
        assert fresh.post("/api/hub/import", content=b"not a tarball").status_code == 400
        assert fresh.post("/api/hub/import", content=blob).json() == {"ok": True, "projects": ["wf"]}
        snap = fresh.get("/p/wf/api/project", params={"params": json.dumps({"as_of": "2026-03-02"})}).json()
        assert snap["name"] == "Kept" and snap["nodes"][0]["state"] == "fresh"
        rows = fresh.get("/p/wf/api/nodes/prices/data", params={"params": json.dumps({"as_of": "2026-03-02"})})
        assert rows.json()["rows"] == [["2026-03-02", 2]]


def test_params_can_be_declared_over_http(hub):
    hub.post("/api/projects", json={"id": "blank"})
    base = "/p/blank/api"
    assert hub.get(f"{base}/params").json() == {"params": []}
    r = hub.put(f"{base}/params/as_of", json={"type": "date", "default": "today", "description": "run date"})
    assert r.json()["params"][0]["name"] == "as_of"
    assert hub.put(f"{base}/params/as_of", json={"type": "date", "default": "never"}).status_code == 400
    assert hub.put(f"{base}/params/Bad-Name", json={"type": "string"}).status_code == 400
    assert hub.patch(f"{base}/project", json={"timezone": "Mars/Olympus"}).status_code == 400
    assert hub.patch(f"{base}/project", json={"timezone": "UTC"}).json()["timezone"] == "UTC"
    assert hub.delete(f"{base}/params/as_of").json() == {"params": []}


def test_agent_notes_and_saved_conversation(tmp_path, hub):
    hub.post("/api/projects", json={"id": "wf", "template": "dated"})
    assert hub.get("/p/wf/api/agent/abc").json() == {"session_id": "abc", "running": False, "events": []}
    agent = hub.app.state.hub._ctxs["wf"].agent  # loaded by the request above
    system = agent.instructions()
    assert "Prices come from the test fixture." in system  # AGENTS.md reaches the agent
    assert "`as_of` (date" in system
    agent.ws.store.save_agent_session("abc", [{"role": "user", "content": "hi"}], [{"type": "user", "text": "hi"}])
    agent.sessions.clear()
    agent.transcripts.clear()
    assert hub.get("/p/wf/api/agent/abc").json()["events"] == [{"type": "user", "text": "hi"}]
    assert hub.delete("/p/wf/api/agent/abc").json() == {"ok": True}
    assert hub.get("/p/wf/api/agent/abc").json()["events"] == []


def test_single_project_app_keeps_its_routes(tmp_path):
    (tmp_path / "nodes").mkdir()
    (tmp_path / "nodes" / "one.sql").write_text("select 1 as x\n", encoding="utf-8")
    with TestClient(create_app(tmp_path)) as client:
        assert [n["name"] for n in client.get("/api/project").json()["nodes"]] == ["one"]
        assert client.post("/api/run-stale").json()["targets"] == ["one"]
        assert client.get("/api/config").json()["scheduler"] in ("internal", "external")


def test_healthz_reports_uptime(hub):
    first = hub.get("/healthz").json()
    assert first["ok"] is True and first["mode"] == "hub"
    assert first["uptime_s"] >= 0
    time.sleep(0.05)
    assert hub.get("/healthz").json()["uptime_s"] > first["uptime_s"]


BROKEN = '''import exuvia


@exuvia.node()
def broken():
    """Always fails."""
    raise ValueError("no data for that day")
'''


def test_run_wait_reports_like_the_agent(hub):
    """An outside agent (Lobster's MCP server) runs a node and waits for the
    same report the built-in agent's run_node returns."""
    hub.post("/api/projects", json={"id": "wf", "template": "dated"})
    base = "/p/wf/api"
    done = hub.post(f"{base}/nodes/prices/run", json={"params": {"as_of": "2026-03-02"}, "wait": 60}).json()
    assert done["ok"] is True and done["running"] is False
    assert done["runs"] == {"prices": "success"} and done["rows"] == 1 and done["run_id"]
    assert done["head"] == [{"as_of": "2026-03-02", "px": 2}]

    assert hub.put(f"{base}/nodes/broken", json={"source": BROKEN, "kind": "python"}).status_code == 200
    failed = hub.post(f"{base}/nodes/broken/run", json={"wait": 60}).json()
    assert failed["ok"] is False and failed["running"] is False
    assert "no data for that day" in (failed["error"] or "") + failed["log_tail"]

    graph = hub.post(f"{base}/run", json={"params": {"as_of": "2026-03-03"}, "force": True, "wait": 60}).json()
    assert graph["ok"] is False and graph["runs"]["prices"] == "success"
    assert [f["node"] for f in graph["failed"]] == ["broken"]

    # Without `wait` the call returns as soon as the run starts.
    assert hub.post(f"{base}/nodes/prices/run", json={}).json() == {"ok": True, "params": {"as_of": hub.get(f"{base}/params").json()["params"][0]["value"]}}
    wait_idle(hub)


def test_guide_serves_the_agent_instructions(hub):
    hub.post("/api/projects", json={"id": "wf", "template": "dated"})
    guide = hub.get("/p/wf/api/guide").json()
    assert "Prices come from the test fixture." in guide["instructions"]
    assert "@exuvia.node" in guide["instructions"]
    assert {"write_node", "run_node", "define_param"} <= {t["name"] for t in guide["tools"]}
