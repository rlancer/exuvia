"""`exuvia mcp`: the agent's tools for MCP clients, in process, relayed to a
running server, and over a real stdio pipe. Also `POST /api/tools/{name}`."""

import asyncio
import json
import socket
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from exuvia.agent import TOOLS
from exuvia.project import Project
from exuvia.server import create_app

mcp = pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters  # noqa: E402

from exuvia.mcp_server import LocalBackend, RemoteBackend, build_server  # noqa: E402

SRC = '''import exuvia


@exuvia.node()
def nums():
    """Three numbers."""
    import pandas as pd

    return pd.DataFrame({"n": [1, 2, 3]})
'''
TOTAL = "select sum(n) as s from nums\n"
TOOL_NAMES = {"guide", *(t["name"] for t in TOOLS)}


def text(result) -> str:
    return "".join(c.text for c in result.content)


async def drive(backend) -> dict:
    """Build and run a small graph through an MCP client; return what it saw."""
    await backend.start()
    try:
        server = build_server(backend, await backend.instructions())
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            guide = await client.call_tool("guide", {})
            wrote = await client.call_tool("write_node", {"name": "nums", "kind": "python", "source": SRC})
            await client.call_tool("write_node", {"name": "total", "kind": "sql", "source": TOTAL})
            ran = await client.call_tool("run_node", {"name": "total"})
            queried = await client.call_tool("query_data", {"sql": "select max(n) as m from nums"})
            bad = await client.call_tool("write_node", {"name": "x"})
            missing = await client.call_tool("run_node", {"name": "nope"})
        return {
            "tools": tools,
            "guide": text(guide),
            "wrote": wrote,
            "ran": ran,
            "queried": queried,
            "bad": bad,
            "missing": missing,
            "instructions": server.instructions,
        }
    finally:
        await backend.stop()


def check(seen: dict) -> None:
    assert set(seen["tools"]) == TOOL_NAMES
    schema = seen["tools"]["run_node"].input_schema
    assert schema["required"] == ["name"] and "params" in schema["properties"]
    assert "How exuvia works" in seen["guide"] and seen["guide"] == seen["instructions"]
    assert not seen["wrote"].is_error and json.loads(text(seen["wrote"]))["written"] == "nums"
    out = json.loads(text(seen["ran"]))
    assert not seen["ran"].is_error and out["runs"] == {"nums": "success", "total": "success"}
    assert out["head"] == [{"s": 6}]
    assert json.loads(text(seen["queried"])) == [{"m": 3}]
    # Errors come back as tool results the model can read, not protocol errors.
    assert seen["bad"].is_error and text(seen["bad"]).startswith("INVALID_INPUT")
    assert seen["missing"].is_error and "no node named nope" in text(seen["missing"])


def project(tmp_path):
    Project(tmp_path)  # creates nodes/
    return tmp_path


def test_local_backend_runs_the_agent_tools(tmp_path):
    backend = LocalBackend(project(tmp_path))
    check(asyncio.run(drive(backend)))
    # Changes and runs are recorded as the MCP client's.
    ws = backend.ws
    assert ws.code_revisions(node="nums", limit=5)[0]["origin"] == "mcp"
    assert ws.store.runs_for("total")[0]["trigger"] == "mcp"


def test_tools_route(tmp_path):
    with TestClient(create_app(project(tmp_path))) as client:
        guide = client.get("/api/guide").json()
        assert {t["name"] for t in guide["tools"]} == TOOL_NAMES - {"guide"}
        assert all(t["input_schema"]["type"] == "object" for t in guide["tools"])

        r = client.post("/api/tools/write_node", json={"name": "nums", "kind": "python", "source": SRC},
                        headers={"X-Exuvia-Origin": "my-bot"})
        assert r.status_code == 200 and r.json()["ok"] and r.json()["node"] == "nums"
        revs = client.get("/api/code/revisions", params={"node": "nums"}).json()
        assert revs[0]["origin"] == "my-bot"

        r = client.post("/api/tools/run_node", json={"name": "nums"}).json()
        assert r["ok"] and json.loads(r["text"])["rows"] == 3
        assert client.get("/api/nodes/nums/runs").json()[0]["trigger"] == "api"

        r = client.post("/api/tools/list_nodes").json()  # no body: no arguments
        assert r["ok"] and [n["name"] for n in json.loads(r["text"])] == ["nums"]

        r = client.post("/api/tools/write_node", json=["not", "an", "object"]).json()
        assert not r["ok"] and r["text"] == "INVALID_INPUT: tool input must be an object"
        assert client.post("/api/tools/drop_everything", json={}).status_code == 404


@pytest.fixture
def served(tmp_path):
    """`exuvia serve` on a real port, in a thread."""
    import uvicorn

    root = project(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(root), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", root
    server.should_exit = True
    thread.join(timeout=10)


def test_remote_backend_relays_to_a_running_server(served):
    url, root = served
    check(asyncio.run(drive(RemoteBackend(url))))
    # The server did the work: its project has the nodes, recorded as "mcp".
    with TestClient(create_app(root)) as client:
        revs = client.get("/api/code/revisions", params={"node": "total"}).json()
        assert revs[0]["origin"] == "mcp"


def test_remote_backend_starts_without_a_running_server():
    """No workbench yet: the relay still lists the tools, and calls (and
    the guide) say how to start one, so the agent can fix it itself."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here once the socket closes

    async def go():
        backend = RemoteBackend(f"http://127.0.0.1:{port}")
        await backend.start()
        try:
            return await backend.tools(), await backend.instructions(), await backend.call("list_nodes", {})
        finally:
            await backend.stop()

    tools, instructions, (ok, text) = asyncio.run(go())
    assert {t["name"] for t in tools} == {t["name"] for t in TOOLS}
    assert not ok
    for said in (text, instructions):
        assert "could not reach exuvia" in said and f"exuvia serve <project folder> --port {port}" in said


def test_remote_backend_picks_up_a_server_started_later(served):
    url, _ = served

    async def go():
        backend = RemoteBackend("http://127.0.0.1:1")  # nothing there
        await backend.start()
        backend.api = f"{url}/api"  # ...until the workbench comes up
        try:
            return await backend.call("list_nodes", {}), await backend.instructions()
        finally:
            await backend.stop()

    (ok, _), instructions = asyncio.run(go())
    assert ok and "could not reach" not in instructions


def test_stdio_end_to_end(tmp_path):
    """The real command, spoken to over its stdin/stdout."""
    root = project(tmp_path)
    params = StdioServerParameters(command=sys.executable, args=["-m", "exuvia.cli", "mcp", str(root)])

    async def go():
        async with Client(params) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            await client.call_tool("write_node", {"name": "nums", "kind": "python", "source": SRC})
            ran = await client.call_tool("run_node", {"name": "nums"})
            return tools, ran

    tools, ran = asyncio.run(go())
    assert tools == TOOL_NAMES
    assert not ran.is_error and json.loads(text(ran))["rows"] == 3
    assert (root / "nodes" / "nums.py").read_text(encoding="utf-8") == SRC


def test_refuses_a_directory_that_is_not_a_project(tmp_path, capsys):
    from exuvia.mcp_server import main

    with pytest.raises(SystemExit, match="not an exuvia project"):
        main(str(tmp_path), None)
    assert not (tmp_path / "nodes").exists()
