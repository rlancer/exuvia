"""Code revisions, and the database adapters behind every record.

The store tests run on each backend: a per-project SQLite file, a shared
SQLite file (``EXUVIA_DB=sqlite:///...``) and, when
``EXUVIA_TEST_POSTGRES_URL`` is set, Postgres.
"""

import asyncio
import json
import os
import sqlite3
import tarfile
import uuid

import pytest
from fastapi.testclient import TestClient

from exuvia.agent import Agent
from exuvia.db import _pg_placeholders, close_shared
from exuvia.hub import _shared_store
from exuvia.server import create_app, create_hub_app
from exuvia.workspace import Workspace, acting_as

V1 = '''import exuvia


@exuvia.node()
def nums():
    """Version one."""
    import pandas as pd

    return pd.DataFrame({"n": [1, 2, 3]})
'''
V2 = V1.replace("Version one", "Version two").replace("[1, 2, 3]", "[4, 5]")

PG_URL = os.environ.get("EXUVIA_TEST_POSTGRES_URL", "")
BACKENDS = ["sqlite-project", "sqlite-shared"] + (["postgres"] if PG_URL else [])


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path, monkeypatch):
    """Point EXUVIA_DB at the backend under test, isolated per test."""
    close_shared()
    if request.param == "sqlite-shared":
        monkeypatch.setenv("EXUVIA_DB", f"sqlite:///{(tmp_path / 'shared' / 'exuvia.db').as_posix()}")
    elif request.param == "postgres":
        monkeypatch.setenv("EXUVIA_DB", PG_URL)
    else:
        monkeypatch.delenv("EXUVIA_DB", raising=False)
    monkeypatch.setenv("EXUVIA_DB_SCOPE", f"t{uuid.uuid4().hex[:8]}/")
    yield request.param
    close_shared()


def write(ws: Workspace, name: str, source: str, origin: str = "api") -> None:
    with acting_as(origin):
        ws.project.write_node(name, source, "python")
        ws.reload()


def test_load_records_every_versioned_file(backend, tmp_path):
    root = tmp_path / "p"
    (root / "nodes").mkdir(parents=True)
    (root / "nodes" / "nums.py").write_text(V1, encoding="utf-8")
    (root / "nodes" / "_helpers.py").write_text("X = 1\n", encoding="utf-8")
    (root / "exuvia.json").write_text('{"name": "p"}\n', encoding="utf-8")
    (root / "AGENTS.md").write_text("notes\n", encoding="utf-8")
    (root / "lib").mkdir()
    (root / "lib" / "stats.py").write_text("def mean(xs):\n    return sum(xs) / len(xs)\n", encoding="utf-8")

    ws = Workspace(root)
    revs = {r["path"]: r for r in ws.code_revisions()}
    assert set(revs) == {"nodes/nums.py", "nodes/_helpers.py", "lib/stats.py", "exuvia.json", "AGENTS.md"}
    assert {r["origin"] for r in revs.values()} == {"load"}
    assert revs["nodes/nums.py"]["node"] == "nums" and revs["nodes/_helpers.py"]["node"] is None
    assert all(r["current"] for r in revs.values())
    # The hash is the one runs record, so it resolves to the source.
    assert revs["nodes/nums.py"]["hash"] == ws.graph.nodes["nums"].code_hash
    assert ws.code_source(revs["nodes/nums.py"]["hash"]) == V1

    # Loading again with nothing changed records nothing.
    ws.store.close()
    again = Workspace(root)
    assert len(again.code_revisions()) == 5


def test_every_save_is_a_revision_and_restore_brings_one_back(backend, tmp_path):
    ws = Workspace(tmp_path / "p")
    write(ws, "nums", V1, "ui")
    write(ws, "nums", V2, "mcp")
    ws.reload()  # the file watcher seeing the same content adds nothing

    revs = ws.code_revisions(node="nums")
    assert [r["origin"] for r in revs] == ["mcp", "ui"]
    assert [r["current"] for r in revs] == [True, False]
    old = revs[1]["hash"]

    ws.restore_node("nums", old)
    assert ws.project.source("nums") == V1
    revs = ws.code_revisions(node="nums")
    assert revs[0]["origin"] == "restore" and revs[0]["hash"] == old and revs[0]["current"]
    assert len(revs) == 3  # history only grows

    with pytest.raises(KeyError):
        ws.restore_node("nums", "0" * 16)


def test_a_deleted_node_is_recorded_and_can_come_back(backend, tmp_path):
    ws = Workspace(tmp_path / "p")
    write(ws, "nums", V1)
    with acting_as("api"):
        ws.project.delete_node("nums")
        ws.reload()

    revs = ws.code_revisions(node="nums")
    assert revs[0]["hash"] is None and revs[0]["origin"] == "api"
    ws.restore_node("nums", revs[1]["hash"])
    assert "nums" in ws.graph.nodes and ws.project.source("nums") == V1


def test_revisions_saved_in_the_same_instant_read_back_in_the_order_written(backend, tmp_path):
    # time.time() can return the same value twice (15.6 ms steps on Windows
    # before Python 3.13), so saved_at ties; the newest write must still win.
    ws = Workspace(tmp_path / "p")
    path = "nodes/nums.py"
    for i in range(20):
        ws.store.record_code([(path, "nums", None, f"h{i}", f"v{i}")], "api", now=1000.0)
        assert ws.store.latest_code()[path] == f"h{i}"
    revs = ws.store.code_revisions(path=path)
    assert [r["hash"] for r in revs] == [f"h{i}" for i in reversed(range(20))]

    # Paging oldest first (exuvia mirror) keeps that order across the ties.
    ws.store.record_code([(path, "nums", None, "h20", "v20")], "api", now=999.0)
    pages, after = [], None
    while page := ws.store.code_revisions_after(after, 3):
        pages += page
        after = page[-1]["id"]
    assert [r["hash"] for r in pages] == ["h20", *(f"h{i}" for i in range(20))]
    assert pages[0]["view"] is None
    with pytest.raises(KeyError):
        ws.store.code_revisions_after("no-such-id")


def test_config_changes_are_versioned(backend, tmp_path):
    ws = Workspace(tmp_path / "p")
    with acting_as("ui"):
        ws.set_settings(name="Renamed")
    revs = ws.code_revisions()
    assert revs[0]["path"] == "exuvia.json" and revs[0]["origin"] == "ui"
    assert json.loads(ws.code_source(revs[0]["hash"]))["name"] == "Renamed"


def test_unclaimed_changes_are_file_edits(backend, tmp_path):
    ws = Workspace(tmp_path / "p")
    (ws.project.nodes_dir / "nums.py").write_text(V1, encoding="utf-8")
    ws.reload()  # what the file watcher does
    assert ws.code_revisions(node="nums")[0]["origin"] == "file"


def test_agent_writes_are_the_agents(backend, tmp_path):
    agent = Agent(Workspace(tmp_path / "p"))

    async def go():
        agent.ws.bus.bind(asyncio.get_running_loop())
        with acting_as("api"):  # the request that started the turn
            ok, _, _ = await agent._call("write_node", {"name": "nums", "kind": "python", "source": V1})
            assert ok
            ok, _, _ = await agent._call("write_node", {"name": "nums", "kind": "python", "source": V2})
            assert ok
            ok, text, _ = await agent._call("list_revisions", {"name": "nums"})
            assert ok
            revs = json.loads(text)
            ok, src, _ = await agent._call("read_revision", {"hash": revs[1]["hash"]})
            assert ok and src == V1
            ok, _, _ = await agent._call("restore_node", {"name": "nums", "hash": revs[1]["hash"]})
            assert ok

    asyncio.run(go())
    assert [r["origin"] for r in agent.ws.code_revisions(node="nums")] == ["restore", "agent", "agent"]
    assert agent.ws.project.source("nums") == V1


def test_a_run_links_to_the_exact_code_it_ran(backend, tmp_path):
    ws = Workspace(tmp_path / "p")
    write(ws, "nums", V1)

    async def go():
        await ws.start()
        try:
            return await ws.run(["nums"], force={"nums"})
        finally:
            await ws.stop()

    finals = asyncio.run(go())
    run_id = finals["nums"]["id"]
    write(ws, "nums", V2)

    out = ws.run_source(run_id)
    assert out["source"] == V1 and out["code_current"] is False
    assert ws.history("nums")[0]["code_hash"] == out["code_hash"]
    assert ws.run_source("nope") is None


def test_http_routes_record_the_callers_origin(backend, tmp_path):
    with TestClient(create_app(tmp_path / "p")) as client:
        r = client.put("/api/nodes/nums", json={"source": V1, "kind": "python"}, headers={"X-Exuvia-Origin": "mcp"})
        assert r.status_code == 200
        assert client.put("/api/nodes/nums", json={"source": V2}).status_code == 200

        revs = client.get("/api/code/revisions", params={"node": "nums"}).json()
        assert [r["origin"] for r in revs] == ["api", "mcp"]
        old = revs[1]["hash"]
        assert client.get(f"/api/code/blobs/{old}").json() == {"hash": old, "source": V1}
        assert client.get("/api/code/blobs/0000").status_code == 404

        assert client.post("/api/nodes/nums/restore", json={"hash": old}).status_code == 200
        assert client.get("/api/nodes/nums").json()["source"] == V1
        assert client.post("/api/nodes/nums/restore", json={"hash": "0000"}).status_code == 404
        assert client.get("/api/runs/nope/source").status_code == 404

        # A bad origin header is not stored as given.
        client.put("/api/nodes/nums", json={"source": V2}, headers={"X-Exuvia-Origin": "<script>"})
        assert client.get("/api/code/revisions").json()[0]["origin"] == "api"


def test_hub_labels_template_updates_and_scopes_projects(backend, tmp_path):
    t = tmp_path / "templates" / "tpl"
    (t / "nodes").mkdir(parents=True)
    (t / "exuvia.json").write_text('{"name": "tpl"}\n', encoding="utf-8")
    (t / "nodes" / "nums.py").write_text(V1, encoding="utf-8")

    with TestClient(create_hub_app(tmp_path / "hub", tmp_path / "templates")) as hub:
        for pid in ("a", "b"):
            assert hub.post("/api/projects", json={"id": pid, "template": "tpl"}).status_code == 200
        hub.put("/p/a/api/nodes/nums", json={"source": V2})

        (t / "nodes" / "nums.py").write_text(V1.replace("one", "three"), encoding="utf-8")
        assert hub.put("/api/projects/b/template", json={"template": "tpl"}).status_code == 200

        a = hub.get("/p/a/api/code/revisions", params={"node": "nums"}).json()
        b = hub.get("/p/b/api/code/revisions", params={"node": "nums"}).json()
        assert [r["origin"] for r in a] == ["api", "load"]
        assert [r["origin"] for r in b] == ["template", "load"]

        export = hub.get("/api/hub/export")
        names = tarfile.open(fileobj=__import__("io").BytesIO(export.content), mode="r:gz").getnames()
        assert ("a/.exuvia/exuvia.db" in names) == (backend == "sqlite-project")

        assert hub.delete("/api/projects/a").status_code == 200
        shared = _shared_store("a")
        if shared is not None:  # a deleted project's rows leave the shared database too
            assert shared.code_revisions() == [] and shared.code_source(a[0]["hash"]) is None
        assert len(hub.get("/p/b/api/code/revisions", params={"node": "nums"}).json()) == 2


def test_a_database_from_before_scopes_is_upgraded_in_place(tmp_path, monkeypatch):
    monkeypatch.delenv("EXUVIA_DB", raising=False)
    root = tmp_path / "p"
    (root / ".exuvia").mkdir(parents=True)
    (root / "nodes").mkdir()
    con = sqlite3.connect(root / ".exuvia" / "exuvia.db")
    con.executescript(
        """
        create table runs (id text primary key, node text not null, batch_id text, trigger text not null,
            status text not null, queued_at real not null, started_at real, finished_at real, code_hash text,
            inputs text, artifact_id text, artifact_path text, kind text, rows integer, schema text,
            log text default '', error text);
        create index runs_node on runs(node, queued_at desc);
        create table schedules (node text primary key, paused integer not null default 0);
        create table schedule_state (node text primary key, cron text not null, last_fired real not null);
        create table agent_sessions (id text primary key, history text not null, events text not null,
            updated_at real not null);
        create table stored_artifacts (path text primary key);
        insert into runs (id, node, trigger, status, queued_at, finished_at, code_hash)
            values ('r1', 'nums', 'manual', 'success', 1, 2, 'abc');
        insert into schedules values ('nums', 1);
        insert into stored_artifacts values ('.exuvia/artifacts/nums/x.parquet');
        insert into agent_sessions values ('s1', '[]', '[]', 3);
        """
    )
    con.close()

    ws = Workspace(root)
    assert ws.store.run("r1")["code_hash"] == "abc"
    assert ws.store.paused() == {"nums"}
    assert ws.store.stored_paths() == {".exuvia/artifacts/nums/x.parquet"}
    assert ws.store.agent_session("s1")["updated_at"] == 3
    assert ws.store.agent_sessions() == [{"id": "s1", "title": "", "created_at": 3, "updated_at": 3, "started_by": None}]
    assert ws.store.run("r1")["params_key"] == ""
    ws.store.close()

    con = sqlite3.connect(root / ".exuvia" / "exuvia.db")
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    assert not any(t.endswith("_legacy") for t in tables)
    assert {"code_blobs", "code_revisions"} <= tables


@pytest.mark.skipif(not PG_URL, reason="needs EXUVIA_TEST_POSTGRES_URL")
def test_processes_creating_the_postgres_schema_at_once_all_succeed():
    """Postgres does not make ``create table if not exists`` atomic: two
    connections creating the same table in an empty schema at once can fail
    with a duplicate ``pg_type`` key. Several hub replicas starting together on
    a new database do exactly that, so every one of them has to come up."""
    import threading

    from exuvia.db import PostgresDatabase
    from exuvia.store import TABLES, Store

    admin = PostgresDatabase(PG_URL)
    sep = "&" if "?" in PG_URL else "?"
    try:
        for _ in range(5):  # the race is timing-dependent: try it a few times
            schema = f"race_{uuid.uuid4().hex[:12]}"
            admin.query(f"create schema {schema}")
            url = f"{PG_URL}{sep}options=-csearch_path%3D{schema}"
            dbs = [PostgresDatabase(url) for _ in range(8)]
            start = threading.Barrier(len(dbs))
            errors: list[BaseException] = []

            def open_store(db):
                start.wait()
                try:
                    Store(db, scope="s", owns_db=False)
                except BaseException as e:  # noqa: BLE001 - reported below
                    errors.append(e)

            threads = [threading.Thread(target=open_store, args=(db,)) for db in dbs]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            try:
                assert errors == []
                assert set(TABLES) <= {r["table_name"] for r in admin.query(
                    "select table_name from information_schema.tables where table_schema = ?", (schema,)
                )}
            finally:
                for db in dbs:
                    db.close()
                admin.query(f"drop schema {schema} cascade")
    finally:
        admin.close()


def test_postgres_placeholders():
    assert _pg_placeholders("select * from t where a=? and b like 'x%'") == (
        "select * from t where a=%s and b like 'x%%'"
    )


def test_view_files_are_versioned_per_view(backend, tmp_path):
    ws = Workspace(tmp_path / "p")
    with acting_as("agent"):
        ws.write_view_manifest("board", {"reads": []})
        ws.write_view_file("board", "App.tsx", "export default () => 1;\n")
        ws.write_view_file("board", "App.tsx", "export default () => 2;\n")
        ws.write_view_manifest("other", {})
        ws.delete_view("board")
    revs = ws.code_revisions(view="board")
    assert {r["view"] for r in revs} == {"board"} and len(revs) == 5
    assert {r["path"] for r in revs if r["hash"] is None} == {"views/board/App.tsx", "views/board/view.json"}
    first = [r for r in revs if r["path"] == "views/board/App.tsx"][-1]["hash"]
    ws.restore_view_file("board", "App.tsx", first)
    assert ws.project.read_view_file("board", "App.tsx") == "export default () => 1;\n"
    assert ws.code_revisions(view="board")[0]["origin"] == "restore"
    assert [r["path"] for r in ws.code_revisions(view="other")] == ["views/other/view.json"]


def test_helper_modules_are_versioned(backend, tmp_path):
    ws = Workspace(tmp_path / "p")
    with acting_as("agent"):
        ws.write_helper("lib/stats.py", "def mean(xs):\n    return sum(xs) / len(xs)\n")
        ws.write_helper("lib/stats.py", "def mean(xs):\n    return sum(xs) / max(len(xs), 1)\n")
        ws.delete_helper("lib/stats.py")
    revs = [r for r in ws.code_revisions() if r["path"] == "lib/stats.py"]
    assert [r["origin"] for r in revs] == ["agent"] * 3
    assert revs[0]["hash"] is None and "max(len(xs), 1)" in ws.code_source(revs[1]["hash"])


def test_a_slow_api_edit_is_not_recorded_as_a_hand_edit(tmp_path, monkeypatch):
    """The file watcher must not record an API or agent write as a hand edit
    ("file") while the request is still between writing the file and its own
    reload. Here the write is made slow on purpose: the watcher sees the file
    change long before the request reloads."""
    import time

    with TestClient(create_app(tmp_path / "p")) as client:
        ws = client.app.state.ws
        write = ws.project.write_node

        def slow_write(*a, **k):
            write(*a, **k)
            time.sleep(1.0)  # well past the watcher's debounce

        monkeypatch.setattr(ws.project, "write_node", slow_write)
        r = client.put("/api/nodes/nums", json={"source": V1, "kind": "python"}, headers={"X-Exuvia-Origin": "ui"})
        assert r.status_code == 200
        time.sleep(0.5)  # let the watcher catch up
        assert [r["origin"] for r in ws.code_revisions(node="nums")] == ["ui"]


def test_agent_sessions_are_listed_latest_first_per_project(backend, tmp_path):
    """The list of past chats: titled by the first question, kept on later
    saves, filtered by who started each one, and scoped to its project."""
    import time

    a, b = Workspace(tmp_path / "a"), Workspace(tmp_path / "b")
    a.store.save_agent_session("s1", [], [{"type": "user", "text": "first\nquestion"}], "alice", "u1")
    time.sleep(0.01)
    a.store.save_agent_session("s2", [], [{"type": "user", "text": "second"}], "bob", "u2")
    time.sleep(0.01)
    a.store.save_agent_session("s1", [], [{"type": "user", "text": "first\nquestion"}, {"type": "user", "text": "more"}])
    rows = a.store.agent_sessions()
    assert [(r["id"], r["title"], r["started_by"]) for r in rows] == [("s1", "first question", "alice"), ("s2", "second", "bob")]
    assert rows[0]["created_at"] < rows[0]["updated_at"]
    assert [r["id"] for r in a.store.agent_sessions(started_by_user="u2")] == ["s2"]
    assert [r["id"] for r in a.store.agent_sessions(started_by="alice")] == ["s1"]
    assert [r["id"] for r in a.store.agent_sessions(limit=1)] == ["s1"]
    if backend != "sqlite-project":  # one database for both projects: each lists its own
        assert b.store.agent_sessions() == []
    a.store.close()
    b.store.close()


def test_a_tag_names_the_whole_projects_code_at_one_save(backend, tmp_path):
    ws = Workspace(tmp_path / "p")
    write(ws, "nums", V1)
    write(ws, "other", V1)
    tag = ws.create_tag("v1", "  before the change  ")
    assert tag["name"] == "v1" and tag["message"] == "before the change"
    write(ws, "nums", V2)
    with acting_as("api"):
        ws.project.delete_node("other")
        ws.reload()
    write(ws, "fresh", V1)

    got = ws.code_tag("v1")
    status = {f["path"]: f["status"] for f in got["files"]}
    assert status["nodes/nums.py"] == "changed"
    assert status["nodes/other.py"] == "deleted"
    assert status["nodes/fresh.py"] == "added"
    assert ws.tag_source("v1", "nodes/nums.py") == V1
    assert ws.tag_source("v1", "nodes/other.py") == V1  # gone now, kept by the tag
    with pytest.raises(KeyError):
        ws.tag_source("v1", "nodes/fresh.py")
    [listed] = ws.code_tags()
    assert listed["name"] == "v1" and listed["changed_since"] == 3 and listed["files"] == 2

    # A tag's version of a node restores like any other.
    deleted = next(f for f in got["files"] if f["path"] == "nodes/other.py")
    ws.restore_node("other", deleted["hash"])
    assert ws.project.source("other") == V1

    # A name is never moved: delete it to use it again.
    with pytest.raises(ValueError, match="already a tag v1"):
        ws.create_tag("v1")
    for bad in ("", "-x", "a b", "x/y", "a..b", "v1.lock", "x" * 65, "end."):
        with pytest.raises(ValueError, match="not a tag name"):
            ws.create_tag(bad)
    ws.delete_tag("v1")
    assert ws.code_tags() == []
    with pytest.raises(KeyError):
        ws.delete_tag("v1")
    with pytest.raises(KeyError):
        ws.code_tag("v1")


def test_a_tag_at_a_revision_takes_its_whole_save(backend, tmp_path):
    ws = Workspace(tmp_path / "p")
    from exuvia.project import _hash

    ws.store.record_code(
        [("nodes/a.py", "a", None, _hash(V1), V1), ("nodes/b.py", "b", None, _hash(V1), V1)], "template", now=100.0
    )
    ws.store.record_code([("nodes/a.py", "a", None, _hash(V2), V2)], "api", now=200.0)
    first = ws.store.code_revisions_after(None)[0]  # a.py, the first file of the template's save
    tag = ws.create_tag("start", revision=first["id"])
    assert set(ws.store.code_at(tag["revision"])) == {"nodes/a.py", "nodes/b.py"}
    assert ws.tag_source("start", "nodes/a.py") == V1
    with pytest.raises(KeyError, match="no revision"):
        ws.create_tag("nope", revision="0" * 32)


def test_tag_tools_and_routes(backend, tmp_path):
    with TestClient(create_app(tmp_path / "p")) as client:
        assert client.post("/api/code/tags", json={"name": "v0"}).status_code == 404  # no code yet
        client.put("/api/nodes/nums", json={"source": V1, "kind": "python"})
        r = client.post("/api/code/tags", json={"name": "v1", "message": "shipped"})
        assert r.status_code == 200 and r.json()["message"] == "shipped"
        assert client.post("/api/code/tags", json={"name": "v1"}).status_code == 409
        assert client.post("/api/code/tags", json={"name": "a b"}).status_code == 400
        client.put("/api/nodes/nums", json={"source": V2})

        [tag] = client.get("/api/code/tags").json()
        assert tag["name"] == "v1" and tag["changed_since"] == 1
        files = client.get("/api/code/tags/v1").json()["files"]
        assert [(f["path"], f["status"]) for f in files] == [("nodes/nums.py", "changed")]
        src = client.get("/api/code/tags/v1", params={"path": "nodes/nums.py"}).json()["source"]
        assert src == V1
        assert client.get("/api/code/tags/v1", params={"path": "nodes/x.py"}).status_code == 404
        assert client.get("/api/code/tags/nope").status_code == 404
        assert client.delete("/api/code/tags/v1").json() == {"ok": True, "deleted": "v1"}
        assert client.delete("/api/code/tags/v1").status_code == 404

    agent = Agent(Workspace(tmp_path / "q"))

    async def go():
        agent.ws.bus.bind(asyncio.get_running_loop())
        ok, text, _ = await agent._call("list_tags", {})
        assert ok and "no tags yet" in text
        with acting_as("api"):
            await agent._call("write_node", {"name": "nums", "kind": "python", "source": V1})
        ok, text, _ = await agent._call("create_tag", {"name": "v1", "message": "first"})
        assert ok and json.loads(text)["name"] == "v1"
        ok, text, _ = await agent._call("create_tag", {"name": "v1"})
        assert not ok and "already" in text
        ok, text, _ = await agent._call("list_tags", {})
        assert ok and json.loads(text)[0]["message"] == "first"
        ok, text, _ = await agent._call("read_tag", {"name": "v1"})
        assert ok and json.loads(text)["files"][0]["status"] == "same"
        ok, text, _ = await agent._call("read_tag", {"name": "v1", "path": "nodes/nums.py"})
        assert ok and text == V1
        ok, text, _ = await agent._call("delete_tag", {"name": "v1"})
        assert ok
        ok, text, _ = await agent._call("read_tag", {"name": "v1"})
        assert not ok and "no tag v1" in text

    asyncio.run(go())
