"""The canvas's overview: stages (stages of the graph, kept in exuvia.json)
and views pinned on the canvas (``canvas`` in view.json)."""

import json

from fastapi.testclient import TestClient

from exuvia.agent import TOOLS, Agent
from exuvia.project import Project
from exuvia.server import create_app
from exuvia.workspace import Workspace

from test_views import APP, call, manifest, project


def config(root, **extra):
    cfg = json.loads((root / "exuvia.json").read_text(encoding="utf-8"))
    cfg.update(extra)
    (root / "exuvia.json").write_text(json.dumps(cfg), encoding="utf-8")


# -- stages -----------------------------------------------------------------------
def test_stages_are_read_from_the_config_in_order(tmp_path):
    root = project(tmp_path)
    config(root, stages={
        "ingest": {"title": "Ingest", "description": "Raw data in", "nodes": ["prices", "gone"]},
        "ref": ["universe", "prices"],  # the short form; prices is in ingest already
        "Bad-Name": ["universe"],
        "odd": {"nodes": ["universe"], "colour": "red"},
    })
    g = Project(root).load()
    assert list(g.stages) == ["ingest", "ref", "Bad-Name", "odd"]
    ingest = g.stages["ingest"]
    assert (ingest.title, ingest.description, ingest.nodes, ingest.missing) == ("Ingest", "Raw data in", ["prices"], ["gone"])
    assert ingest.problem is None
    ref = g.stages["ref"]
    assert ref.title == "ref" and ref.nodes == ["universe"] and ref.problem == "Already in another stage: prices (in ingest)."
    assert "snake_case" in g.stages["Bad-Name"].problem and g.stages["Bad-Name"].nodes == []
    assert "Unknown key(s) in stage odd: colour" in g.stages["odd"].problem
    assert g.nodes["prices"].stage == "ingest" and g.nodes["universe"].stage == "ref"


def test_arranging_stages_changes_no_node_and_no_code_hash(tmp_path):
    ws = Workspace(project(tmp_path))
    before = {n.name: n.code_hash for n in ws.graph.nodes.values()}
    ws.set_stage("ingest", ["prices", "universe"], title="Ingest")
    assert {n.name: n.code_hash for n in ws.graph.nodes.values()} == before
    assert json.loads((tmp_path / "exuvia.json").read_text())["stages"] == {
        "ingest": {"title": "Ingest", "nodes": ["prices", "universe"]},
    }


def test_set_stage_moves_nodes_and_keeps_what_it_is_not_given(tmp_path):
    ws = Workspace(project(tmp_path))
    ws.set_stage("ingest", ["prices", "universe"], title="Ingest", description="Raw data")
    out = ws.set_stage("ref", ["universe"])
    assert [(g["name"], g["nodes"]) for g in out["stages"]] == [("ingest", ["prices"]), ("ref", ["universe"])]
    ws.set_stage("ingest", ["prices"])  # no title or description given: both kept
    assert ws.graph.stages["ingest"].title == "Ingest" and ws.graph.stages["ingest"].description == "Raw data"
    ws.set_stage("ingest", ["prices"], title="")  # an empty title falls back to the name
    assert ws.graph.stages["ingest"].title == "ingest"
    ws.set_stage("ingest", None)
    ws.set_stage("ref", None)
    assert "stages" not in json.loads((tmp_path / "exuvia.json").read_text())
    for bad in (("Bad", ["prices"]), ("ok", ["nope"])):
        try:
            ws.set_stage(*bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_stage_routes_and_snapshot(tmp_path):
    with TestClient(create_app(project(tmp_path))) as client:
        r = client.put("/api/stages/ingest", json={"nodes": ["prices"], "title": "Ingest"})
        assert r.status_code == 200 and r.json()["stages"][0]["nodes"] == ["prices"]
        assert client.put("/api/stages/x", json={"nodes": ["nope"]}).status_code == 400
        snap = client.get("/api/project").json()
        assert snap["stages"] == [
            {"name": "ingest", "title": "Ingest", "description": "", "nodes": ["prices"], "missing": [], "problem": None},
        ]
        assert {n["name"]: n["stage"] for n in snap["nodes"]} == {"prices": "ingest", "universe": None}
        assert client.get("/api/stages").json()["stages"][0]["name"] == "ingest"
        assert client.delete("/api/stages/ingest").status_code == 200
        assert client.delete("/api/stages/ingest").status_code == 404


def test_agent_stage_tools(tmp_path):
    agent = Agent(Workspace(project(tmp_path)))
    assert {"set_stage", "delete_stage"} <= {t["name"] for t in TOOLS}
    ok, text, _ = call(agent, "set_stage", {"name": "ingest", "nodes": ["prices", "universe"], "title": "Ingest"})
    assert ok and json.loads(text)["stages"][0]["nodes"] == ["prices", "universe"]
    ok, text, _ = call(agent, "list_nodes", {})
    assert ok and {n["name"]: n.get("stage") for n in json.loads(text)} == {"prices": "ingest", "universe": "ingest"}
    ok, text, _ = call(agent, "set_stage", {"name": "x", "nodes": ["nope"]})
    assert not ok and "no node named nope" in text
    ok, _, _ = call(agent, "delete_stage", {"name": "ingest"})
    assert ok and not agent.ws.graph.stages
    ok, text, _ = call(agent, "delete_stage", {"name": "ingest"})
    assert not ok and text == "no stage named ingest"
    assert "## Stages" in agent.instructions()


# -- views pinned on the canvas ------------------------------------------------------
def test_a_view_can_be_pinned_on_the_canvas(tmp_path):
    root = project(tmp_path, {
        "plain": {"view.json": manifest(), "App.tsx": APP},
        "pinned": {"view.json": manifest(canvas=True), "App.tsx": APP},
        "sized": {"view.json": manifest(canvas={"width": 800}), "App.tsx": APP},
        "off": {"view.json": manifest(canvas=False), "App.tsx": APP},
        "too_big": {"view.json": manifest(canvas={"width": 5000}), "App.tsx": APP},
        "odd": {"view.json": manifest(canvas="yes"), "App.tsx": APP},
    })
    g = Project(root).load()
    assert g.views["plain"].canvas is None and g.views["off"].canvas is None
    assert g.views["pinned"].canvas == {"width": 560, "height": 360}
    assert g.views["sized"].canvas == {"width": 800, "height": 360}
    assert "`canvas.width` must be a number of pixels from 280 to 1600" in g.views["too_big"].parse_error
    assert "`canvas` pins the view" in g.views["odd"].parse_error
    for name in ("plain", "pinned", "sized"):
        assert g.views[name].parse_error is None


def test_writing_a_manifest_keeps_canvas_out_unless_pinned(tmp_path):
    agent = Agent(Workspace(project(tmp_path)))
    read = lambda: json.loads((tmp_path / "views" / "board" / "view.json").read_text())  # noqa: E731
    call(agent, "write_view", {"name": "board", "reads": ["prices"]})
    assert "canvas" not in read()
    call(agent, "write_view", {"name": "board", "reads": ["prices"], "canvas": True})
    assert read()["canvas"] is True
    call(agent, "write_view", {"name": "board", "reads": ["prices"], "canvas": {"width": 700, "height": 400}})
    assert read()["canvas"] == {"width": 700, "height": 400}
    ok, text, _ = call(agent, "write_view", {"name": "board", "reads": ["prices"], "canvas": {"depth": 1}})
    assert not ok and "`canvas` pins the view" in text
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/project").json()["views"][0]["canvas"] == {"width": 700, "height": 400}
