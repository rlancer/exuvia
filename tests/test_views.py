"""Views: UI folders under views/ that read node outputs and never run compute.

Discovery and manifest errors, state per parameter set, the path-safe file
API, code revisions and restore, the agent tools, the file watcher, and how
hub templates and exports carry views."""

import asyncio
import io
import json
import os
import sqlite3
import tarfile
import textwrap
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exuvia.agent import TOOLS, Agent
from exuvia.executor import bundle_files
from exuvia.project import Project
from exuvia.server import create_app, create_hub_app
from exuvia.workspace import Workspace, acting_as

PRICES = '''
import exuvia

@exuvia.node(params=["as_of"])
def prices():
    import pandas as pd
    return pd.DataFrame({"day": [exuvia.param("as_of")], "px": [1.0]})
'''

UNIVERSE = '''
import exuvia

@exuvia.node()
def universe():
    import pandas as pd
    return pd.DataFrame({"ticker": ["AAPL"]})
'''

APP = "export default function App() {\n  return <div>hello</div>;\n}\n"
CONFIG = {"name": "p", "params": {"as_of": {"type": "date", "default": "2026-01-02"}}}


def project(root: Path, views: dict[str, dict[str, str]] | None = None) -> Path:
    """A project with two nodes (one per as_of value, one shared) and views
    given as {view: {relative path: text}}."""
    (root / "nodes").mkdir(parents=True, exist_ok=True)
    (root / "exuvia.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    for name, body in (("prices", PRICES), ("universe", UNIVERSE)):
        (root / "nodes" / f"{name}.py").write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    for view, files in (views or {}).items():
        for rel, text in files.items():
            p = root / "views" / view / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    return root


def manifest(**kw) -> str:
    return json.dumps({"title": "Board", "reads": ["prices", "universe"], **kw})


def run(ws, *a, **k):
    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        return await ws.run(*a, **k)

    return asyncio.run(go())


# -- discovery ------------------------------------------------------------------
def test_discovery_lists_good_and_broken_views(tmp_path):
    root = project(
        tmp_path,
        {
            "board": {
                "view.json": manifest(description="Prices by day"),
                "App.tsx": APP,
                "components/Chart.tsx": "export const Chart = () => null;\n",
                "styles.css": "div { color: red; }\n",
                "logo.png": "not part of the view",
                "node_modules/x/index.js": "ignored\n",
            },
            "broken_json": {"view.json": "{not json", "App.tsx": APP},
            "no_manifest": {"App.tsx": APP},
            "unknown_read": {"view.json": json.dumps({"reads": ["prices", "nope"]}), "App.tsx": APP},
            "no_entry": {"view.json": json.dumps({"entry": "Main.tsx"})},
            "bad_fields": {
                "view.json": json.dumps({"runtime": "vue", "reads": "prices", "extra": 1, "title": 3}),
                "App.tsx": APP,
            },
            "Bad-Name": {"view.json": "{}", "App.tsx": APP},
        },
    )
    g = Project(root).load()
    assert sorted(g.views) == [
        "Bad-Name", "bad_fields", "board", "broken_json", "no_entry", "no_manifest", "unknown_read",
    ]
    board = g.views["board"]
    assert board.parse_error is None
    assert (board.title, board.description, board.runtime, board.entry) == ("Board", "Prices by day", "react", "App.tsx")
    assert board.reads == ["prices", "universe"]
    assert board.files == ["App.tsx", "components/Chart.tsx", "styles.css", "view.json"]
    assert board.path == "views/board" and len(board.hash) == 16
    assert g.readers("prices") == ["board", "unknown_read"]

    assert "not valid JSON" in g.views["broken_json"].parse_error
    assert g.views["broken_json"].title == "broken_json"  # still listed, with defaults
    assert g.views["no_manifest"].parse_error == "Missing view.json."
    unknown = g.views["unknown_read"]
    assert unknown.reads == ["prices", "nope"]  # kept, and reported
    assert unknown.parse_error == "Reads unknown node(s): nope."
    assert g.views["no_entry"].parse_error == "Entry file Main.tsx does not exist."
    bad = g.views["bad_fields"].parse_error
    assert "Unknown key(s) in view.json: extra" in bad and "runtime 'vue'" in bad
    assert "`reads` must be a list" in bad and "`title` must be a string" in bad
    assert "snake_case" in g.views["Bad-Name"].parse_error

    # The hash covers names and contents.
    before = board.hash
    (root / "views" / "board" / "styles.css").write_text("div { color: blue; }\n", encoding="utf-8")
    assert Project(root).load().views["board"].hash != before


def test_views_do_not_ship_to_executors(tmp_path):
    root = project(tmp_path, {"board": {"view.json": manifest(), "App.tsx": APP}})
    (root / "lib").mkdir(exist_ok=True)
    (root / "lib" / "views").mkdir()
    (root / "lib" / "views" / "x.py").write_text("X = 1\n", encoding="utf-8")
    shipped = {p.relative_to(root).as_posix() for p in bundle_files(root)}
    assert "nodes/prices.py" in shipped and "lib/views/x.py" in shipped
    assert not any(p.startswith("views/") for p in shipped)


# -- state ----------------------------------------------------------------------------
def test_state_follows_the_read_nodes_per_parameter_set(tmp_path):
    root = project(tmp_path, {"board": {"view.json": manifest(), "App.tsx": APP}})
    ws = Workspace(root)
    jan2, jan5 = {"as_of": "2026-01-02"}, {"as_of": "2026-01-05"}

    def state(params):
        v = next(v for v in ws.snapshot(params)["views"] if v["name"] == "board")
        return v["state"], v["reason"], {o["node"]: o for o in v["outputs"]}

    st, reason, _ = state(jan2)
    assert st == "missing" and reason == "no output yet: prices, universe"

    run(ws, ["prices", "universe"], params=jan2)
    st, reason, outs = state(jan2)
    assert (st, reason) == ("ready", None)
    assert outs["prices"]["state"] == "fresh" and outs["prices"]["kind"] == "table" and outs["prices"]["artifact_id"]
    # Another date: `universe` (no parameter) is shared, `prices` is not.
    st, reason, outs = state(jan5)
    assert (st, reason) == ("missing", "no output yet: prices")
    assert outs["universe"]["artifact_id"]

    (root / "nodes" / "universe.py").write_text(
        textwrap.dedent(UNIVERSE).lstrip().replace('"AAPL"', '"MSFT"'), encoding="utf-8"
    )
    ws.reload()
    st, reason, _ = state(jan2)
    assert st == "stale" and reason == "out of date: universe (stale)"
    # Node entries keep their shape; views are a separate list.
    snap = ws.snapshot(jan2)
    assert "outputs" not in snap["nodes"][0] and {"nodes", "views"} <= set(snap)


def test_a_broken_view_is_in_error_whatever_its_reads(tmp_path):
    root = project(tmp_path, {"v": {"view.json": json.dumps({"reads": ["nope"]}), "App.tsx": APP}})
    v = Workspace(root).snapshot()["views"][0]
    assert v["state"] == "error" and v["reason"] == "Reads unknown node(s): nope." and v["outputs"] == []


# -- HTTP ----------------------------------------------------------------------------
BAD_PATHS = [
    "../prices.py",
    "a/../../x.tsx",
    "/abs.tsx",
    "%2E%2E/x.tsx",
    "a//b.tsx",
    "a\\b.tsx",
    ".hidden.tsx",
    "x.py",
    "x.TSX",
    "x",
    "node_modules/x.js",
    "a/b/c/d/e/f/g/h/i.tsx",
    "C:/x.tsx",
]


@pytest.mark.parametrize("path", BAD_PATHS)
def test_bad_view_file_paths_are_refused(tmp_path, path):
    p = Project(tmp_path)
    with pytest.raises(ValueError):
        p.write_view_file("board", path, "x")
    with pytest.raises(ValueError):
        p.read_view_file("board", path)
    assert not any(f.is_file() for f in tmp_path.rglob("*"))


def test_view_routes(tmp_path):
    root = project(tmp_path)
    with TestClient(create_app(root)) as client:
        assert client.get("/api/views").json() == {"views": []}
        assert client.get("/api/project").json()["views"] == []

        r = client.put("/api/views/board", json={"title": "Board", "reads": ["prices"]})
        assert r.status_code == 200
        assert r.json()["state"] == "error" and r.json()["reason"] == "Entry file App.tsx does not exist."
        written = json.loads((root / "views" / "board" / "view.json").read_text(encoding="utf-8"))
        assert written == {"title": "Board", "description": "", "runtime": "react", "entry": "App.tsx", "reads": ["prices"]}

        r = client.put("/api/views/board/files/App.tsx", json={"source": APP})
        assert r.status_code == 200 and r.json()["state"] == "missing" and r.json()["files"] == ["App.tsx", "view.json"]
        r = client.put("/api/views/board/files/components/Chart.tsx", json={"source": "export {};\n"})
        assert r.json()["files"] == ["App.tsx", "components/Chart.tsx", "view.json"]

        got = client.get("/api/views/board").json()
        assert got["manifest"]["reads"] == ["prices"]
        assert {f["path"]: f["source"] for f in got["sources"]}["App.tsx"] == APP
        assert client.get("/api/views/board/files/App.tsx").json() == {"view": "board", "path": "App.tsx", "source": APP}
        listed = client.get("/api/views", params={"params": json.dumps({"as_of": "2026-01-05"})}).json()["views"]
        assert [v["name"] for v in listed] == ["board"]
        assert client.get("/api/project").json()["views"][0]["hash"] == listed[0]["hash"]

        # Refusals: bad names and paths are 400, missing things 404, nothing
        # written. HTTP clients resolve a literal `..` before sending (it never
        # reaches these routes), so traversal is sent encoded here.
        for path in BAD_PATHS:
            if "/../" in f"/{path}":
                continue
            assert client.put(f"/api/views/board/files/{path}", json={"source": "x"}).status_code in (400, 404), path
            assert client.get(f"/api/views/board/files/{path}").status_code in (400, 404), path
        for path in ("%2E%2E/x.tsx", "a/%2E%2E/%2E%2E/x.tsx", "%2E%2E%2Fx.tsx"):
            assert client.put(f"/api/views/board/files/{path}", json={"source": "x"}).status_code == 400, path
            assert client.get(f"/api/views/board/files/{path}").status_code == 400, path
        assert client.put("/api/views/Bad-Name/files/App.tsx", json={"source": APP}).status_code == 400
        assert client.put("/api/views/Bad-Name", json={}).status_code == 400
        assert client.get("/api/views/Bad-Name").status_code == 400
        big = "x" * (513 * 1024)
        assert client.put("/api/views/board/files/big.js", json={"source": big}).status_code == 400
        for bad in ({"reads": "prices"}, {"runtime": "vue"}, {"entry": "../x.tsx"}, {"entry": "x.css"}, {"color": 1}):
            assert client.put("/api/views/board", json=bad).status_code == 400, bad
        assert client.put("/api/views/board", json=["not", "an", "object"]).status_code in (400, 422)
        assert client.get("/api/views/nope").status_code == 404
        assert client.get("/api/views/board/files/missing.tsx").status_code == 404
        assert client.delete("/api/views/board/files/missing.tsx").status_code == 404
        assert client.delete("/api/views/nope").status_code == 404
        on_disk = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".exuvia" not in p.parts)
        assert on_disk == [
            "exuvia.json", "nodes/prices.py", "nodes/universe.py",
            "views/board/App.tsx", "views/board/components/Chart.tsx", "views/board/view.json",
        ]

        # An unknown read is accepted on write (the node may come later) and reported.
        r = client.put("/api/views/board", json={"reads": ["prices", "later"]})
        assert r.status_code == 200 and r.json()["reason"] == "Reads unknown node(s): later."
        assert r.json()["title"] == "board"

        r = client.delete("/api/views/board/files/components/Chart.tsx")
        assert r.status_code == 200 and r.json()["files"] == ["App.tsx", "view.json"]
        assert not (root / "views" / "board" / "components").exists()  # emptied dirs go too
        assert client.delete("/api/views/board").json() == {"deleted": "board"}
        assert not (root / "views" / "board").exists()
        assert client.get("/api/views").json() == {"views": []}


def test_view_writes_publish_a_graph_event(tmp_path):
    ws = Workspace(project(tmp_path))
    seen: list[str] = []
    ws.bus.publish = lambda ev: seen.append(ev["type"])  # type: ignore[method-assign]
    ws.write_view_manifest("board", {"reads": ["prices"]})
    ws.write_view_file("board", "App.tsx", APP)
    ws.delete_view_file("board", "App.tsx")
    ws.delete_view("board")
    assert seen == ["graph"] * 4


# -- revisions -------------------------------------------------------------------------
def test_view_files_are_versioned_and_restorable(tmp_path):
    root = project(tmp_path, {"board": {"view.json": manifest(), "App.tsx": APP}})
    with TestClient(create_app(root)) as client:
        v2 = APP.replace("hello", "hi")
        assert client.put("/api/views/board/files/App.tsx", json={"source": v2}, headers={"X-Exuvia-Origin": "ui"}).status_code == 200
        client.put("/api/views/other", json={})

        revs = client.get("/api/code/revisions", params={"view": "board"}).json()
        assert {r["view"] for r in revs} == {"board"} and {r["node"] for r in revs} == {None}
        app = [r for r in revs if r["path"] == "views/board/App.tsx"]
        assert [(r["origin"], r["current"]) for r in app] == [("ui", True), ("load", False)]
        assert client.get("/api/code/revisions", params={"view": "other"}).json()[0]["path"] == "views/other/view.json"

        old = app[1]["hash"]
        r = client.post("/api/views/board/restore", json={"path": "App.tsx", "hash": old})
        assert r.status_code == 200
        assert (root / "views" / "board" / "App.tsx").read_text(encoding="utf-8") == APP
        assert client.post("/api/views/board/restore", json={"path": "App.tsx", "hash": "0" * 16}).status_code == 404
        # A version of another file is not a version of this one.
        manifest_hash = next(r["hash"] for r in revs if r["path"] == "views/board/view.json")
        assert client.post("/api/views/board/restore", json={"path": "App.tsx", "hash": manifest_hash}).status_code == 404
        assert client.post("/api/views/board/restore", json={"path": "../x.tsx", "hash": old}).status_code == 400

        # Deleting the view is a revision per file; restoring brings it back.
        client.delete("/api/views/board")
        revs = client.get("/api/code/revisions", params={"view": "board"}).json()
        gone = {r["path"] for r in revs if r["hash"] is None and r["origin"] == "api"}
        assert gone == {"views/board/App.tsx", "views/board/view.json"}
        for rel in ("view.json", "App.tsx"):
            h = next(r["hash"] for r in revs if r["path"] == f"views/board/{rel}" and r["hash"])
            assert client.post("/api/views/board/restore", json={"path": rel, "hash": h}).status_code == 200
        board = client.get("/api/views/board").json()
        assert board["state"] == "missing" and board["files"] == ["App.tsx", "view.json"]
        assert client.get("/api/code/revisions", params={"view": "board"}).json()[0]["origin"] == "restore"


def test_a_database_from_before_views_gets_the_view_column(tmp_path, monkeypatch):
    monkeypatch.delenv("EXUVIA_DB", raising=False)
    root = tmp_path / "p"
    (root / ".exuvia").mkdir(parents=True)
    con = sqlite3.connect(root / ".exuvia" / "exuvia.db")
    con.executescript(
        """
        create table code_revisions (project text not null default '', id text not null, path text not null,
            node text, hash text, origin text not null, saved_at double precision not null,
            primary key (project, id));
        insert into code_revisions values ('', 'r1', 'nodes/old.py', 'old', null, 'api', 1);
        """
    )
    con.close()
    project(root, {"board": {"view.json": manifest(), "App.tsx": APP}})
    ws = Workspace(root)
    assert ws.code_revisions(node="old")[0]["view"] is None
    assert {r["path"] for r in ws.code_revisions(view="board")} == {"views/board/App.tsx", "views/board/view.json"}


# -- agent ---------------------------------------------------------------------------
def call(agent, name, args):
    async def go():
        agent.ws.bus.bind(asyncio.get_running_loop())
        return await agent._call(name, args)

    return asyncio.run(go())


def test_agent_view_tools(tmp_path):
    agent = Agent(Workspace(project(tmp_path)))

    ok, text, extra = call(agent, "write_view", {"name": "board", "title": "Board", "reads": ["prices"]})
    assert ok and json.loads(text)["state"] == "error" and extra == {"view": "board"}
    ok, text, _ = call(agent, "write_view_file", {"name": "board", "path": "App.tsx", "source": APP})
    assert ok and json.loads(text) == {
        "name": "board", "reads": ["prices"], "files": ["App.tsx", "view.json"], "state": "missing",
        "reason": "no output yet: prices",
    }
    ok, text, _ = call(agent, "list_views", {})
    assert ok and json.loads(text)[0]["title"] == "Board"
    ok, text, _ = call(agent, "read_view", {"name": "board"})
    assert ok and text.startswith("view board: missing") and text.index("--- view.json ---") < text.index("--- App.tsx ---")

    ok, text, _ = call(agent, "write_view_file", {"name": "board", "path": "../escape.tsx", "source": "x"})
    assert not ok and "relative to the view folder" in text
    ok, text, _ = call(agent, "write_view", {"name": "board", "reads": "prices"})
    assert not ok and "`reads`" in text
    ok, text, _ = call(agent, "read_view", {"name": "nope"})
    assert not ok and text == "not found: nope"

    call(agent, "write_view_file", {"name": "board", "path": "App.tsx", "source": APP.replace("hello", "hi")})
    ok, text, extra = call(agent, "list_revisions", {"view": "board"})
    revs = json.loads(text)
    assert ok and extra == {"view": "board"} and {r["origin"] for r in revs} == {"agent"}
    first = [r for r in revs if r["path"] == "views/board/App.tsx"][-1]["hash"]
    ok, _, _ = call(agent, "restore_view_file", {"name": "board", "path": "App.tsx", "hash": first})
    assert ok and agent.ws.project.read_view_file("board", "App.tsx") == APP
    ok, text, _ = call(agent, "list_revisions", {})
    assert not ok and "either" in text
    ok, text, _ = call(agent, "list_revisions", {"name": "prices"})
    assert ok  # nodes as before

    ok, _, _ = call(agent, "delete_view_file", {"name": "board", "path": "App.tsx"})
    assert ok and agent.ws.project.view_files("board") == ["view.json"]
    ok, text, _ = call(agent, "delete_view", {"name": "board"})
    assert ok and json.loads(text) == {"deleted": "board"}

    names = {t["name"] for t in TOOLS}
    assert {"list_views", "read_view", "write_view", "write_view_file", "delete_view_file", "delete_view", "restore_view_file"} <= names
    guide = agent.instructions()
    assert "## Views" in guide and "only reads" in guide and "not available yet" not in guide
    assert "useTable(node)" in guide and "`vega-embed`" in guide


def test_guide_lists_the_view_tools(tmp_path):
    with TestClient(create_app(project(tmp_path))) as client:
        guide = client.get("/api/guide").json()
    assert {"list_views", "read_view", "write_view", "write_view_file", "delete_view_file"} <= {t["name"] for t in guide["tools"]}
    assert "## Views" in guide["instructions"]


def test_the_views_runtime_is_served_under_the_api(tmp_path, monkeypatch):
    import exuvia.server as server

    static = tmp_path / "static"
    monkeypatch.setattr(server, "STATIC", static)
    with TestClient(create_app(project(tmp_path / "p"))) as client:
        missing = client.get("/api/view-runtime/runtime.js")
        assert missing.status_code == 404 and "not built" in missing.json()["detail"]
        # Only the runtime, no UI: a headless install (the UI routes stay off).
        (static / "view-runtime").mkdir(parents=True)
        (static / "view-runtime" / "runtime.js").write_text("/*rt*/", encoding="utf-8")
        got = client.get("/api/view-runtime/runtime.js")
        assert got.status_code == 200 and got.text == "/*rt*/"
        assert got.headers["content-type"].startswith("text/javascript")
    project(tmp_path / "templates" / "tpl")
    with TestClient(create_hub_app(tmp_path / "hub", tmp_path / "templates")) as hub:
        assert hub.post("/api/projects", json={"id": "a", "template": "tpl"}).status_code == 200
        assert hub.get("/p/a/api/view-runtime/runtime.js").text == "/*rt*/"
        assert hub.get("/p/nope/api/view-runtime/runtime.js").status_code == 404


# -- file watcher ------------------------------------------------------------------------
def test_editing_view_files_on_disk_is_seen(tmp_path):
    root = project(tmp_path, {"board": {"view.json": manifest(), "App.tsx": APP}})
    ws = Workspace(root)

    async def go():
        await ws.start()
        try:
            await asyncio.sleep(0.5)  # let the watcher start
            (root / "views" / "board" / "App.tsx").write_text(APP.replace("hello", "edited"), encoding="utf-8")
            (root / "views" / "fresh").mkdir()
            (root / "views" / "fresh" / "view.json").write_text("{}", encoding="utf-8")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                revs = ws.code_revisions(view="board")
                if "fresh" in ws.graph.views and revs and revs[0]["origin"] == "file":
                    return revs
                await asyncio.sleep(0.1)
            return ws.code_revisions(view="board")
        finally:
            await ws.stop()

    revs = asyncio.run(go())
    assert revs[0]["origin"] == "file" and revs[0]["path"] == "views/board/App.tsx"
    assert "fresh" in ws.graph.views
    assert [r["origin"] for r in ws.code_revisions(view="fresh")] == ["file"]


def test_stopping_waits_for_a_reload_the_watcher_started(tmp_path):
    # Hub.apply_template deletes views/ right after stop(); on Windows a file
    # that the watcher's reload still has open cannot be deleted.
    root = project(tmp_path, {"board": {"view.json": manifest(), "App.tsx": APP}})
    ws = Workspace(root)
    started, finished = threading.Event(), threading.Event()
    real_reload = ws.reload

    def slow_reload():
        started.set()
        time.sleep(0.5)
        real_reload()
        finished.set()

    ws.reload = slow_reload

    async def go():
        await ws.start()
        try:
            await asyncio.sleep(0.5)  # let the watcher start
            (root / "views" / "board" / "App.tsx").write_text(APP.replace("hello", "edited"), encoding="utf-8")
            assert await asyncio.to_thread(started.wait, 15)
        finally:
            await ws.stop()
        return finished.is_set()

    assert asyncio.run(go())


# -- hub -------------------------------------------------------------------------------------
def test_hub_templates_and_exports_carry_views(tmp_path):
    tpl = project(
        tmp_path / "templates" / "tpl",
        {"board": {"view.json": manifest(), "App.tsx": APP, "parts/Old.tsx": "export {};\n"}},
    )
    with TestClient(create_hub_app(tmp_path / "hub", tmp_path / "templates")) as hub:
        assert hub.post("/api/projects", json={"id": "a", "template": "tpl"}).status_code == 200
        views = hub.get("/p/a/api/views").json()["views"]
        assert [v["name"] for v in views] == ["board"]
        assert views[0]["files"] == ["App.tsx", "parts/Old.tsx", "view.json"]

        # The project grows a view of its own and edits the template's.
        hub.put("/p/a/api/views/mine", json={})
        hub.put("/p/a/api/views/board/files/App.tsx", json={"source": APP.replace("hello", "local")})

        # The template moves on: Old.tsx goes, a file and a view are added.
        (tpl / "views" / "board" / "parts" / "Old.tsx").unlink()
        (tpl / "views" / "board" / "parts" / "New.tsx").write_text("export {};\n", encoding="utf-8")
        project(tpl, {"second": {"view.json": "{}", "App.tsx": APP}})
        assert hub.put("/api/projects/a/template", json={"template": "tpl"}).status_code == 200

        views = {v["name"]: v for v in hub.get("/p/a/api/views").json()["views"]}
        assert sorted(views) == ["board", "second"]  # views not in the template are removed, like nodes
        assert views["board"]["files"] == ["App.tsx", "parts/New.tsx", "view.json"]
        assert hub.get("/p/a/api/views/board/files/App.tsx").json()["source"] == APP
        revs = hub.get("/p/a/api/code/revisions", params={"view": "mine"}).json()
        assert revs[0]["hash"] is None and revs[0]["origin"] == "template"  # restorable

        # Export / import round-trips them.
        blob = hub.get("/api/hub/export").content
        names = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz").getnames()
        assert "a/views/board/parts/New.tsx" in names
        assert hub.delete("/api/projects/a").status_code == 200
        r = hub.post("/api/hub/import", content=blob)
        assert r.status_code == 200 and r.json()["projects"] == ["a"]
        views = {v["name"]: v for v in hub.get("/p/a/api/views").json()["views"]}
        assert sorted(views) == ["board", "second"] and views["board"]["files"] == ["App.tsx", "parts/New.tsx", "view.json"]


@pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
def test_a_symlink_out_of_a_view_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = project(tmp_path / "p", {"board": {"view.json": manifest(), "App.tsx": APP}})
    os.symlink(outside, root / "views" / "board" / "escape", target_is_directory=True)
    p = Project(root)
    with pytest.raises(ValueError, match="outside"):
        p.write_view_file("board", "escape/x.tsx", "x")
    assert not (outside / "x.tsx").exists()
    assert "escape" not in " ".join(p.view_files("board"))


def test_acting_as_labels_view_revisions(tmp_path):
    ws = Workspace(project(tmp_path))
    with acting_as("mcp"):
        ws.write_view_manifest("board", {})
    assert ws.code_revisions(view="board")[0]["origin"] == "mcp"


# -- what a view may read ---------------------------------------------------------
def _arrow_rows(resp) -> list[dict]:
    import pyarrow as pa

    return pa.ipc.open_stream(resp.content).read_all().to_pylist()


def test_a_view_reads_only_its_declared_nodes(tmp_path):
    """The view-scoped routes are the only data paths the views runtime uses:
    a node the manifest does not list in ``reads`` is not there at all."""
    from exuvia.arrowstream import MEDIA_TYPE

    root = project(tmp_path, {"board": {"view.json": manifest(reads=["prices"]), "App.tsx": APP}})
    jan2 = {"as_of": "2026-01-02"}
    with TestClient(create_app(root)) as client:
        ws = client.app.state.ws
        run(ws, ["prices", "universe"], params=jan2)
        run(ws, ["prices"], params={"as_of": "2026-01-05"})
        other = Path(ws.artifact_path(ws.store.last_success("universe", ws.node_params("universe", jan2)[1])))
        assert other.is_file()

        q = lambda sql, **kw: client.post("/api/views/board/query", json={"sql": sql, "params": jan2}, **kw)  # noqa: E731
        r = q("select day, px from prices")
        assert r.status_code == 200 and r.json()["rows"] == [["2026-01-02", 1.0]]
        r = q("select _as_of, day from prices__runs order by 1")
        assert r.json()["rows"] == [["2026-01-02", "2026-01-02"], ["2026-01-05", "2026-01-05"]]
        r = q("select * from prices", headers={"Accept": MEDIA_TYPE})
        assert r.headers["content-type"].startswith(MEDIA_TYPE) and _arrow_rows(r) == [{"day": "2026-01-02", "px": 1.0}]
        # The project-wide route still sees everything.
        assert client.post("/api/query", json={"sql": "select * from universe"}).status_code == 200

        # Undeclared node: not a table in this connection.
        r = q("select * from universe")
        assert r.status_code == 400 and "universe" in r.json()["detail"]
        # Nor can the SQL reach its file, any other file, or undo the lock.
        posix = other.as_posix()
        for sql in (
            f"select * from read_parquet('{other}')",
            f"select * from read_parquet('{posix}')",
            f"select * from read_csv('{(root / 'exuvia.json').as_posix()}')",
            "select * from glob('**')",
            f"copy (select 1) to '{(tmp_path / 'leak.csv').as_posix()}'",
            f"attach '{(tmp_path / 'x.duckdb').as_posix()}' as x",
            "set enable_external_access = true",
            "set lock_configuration = false",
            "set allowed_paths = ['/']",
            "install httpfs",
        ):
            r = q(sql)
            assert r.status_code == 400, (sql, r.json())
        assert not (tmp_path / "leak.csv").exists() and not (tmp_path / "x.duckdb").exists()

        # Node data: the declared node as JSON or Arrow, the other refused.
        r = client.get("/api/views/board/data/prices", params={"params": json.dumps(jan2)})
        assert r.status_code == 200 and r.json()["rows"] == [["2026-01-02", 1.0]]
        r = client.get("/api/views/board/data/prices", params={"params": json.dumps(jan2)}, headers={"Accept": MEDIA_TYPE})
        assert _arrow_rows(r) == [{"day": "2026-01-02", "px": 1.0}]
        r = client.get("/api/views/board/data/universe")
        assert r.status_code == 403 and "reads" in r.json()["detail"]
        assert client.get("/api/views/board/data/nope").status_code == 403
        assert client.get("/api/views/nope/data/prices").status_code == 404
        assert client.post("/api/views/nope/query", json={"sql": "select 1"}).status_code == 404

        # A view whose reads name no existing node still gets a (locked, empty) connection.
        client.put("/api/views/empty", json={"reads": ["later"]})
        assert client.post("/api/views/empty/query", json={"sql": "select 42"}).json()["rows"] == [[42]]
        assert client.post("/api/views/empty/query", json={"sql": f"select * from read_parquet('{posix}')"}).status_code == 400
