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
HUB_TOOL_NAMES = {
    "list_projects", "create_project", "list_people", "list_groups", "get_group", "create_group", "update_group",
    "delete_group", "set_group_member", "set_project_group", "get_project_sharing", "set_project_member",
    "set_project_access",
}


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


def _serve(app):
    """An app on a real port, in a thread; returns (url, server, thread)."""
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server, thread


@pytest.fixture
def hub_served(tmp_path, monkeypatch):
    """`exuvia hub` with a token on a real port."""
    from exuvia.server import create_hub_app

    monkeypatch.setenv("EXUVIA_API_TOKEN", "s3cret")
    url, server, thread = _serve(create_hub_app(tmp_path / "hub"))
    yield url
    server.should_exit = True
    thread.join(timeout=10)


def test_a_hub_url_relays_to_every_project(hub_served):
    """`exuvia mcp --url <hub>`: one server for the whole hub. The agent
    lists and creates projects, and names the project on every tool call."""
    from exuvia.mcp_server import HubBackend, remote_backend

    async def go():
        backend = await remote_backend(hub_served, "s3cret")
        assert isinstance(backend, HubBackend)
        await backend.start()
        try:
            server = build_server(backend, await backend.instructions())
            async with Client(server) as client:
                tools = {t.name: t for t in (await client.list_tools()).tools}
                empty = text(await client.call_tool("list_projects", {}))
                made = await client.call_tool("create_project", {"id": "books", "name": "Books"})
                await client.call_tool("create_project", {"id": "other"})
                dup = await client.call_tool("create_project", {"id": "books"})
                wrote = await client.call_tool("write_node", {"project": "books", "name": "nums", "kind": "python", "source": SRC})
                ran = await client.call_tool("run_node", {"project": "books", "name": "nums"})
                other = await client.call_tool("list_nodes", {"project": "other"})
                no_project = await client.call_tool("list_nodes", {})
                missing = await client.call_tool("list_nodes", {"project": "nope"})
                hub_guide = text(await client.call_tool("guide", {}))
                book_guide = await client.call_tool("guide", {"project": "books"})
                listing = json.loads(text(await client.call_tool("list_projects", {})))
                docs = await client.call_tool("docs", {"topic": "Kerberos"})
            return locals() | {"instructions": server.instructions}
        finally:
            await backend.stop()

    seen = asyncio.run(go())
    tools = seen["tools"]
    assert set(tools) == {"guide", *HUB_TOOL_NAMES, *(t["name"] for t in TOOLS)}
    run_schema = tools["run_node"].input_schema
    assert run_schema["required"] == ["project", "name"] and "project" in run_schema["properties"]
    assert "project" not in tools["guide"].input_schema.get("required", [])
    assert "project" not in tools["docs"].input_schema["properties"]  # about exuvia, not a project
    assert not seen["docs"].is_error and text(seen["docs"]).startswith("### Kerberos")
    assert "no projects yet" in seen["instructions"]
    assert json.loads(seen["empty"])["projects"] == []

    assert not seen["made"].is_error
    assert json.loads(text(seen["made"])) == {"id": "books", "name": "Books", "workbench": f"{hub_served}/p/books/"}
    assert seen["dup"].is_error and "already exists" in text(seen["dup"])
    assert not seen["wrote"].is_error
    assert not seen["ran"].is_error and json.loads(text(seen["ran"]))["rows"] == 3
    assert json.loads(text(seen["other"])) == []  # each project is its own
    assert seen["no_project"].is_error and "name the project" in text(seen["no_project"])
    assert "ask them" in text(seen["no_project"])
    assert seen["missing"].is_error and "no project nope" in text(seen["missing"])

    assert "Every project tool takes the project's id" in seen["hub_guide"] and "- books: Books" in seen["hub_guide"]
    # With several projects and no word on which, the agent asks instead of taking the first.
    assert "ask the user which one" in seen["hub_guide"] and "never the first one listed" in seen["hub_guide"]
    assert "ask them first" in run_schema["properties"]["project"]["description"]
    assert not seen["book_guide"].is_error and "How exuvia works" in text(seen["book_guide"])
    assert {p["id"]: p["nodes"] for p in seen["listing"]["projects"]} == {"books": 1, "other": 0}


def test_a_hub_url_organizes_groups(hub_served):
    """Through the relay, the agent makes groups, starts a project in one,
    moves projects between groups and renames and deletes a group, with the
    hub API's checks: a hub nobody signs in to (the token only) has no
    people to add."""
    from exuvia.mcp_server import remote_backend

    async def go():
        backend = await remote_backend(hub_served, "s3cret")
        await backend.start()
        try:
            async with Client(build_server(backend, "")) as client:
                async def call(name, args):
                    r = await client.call_tool(name, args)
                    return r.is_error, text(r)

                _, made = await call("create_group", {"name": "Research"})
                gid = json.loads(made)["id"]
                in_group = await call("create_project", {"id": "books", "group": gid})
                await call("create_project", {"id": "loose"})
                moved = await call("set_project_group", {"project": "loose", "group": gid})
                group = await call("get_group", {"group": gid})
                out = await call("set_project_group", {"project": "loose", "group": None})
                renamed = await call("update_group", {"group": gid, "name": "Lab"})
                listed = await call("list_groups", {})
                person = await call("set_group_member", {"group": gid, "email": "bob@example.com", "role": "editor"})
                both = await call("set_group_member", {"group": gid, "user": "u1", "email": "b@x.org", "role": "viewer"})
                nothing = await call("update_group", {"group": gid})
                no_group = await call("create_project", {"id": "x", "group": "nope"})
                sneaky = await call("get_group", {"group": "../projects"})
                bad_project = await call("set_project_group", {"project": "../api", "group": gid})
                deleted = await call("delete_group", {"group": gid})
                after = await call("list_projects", {})
            return locals()
        finally:
            await backend.stop()

    seen = asyncio.run(go())
    err, made = seen["in_group"]
    assert not err and json.loads(made)["group"]["id"] == seen["gid"]
    assert json.loads(made)["workbench"] == f"{hub_served}/p/books/"
    assert not seen["moved"][0]
    err, group = seen["group"]
    assert not err and {p["id"] for p in json.loads(group)["projects"]} == {"books", "loose"}
    assert not seen["out"][0] and json.loads(seen["out"][1])["group"] is None
    assert not seen["renamed"][0] and json.loads(seen["renamed"][1])["name"] == "Lab"
    assert [(g["name"], g["projects"]) for g in json.loads(seen["listed"][1])["groups"]] == [("Lab", 1)]
    assert seen["person"][0] and "nobody signs in" in seen["person"][1]
    assert seen["both"][0] and "give one of `user`" in seen["both"][1]
    assert seen["nothing"][0] and "INVALID_INPUT" in seen["nothing"][1]
    assert seen["no_group"][0] and "no group nope" in seen["no_group"][1]
    assert seen["sneaky"][0] and "INVALID_INPUT: no group ../projects" in seen["sneaky"][1]
    assert seen["bad_project"][0] and "no project ../api" in seen["bad_project"][1]
    assert not seen["deleted"][0] and json.loads(seen["deleted"][1])["projects"] == ["books"]
    assert {p["id"]: p["group"] for p in json.loads(seen["after"][1])["projects"]} == {"books": None, "loose": None}


def test_a_hub_url_needs_the_token(hub_served):
    from exuvia.mcp_server import remote_backend

    async def go():
        backend = await remote_backend(hub_served, None)
        await backend.start()

    with pytest.raises(RuntimeError, match="turned the token away"):
        asyncio.run(go())


def test_which_urls_relay_to_a_whole_hub(hub_served, served):
    """Only a hub's own address; a hub project or an `exuvia serve` is one project."""
    from exuvia.mcp_server import HubBackend, remote_backend

    async def kinds():
        out = []
        for url in (hub_served, hub_served + "/", f"{hub_served}/p/books", served[0], "http://127.0.0.1:1"):
            b = await remote_backend(url, "s3cret")
            out.append(type(b).__name__)
            await b.http.aclose()
        return out

    assert asyncio.run(kinds()) == ["HubBackend", "HubBackend", "RemoteBackend", "RemoteBackend", "RemoteBackend"]
    assert HubBackend  # imported for the names above


def test_hub_tools_route(tmp_path):
    from exuvia.server import create_hub_app

    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        tools = hub.get("/api/tools").json()["tools"]
        assert {t["name"] for t in tools} == TOOL_NAMES - {"guide"}


# -- MCP over HTTP: the server's own /mcp (exuvia/mcp_http.py) ------------------


def http_client(url: str, token: str | None = None):
    """An MCP client of a Streamable HTTP endpoint, as Claude Code or Codex
    connect with just the address (and a bearer token)."""
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else None
    return Client(streamable_http_client(url, http_client=create_mcp_http_client(headers=headers)))


async def drive_http(url: str, token: str | None = None) -> dict:
    async with http_client(url, token) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        guide = text(await client.call_tool("guide", {}))
        wrote = await client.call_tool("write_node", {"name": "nums", "kind": "python", "source": SRC})
        await client.call_tool("write_node", {"name": "total", "kind": "sql", "source": TOTAL})
        ran = await client.call_tool("run_node", {"name": "total"})
        queried = await client.call_tool("query_data", {"sql": "select max(n) as m from nums"})
        bad = await client.call_tool("write_node", {"name": "x"})
        missing = await client.call_tool("run_node", {"name": "nope"})
        instructions = client.instructions
    return locals()


def test_serve_answers_mcp_over_http(served):
    """`exuvia serve` is its own MCP server at /mcp: no `exuvia mcp` needed."""
    url, root = served
    check(asyncio.run(drive_http(f"{url}/mcp")))
    with TestClient(create_app(root)) as client:
        revs = client.get("/api/code/revisions", params={"node": "total"}).json()
        assert revs[0]["origin"] == "mcp"
        assert client.get("/api/nodes/total/runs").json()[0]["trigger"] == "mcp"
    # Some clients add a trailing slash.
    assert asyncio.run(drive_http(f"{url}/mcp/"))["instructions"]


def test_a_hub_answers_mcp_over_http(hub_served):
    """<hub>/mcp: every project, the same tools as `exuvia mcp --url <hub>`;
    <hub>/p/<id>/mcp: one project. Both behind the API's token."""
    async def hub_wide(token):
        async with http_client(f"{hub_served}/mcp", token) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            empty = client.instructions
            made = await client.call_tool("create_project", {"id": "books", "name": "Books"})
            dup = await client.call_tool("create_project", {"id": "books"})
            bad_id = await client.call_tool("create_project", {"id": "no spaces"})
            wrote = await client.call_tool("write_node", {"project": "books", "name": "nums", "kind": "python", "source": SRC})
            ran = await client.call_tool("run_node", {"project": "books", "name": "nums"})
            no_project = await client.call_tool("list_nodes", {})
            missing = await client.call_tool("list_nodes", {"project": "nope"})
            hub_guide = text(await client.call_tool("guide", {}))
            book_guide = await client.call_tool("guide", {"project": "books"})
            listing = json.loads(text(await client.call_tool("list_projects", {})))
            docs = await client.call_tool("docs", {"query": "kerberos keytab"})
        async with http_client(f"{hub_served}/mcp", token) as client:
            later = client.instructions  # worked out again for each client
        return locals()

    seen = asyncio.run(hub_wide("s3cret"))
    assert set(seen["tools"]) == {"guide", *HUB_TOOL_NAMES, *(t["name"] for t in TOOLS)}
    assert seen["tools"]["run_node"].input_schema["required"] == ["project", "name"]
    assert "project" not in seen["tools"]["docs"].input_schema["properties"]
    assert not seen["docs"].is_error and "## Deploying > Kerberos" in text(seen["docs"])
    assert "no projects yet" in seen["empty"] and "- books: Books" in seen["later"]
    assert json.loads(text(seen["made"])) == {"id": "books", "name": "Books", "workbench": f"{hub_served}/p/books/"}
    assert seen["dup"].is_error and "already exists" in text(seen["dup"])
    assert seen["bad_id"].is_error and "project ids are" in text(seen["bad_id"])
    assert not seen["wrote"].is_error
    assert not seen["ran"].is_error and json.loads(text(seen["ran"]))["rows"] == 3
    assert seen["no_project"].is_error and "name the project" in text(seen["no_project"])
    assert seen["missing"].is_error and "no project nope" in text(seen["missing"])
    assert "Every project tool takes the project's id" in seen["hub_guide"]
    assert not seen["book_guide"].is_error and "How exuvia works" in text(seen["book_guide"])
    assert [p["id"] for p in seen["listing"]["projects"]] == ["books"]

    async def one_project():
        async with http_client(f"{hub_served}/p/books/mcp", "s3cret") as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            nodes = json.loads(text(await client.call_tool("list_nodes", {})))
            asked = json.loads(text(await client.call_tool("request_secret", {"name": "API_KEY", "reason": "x"})))
            instructions = client.instructions
        async with http_client(f"{hub_served}/mcp", "s3cret") as client:
            hub_asked = json.loads(text(await client.call_tool(
                "request_secret", {"project": "books", "name": "HUB_KEY", "reason": "x"})))
        return tools, nodes, instructions, asked, hub_asked

    tools, nodes, instructions, asked, hub_asked = asyncio.run(one_project())
    assert tools == TOOL_NAMES and [n["name"] for n in nodes] == ["nums"]
    assert "How exuvia works" in instructions
    # No back-channel over the stateless endpoint: the link is in the answer.
    assert asked["workbench"] == f"{hub_served}/p/books/#env=API_KEY"
    assert asked["terminal"] == f"exuvia env set API_KEY --url {hub_served}/p/books"
    assert hub_asked["workbench"] == f"{hub_served}/p/books/#env=HUB_KEY"

    import httpx

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    accept = {"Accept": "application/json, text/event-stream"}
    for path in ("/mcp", "/p/books/mcp"):  # no token, no tools
        r = httpx.post(hub_served + path, json=init, headers=accept)
        assert r.status_code == 401, (path, r.status_code)
    r = httpx.post(f"{hub_served}/p/nope/mcp", json=init, headers={**accept, "Authorization": "Bearer s3cret"})
    assert r.status_code == 404 and "no project nope" in r.text


def test_mcp_over_http_works_as_the_agent_tokens_owner(tmp_path, monkeypatch):
    """A person's agent token signs the MCP client in as them: what it
    changes and runs is theirs, as through the API."""
    import httpx

    from exuvia.server import create_hub_app

    for name in ("EXUVIA_API_TOKEN", "EXUVIA_AUTH_TRUSTED_PROXIES", "EXUVIA_DB", "EXUVIA_ADMINS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXUVIA_AUTH", "headers")
    alice = {"X-Forwarded-User": "alice", "X-Forwarded-Email": "alice@example.com"}
    url, server, thread = _serve(create_hub_app(tmp_path / "hub"))
    try:
        token = httpx.post(f"{url}/api/me/tokens", json={"name": "codex"}, headers=alice).json()["token"]
        uid = httpx.get(f"{url}/api/me", headers=alice).json()["user"]["uid"]

        async def go():
            async with http_client(f"{url}/mcp", token) as client:
                await client.call_tool("create_project", {"id": "a"})
                await client.call_tool("write_node", {"project": "a", "name": "nums", "kind": "python", "source": SRC})
                return await client.call_tool("run_node", {"project": "a", "name": "nums"})

        assert not asyncio.run(go()).is_error
        [rev] = httpx.get(f"{url}/p/a/api/code/revisions", params={"node": "nums"}, headers=alice).json()
        assert (rev["author"], rev["author_user"], rev["origin"]) == ("alice", uid, "mcp")
        [run] = httpx.get(f"{url}/p/a/api/nodes/nums/runs", headers=alice).json()
        assert (run["started_by"], run["started_by_user"], run["trigger"]) == ("alice", uid, "mcp")

        # Without a token or the proxy's headers, nobody gets in.
        r = httpx.post(f"{url}/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401 and "agent token" in r.json()["detail"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# -- request_secret: the value never passes through the client ----------------------

@pytest.mark.parametrize("mode", ["auto", "legacy"])  # 2026-07-28 input_required / 2025-11-25 elicitation/create
def test_request_secret_asks_the_client_to_open_the_workbench(served, mode):
    import httpx
    import mcp_types as types

    url, _root = served
    asked = []

    async def on_elicit(_ctx, params):
        asked.append(params)
        return types.ElicitResult(action="accept")

    # The real command over stdio: the server asks the client back on it.
    params = StdioServerParameters(command=sys.executable, args=["-m", "exuvia.cli", "mcp", "--url", url])

    async def go():
        async with Client(params, elicitation_callback=on_elicit, mode=mode) as client:
            eliciting = await client.call_tool("request_secret", {"name": "FRED_API_KEY", "reason": "the rates node"})
        async with Client(params, mode=mode) as plain:  # a client without elicitation
            linked = await plain.call_tool("request_secret", {"name": "OTHER_KEY", "reason": "x"})
        return eliciting, linked

    eliciting, linked = asyncio.run(go())
    [params] = asked
    assert params.mode == "url" and params.url == f"{url}/#env=FRED_API_KEY"
    assert "FRED_API_KEY" in params.message and "the rates node" in params.message
    res = json.loads(text(eliciting))
    assert res["status"] == "requested" and res["user"] == "opened the link to set it"
    assert res["terminal"] == f"exuvia env set FRED_API_KEY --url {url}"
    res = json.loads(text(linked))
    assert "user" not in res and res["workbench"] == f"{url}/#env=OTHER_KEY" and "Never ask for the value" in res["message"]
    # The server holds both requests for the workbench to show.
    waiting = httpx.get(f"{url}/api/env").json()["requests"]
    assert {r["name"] for r in waiting} >= {"FRED_API_KEY", "OTHER_KEY"} and waiting[0]["origin"] == "mcp"
    # Setting it answers the request.
    httpx.put(f"{url}/api/env/FRED_API_KEY", json={"value": "k-123456"})
    assert "FRED_API_KEY" not in {r["name"] for r in httpx.get(f"{url}/api/env").json()["requests"]}
    httpx.delete(f"{url}/api/env/FRED_API_KEY")


def test_request_secret_on_a_local_project_points_at_the_terminal(tmp_path):
    from exuvia.mcp_server import LocalBackend

    root = project(tmp_path)

    async def go():
        backend = LocalBackend(root)
        await backend.start()
        try:
            return await backend.call("request_secret", {"name": "FRED_API_KEY", "reason": "rates"})
        finally:
            await backend.stop()

    ok, out = asyncio.run(go())
    res = json.loads(out)
    assert ok and "workbench" not in res and res["terminal"] == f"exuvia env set FRED_API_KEY {root}"


@pytest.fixture
def released(monkeypatch):
    """A uvx install of 0.3.5 with 0.4.0 out on PyPI (no test reaches PyPI)."""
    from exuvia import about

    for name in ("EXUVIA_DEPLOYED", "EXUVIA_UPDATE_CHECK"):
        monkeypatch.delenv(name, raising=False)
    about._cache.update(latest=None, error=None, checked_at=0.0)
    about._lock = None

    async def fetch() -> str:
        return "0.4.0"

    monkeypatch.setattr(about, "_fetch", fetch)
    monkeypatch.setattr(about, "__version__", "0.3.5")
    monkeypatch.setattr(about, "install_kind", lambda: "uvx")


def test_the_version_tool_says_how_to_update(tmp_path, released):
    """`version`: the running exuvia, the latest release and the command that
    updates this install, so an agent can update exuvia without a skill."""
    backend = LocalBackend(project(tmp_path))

    async def go():
        await backend.start()
        try:
            return (
                await backend.instructions(),
                await backend.call("version", {}),
                await backend.call("version", {"refresh": "yes"}),
            )
        finally:
            await backend.stop()

    guide, (ok, out), (bad_ok, bad) = asyncio.run(go())
    v = json.loads(out)
    assert ok and v["version"] == "0.3.5" and v["latest"] == "0.4.0" and v["update"] == "available"
    assert v["upgrade"] == "uvx exuvia@latest mcp <folder>"
    assert not bad_ok and bad.startswith("INVALID_INPUT")
    # The guide sends CLI questions to `docs` and update questions to `version`.
    assert "the `exuvia` command line" in guide and "`version` says which exuvia is running" in guide


def test_a_relay_answers_version_for_the_server_and_this_machine(served, released):
    url, _ = served

    async def go(where):
        backend = RemoteBackend(where)
        await backend.start()
        try:
            return await backend.call("version", {})
        finally:
            await backend.stop()

    ok, out = asyncio.run(go(url))
    v = json.loads(out)
    assert ok and v["server"]["url"] == url and v["server"]["upgrade"] == "uvx exuvia@latest serve <folder>"
    assert v["this_machine"]["upgrade"] == f"uvx exuvia@latest mcp --url {url}"

    with socket.socket() as s:  # nothing answers: this machine's exuvia still does
        s.bind(("127.0.0.1", 0))
        down = f"http://127.0.0.1:{s.getsockname()[1]}"
    ok, out = asyncio.run(go(down))
    v = json.loads(out)
    assert ok and "could not reach exuvia" in v["server"]["error"] and v["this_machine"]["version"] == "0.3.5"


def test_a_hub_answers_version_without_a_project(hub_served, released):
    from exuvia.mcp_server import remote_backend

    async def relay():
        backend = await remote_backend(hub_served, "s3cret")
        await backend.start()
        try:
            tools = {t["name"]: t for t in await backend.tools()}
            return tools, await backend.call("version", {})
        finally:
            await backend.stop()

    tools, (ok, out) = asyncio.run(relay())
    assert "project" not in tools["version"]["input_schema"]["properties"]
    v = json.loads(out)
    assert ok and v["server"]["upgrade"] == "uvx exuvia@latest hub <folder>"
    assert v["this_machine"]["upgrade"] == f"uvx exuvia@latest mcp --url {hub_served}"

    async def over_http():
        async with http_client(f"{hub_served}/mcp", "s3cret") as client:
            return await client.call_tool("version", {})

    res = asyncio.run(over_http())
    assert not res.is_error and json.loads(text(res))["upgrade"] == "uvx exuvia@latest hub <folder>"
