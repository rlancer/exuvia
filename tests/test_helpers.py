"""Shared helper modules under lib/: the path-safe file API, import tracking,
and staleness when a helper changes."""

import asyncio
import json
import os
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exuvia.agent import TOOLS, Agent
from exuvia.project import Project, parse_node
from exuvia.server import create_app
from exuvia.workspace import Workspace


def node(root: Path, name: str, body: str) -> None:
    (root / "nodes").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / f"{name}.py").write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def run(ws, *a, **k):
    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        return await ws.run(*a, **k)

    return asyncio.run(go())


SCALED = '''
import exuvia

@exuvia.node()
def scaled():
    import pandas as pd
    from lib.stats import factor
    return pd.DataFrame({"x": [factor()]})
'''

PLAIN = '''
import exuvia

@exuvia.node()
def plain():
    import pandas as pd
    return pd.DataFrame({"x": [1]})
'''

DOWNSTREAM = '''
import exuvia

@exuvia.node()
def downstream(scaled):
    return scaled.assign(y=scaled.x + 1)
'''


# -- paths ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "nodes/a.py",
        "lib",
        "lib/",
        "lib/../nodes/a.py",
        "lib/sub/../../exuvia.json",
        "/lib/x.py",
        "lib//x.py",
        "lib\\x.py",
        "lib/x.sql",
        "lib/x.py.txt",
        "lib/x",
        "lib/.hidden.py",
        "lib/my-mod.py",
        "lib/1st.py",
        "lib/__pycache__/x.cpython-312.pyc",
        "LIB/x.py",
        "exuvia.json",
        ".exuvia/exuvia.db",
        "lib/a/b/c/d/e/f/g/h.py",
    ],
)
def test_helper_paths_outside_lib_or_not_modules_are_refused(tmp_path, path):
    project = Project(tmp_path)
    with pytest.raises(ValueError):
        project.helper_path(path)
    with pytest.raises(ValueError):
        project.write_helper(path, "x = 1\n")
    assert not any(p.is_file() for p in tmp_path.rglob("*") if ".exuvia" not in p.parts)


@pytest.mark.parametrize("path", ["lib/stats.py", "lib/vol/surface.py", "lib/vol/__init__.py", "lib/A_b2.py"])
def test_module_paths_under_lib_are_accepted(tmp_path, path):
    project = Project(tmp_path)
    assert project.helper_path(path) == tmp_path.resolve().joinpath(*path.split("/"))


def test_a_symlink_out_of_lib_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    project = Project(tmp_path / "proj")
    try:
        os.symlink(outside, project.lib_dir / "escape", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    with pytest.raises(ValueError, match="outside"):
        project.write_helper("lib/escape/x.py", "x = 1\n")
    assert not (outside / "x.py").exists()


def test_write_read_list_delete(tmp_path):
    project = Project(tmp_path)
    assert project.lib_dir.is_dir() and project.helper_files() == []
    project.write_helper("lib/stats.py", "def f():\n    return 1\n")
    project.write_helper("lib/vol/surface.py", "x = 2\n")
    assert project.helper_files() == ["lib/stats.py", "lib/vol/surface.py"]
    assert project.read_helper("lib/vol/surface.py") == "x = 2\n"
    project.delete_helper("lib/vol/surface.py")
    assert not (project.lib_dir / "vol").exists()  # emptied package dirs go too
    assert project.lib_dir.is_dir()
    with pytest.raises(KeyError):
        project.read_helper("lib/vol/surface.py")
    with pytest.raises(KeyError):
        project.delete_helper("lib/nope.py")
    with pytest.raises(ValueError, match="KB"):
        project.write_helper("lib/big.py", "#" * (600 * 1024))


# -- import tracking ---------------------------------------------------------------
def test_imports_are_tracked_directly_and_through_other_helpers(tmp_path):
    project = Project(tmp_path)
    project.write_helper("lib/base.py", "SCALE = 2\n")
    project.write_helper("lib/stats.py", "from .base import SCALE\n\ndef factor():\n    return SCALE\n")
    project.write_helper("lib/vol/__init__.py", "")
    project.write_helper("lib/vol/surface.py", "from lib import stats\nfrom .. import base\n")
    project.write_helper("lib/unused.py", "y = 1\n")
    deps = project.helper_deps
    assert deps("from lib.stats import factor\n") == ["lib/base.py", "lib/stats.py"]
    assert deps("import lib.stats\n") == ["lib/base.py", "lib/stats.py"]
    assert deps("def f():\n    from lib import stats\n") == ["lib/base.py", "lib/stats.py"]
    assert deps("from lib.vol.surface import x\n") == [
        "lib/base.py", "lib/stats.py", "lib/vol/__init__.py", "lib/vol/surface.py",
    ]
    assert deps("import pandas\nfrom libx import y\nimport lib_other\n") == []
    assert deps("from lib.missing import z\n") == []
    assert deps("def broken(:\n") == []


def test_helper_cycles_and_broken_helpers_do_not_hang(tmp_path):
    project = Project(tmp_path)
    project.write_helper("lib/a.py", "from lib import b\n")
    project.write_helper("lib/b.py", "from lib import a\n")
    project.write_helper("lib/bad.py", "def (:\n")
    assert project.helper_deps("import lib.a\n") == ["lib/a.py", "lib/b.py"]
    assert project.helper_deps("import lib.bad\n") == ["lib/bad.py"]


def test_node_without_helpers_keeps_its_plain_file_hash(tmp_path):
    node(tmp_path, "plain", PLAIN)
    Project(tmp_path).write_helper("lib/stats.py", "def factor():\n    return 2\n")
    g = Project(tmp_path).load()
    # Projects that use no helpers see no hash change (no mass re-run on upgrade).
    assert g.nodes["plain"].code_hash == parse_node(tmp_path / "nodes" / "plain.py", tmp_path.resolve()).code_hash
    assert g.nodes["plain"].helpers == []


# -- staleness ------------------------------------------------------------------------
def test_editing_a_helper_stales_only_the_nodes_that_import_it(tmp_path):
    node(tmp_path, "scaled", SCALED)
    node(tmp_path, "plain", PLAIN)
    node(tmp_path, "downstream", DOWNSTREAM)
    ws = Workspace(tmp_path)
    info = ws.write_helper("lib/stats.py", "def factor():\n    return 2\n")
    assert info == {"path": "lib/stats.py", "bytes": 27, "used_by": ["scaled"], "parse_error": None}
    finals = run(ws, ["downstream", "plain"])
    assert {n: r["status"] for n, r in finals.items()} == {"scaled": "success", "plain": "success", "downstream": "success"}
    assert ws.graph.nodes["scaled"].helpers == ["lib/stats.py"]
    assert all(s["state"] == "fresh" for s in ws.status().values())

    ws.write_helper("lib/stats.py", "def factor():\n    return 5\n")
    st = ws.status()
    assert st["scaled"]["state"] == "stale" and st["scaled"]["reason"] == "code changed"
    assert st["downstream"]["state"] == "stale"
    assert st["plain"]["state"] == "fresh"

    run(ws, ["downstream"])
    assert ws.preview("scaled", params=ws.resolve_params())["rows"] == [[5]]
    assert all(s["state"] == "fresh" for s in ws.status().values())

    # A helper reached through another helper counts too.
    ws.write_helper("lib/base.py", "BASE = 1\n")
    ws.write_helper("lib/stats.py", "from lib.base import BASE\n\ndef factor():\n    return 5 * BASE\n")
    assert ws.status()["scaled"]["state"] == "stale"
    run(ws, ["scaled"])
    ws.write_helper("lib/base.py", "BASE = 3\n")
    assert ws.helper_users("lib/base.py") == ["scaled"]
    assert ws.status()["scaled"]["state"] == "stale"

    out = ws.delete_helper("lib/base.py")
    assert out == {"deleted": "lib/base.py", "was_used_by": ["scaled"]}


# -- HTTP + agent ----------------------------------------------------------------------
def test_files_routes(tmp_path):
    node(tmp_path, "scaled", SCALED)
    client = TestClient(create_app(tmp_path))
    assert client.get("/api/files").json() == {"files": []}

    put = client.put("/api/files/lib/stats.py", json={"source": "def factor():\n    return 2\n"})
    assert put.status_code == 200 and put.json()["used_by"] == ["scaled"]
    assert client.get("/api/files").json() == {"files": [{"path": "lib/stats.py", "bytes": 27, "used_by": ["scaled"]}]}
    got = client.get("/api/files/lib/stats.py").json()
    assert got["source"].startswith("def factor") and got["used_by"] == ["scaled"]
    snap = client.get("/api/project").json()
    assert next(n for n in snap["nodes"] if n["name"] == "scaled")["helpers"] == ["lib/stats.py"]

    bad = client.put("/api/files/lib/bad.py", json={"source": "def (:\n"})
    assert bad.status_code == 200 and bad.json()["parse_error"].startswith("SyntaxError")

    for path in ["nodes/scaled.py", "lib/x.sql", "exuvia.json", "lib/%2E%2E/nodes/scaled.py", "lib/my-mod.py"]:
        assert client.put(f"/api/files/{path}", json={"source": "x = 1\n"}).status_code in (400, 404), path
        assert client.get(f"/api/files/{path}").status_code in (400, 404), path
    assert (tmp_path / "nodes" / "scaled.py").read_text(encoding="utf-8").lstrip().startswith("import exuvia")
    assert not (tmp_path / "exuvia.json").exists()

    assert client.get("/api/files/lib/nope.py").status_code == 404
    assert client.delete("/api/files/lib/nope.py").status_code == 404
    assert client.delete("/api/files/lib/stats.py").json() == {"deleted": "lib/stats.py", "was_used_by": ["scaled"]}
    assert [f["path"] for f in client.get("/api/files").json()["files"]] == ["lib/bad.py"]

    guide = client.get("/api/guide").json()
    assert {"list_files", "read_file", "write_file", "delete_file"} <= {t["name"] for t in guide["tools"]}


def call(agent, name, args):
    async def go():
        agent.ws.bus.bind(asyncio.get_running_loop())
        return await agent._call(name, args)

    return asyncio.run(go())


def test_agent_file_tools(tmp_path):
    node(tmp_path, "scaled", SCALED)
    agent = Agent(Workspace(tmp_path))

    ok, text, extra = call(agent, "write_file", {"path": "lib/stats.py", "source": "def factor():\n    return 2\n"})
    assert ok and json.loads(text)["used_by"] == ["scaled"] and extra == {"file": "lib/stats.py"}
    ok, text, _ = call(agent, "read_file", {"path": "lib/stats.py"})
    assert ok and text == "# used by: scaled\ndef factor():\n    return 2\n"
    ok, text, _ = call(agent, "list_files", {})
    assert ok and json.loads(text) == [{"path": "lib/stats.py", "bytes": 27, "used_by": ["scaled"]}]

    ok, text, _ = call(agent, "write_file", {"path": "nodes/scaled.py", "source": "x = 1\n"})
    assert not ok and "lib/" in text
    ok, text, _ = call(agent, "write_file", {"path": "lib/bad.py", "source": "def (:\n"})
    assert not ok and "does not parse" in text
    ok, text, _ = call(agent, "read_file", {"path": "lib/nope.py"})
    assert not ok and text == "no file lib/nope.py"
    ok, text, _ = call(agent, "delete_file", {"path": "lib/stats.py"})
    assert ok and json.loads(text)["was_used_by"] == ["scaled"]

    assert {"list_files", "read_file", "write_file", "delete_file"} <= {t["name"] for t in TOOLS}
    guide = agent.instructions()
    assert "write_file" in guide and "from lib.stats import" in guide
