"""Outside MCP servers for the built-in agent (agent_mcp.py): each person's
list, its API (header values go in and never come back out), connecting to a
real server over Streamable HTTP with the headers that sign in to it, and the
agent's loop offering and calling those tools for the person who asked."""

import asyncio
import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from exuvia import auth
from exuvia.agent import TOOLS, Agent
from exuvia.agent_mcp import McpServers, Server, Toolbox, _tool_name, check_reachable, probe, result_text
from exuvia.db import SQLiteDatabase, close_shared
from exuvia.llm import ToolCall, Turn
from exuvia.project import Project
from exuvia.server import create_app, create_hub_app
from exuvia.workspace import Workspace

pytest.importorskip("mcp")

SRC = '''import exuvia


@exuvia.node()
def nums():
    """Three numbers."""
    import pandas as pd

    return pd.DataFrame({"n": [1, 2, 3]})
'''
ALICE = {"X-Forwarded-User": "alice", "X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-User": "bob", "X-Forwarded-Email": "bob@example.com"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    close_shared()
    for name in ("EXUVIA_API_TOKEN", "EXUVIA_BROWSER_AUTH", "EXUVIA_AUTH", "EXUVIA_AUTH_USER_HEADER",
                 "EXUVIA_AUTH_EMAIL_HEADER", "EXUVIA_AUTH_TRUSTED_PROXIES", "EXUVIA_DB", "EXUVIA_DB_SCOPE",
                 "EXUVIA_AGENT_MCP", "EXUVIA_AGENT_MCP_PRIVATE", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
                 "PORTKEY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def store(tmp_path, *, people=True, allow_private=True) -> McpServers:
    return McpServers(SQLiteDatabase(tmp_path / "mcp.db"), people=people, allow_private=allow_private, owns_db=True)


# -- the list ------------------------------------------------------------------------
def test_each_person_keeps_their_own_list_and_values_never_come_back(tmp_path):
    s = store(tmp_path)
    row = s.put("u-alice", "portkey", url="https://mcp.portkey.ai/linear/mcp",
                headers={"x-portkey-api-key": "pk-live-abcdefgh1234"})
    assert row["headers"] == [{"name": "x-portkey-api-key", "hint": "1234"}]
    assert row["enabled"] and "pk-live" not in json.dumps(s.list("u-alice"))
    assert s.list("u-bob") == [] and s.active("u-bob") == []
    [srv] = s.active("u-alice")
    assert srv.headers == {"x-portkey-api-key": "pk-live-abcdefgh1234"}

    # Headers merge: left out keeps its value, a new spelling replaces, null removes.
    s.put("u-alice", "portkey", headers={"X-Extra": "1"})
    assert s.get("u-alice", "portkey").headers == {"x-portkey-api-key": "pk-live-abcdefgh1234", "X-Extra": "1"}
    s.put("u-alice", "portkey", headers={"X-Portkey-Api-Key": "pk-new-zzzzzzzz9999", "X-Extra": None})
    assert s.get("u-alice", "portkey").headers == {"X-Portkey-Api-Key": "pk-new-zzzzzzzz9999"}

    s.put("u-alice", "portkey", enabled=False)
    assert s.active("u-alice") == [] and s.list("u-alice")[0]["enabled"] is False
    assert s.delete("u-alice", "portkey") and not s.delete("u-alice", "portkey")


@pytest.mark.parametrize("name,kw,error", [
    ("Bad-Name", {"url": "https://x.example/mcp"}, "a name is"),
    ("ok", {}, "needs a url"),
    ("ok", {"url": "ftp://x.example/mcp"}, "http:// or https://"),
    ("ok", {"url": "https://me:pw@x.example/mcp"}, "credentials in a header"),
    ("ok", {"url": "https://x.example/mcp", "headers": {"Content-Type": "x"}}, "sets Content-Type itself"),
    ("ok", {"url": "https://x.example/mcp", "headers": {"X-Key": "a\nb"}}, "one line"),
    ("ok", {"url": "https://x.example/mcp", "headers": {"bad header": "v"}}, "not a header name"),
])
def test_bad_input_is_refused(tmp_path, name, kw, error):
    with pytest.raises(ValueError, match=error):
        store(tmp_path).put("u", name, **kw)


def test_whose_list_a_request_uses(tmp_path):
    people = store(tmp_path)
    assert people.owner(auth.Identity("alice", None, "headers", uid="u-a"), current=False) == "u-a"
    assert people.owner(auth.Identity("alice", None, "token", uid="u-a"), current=False) == "u-a"
    assert people.owner(None, current=False) is None  # the API token alone: nobody's
    assert people.owner(auth.Identity(None, None, "link", link="p"), current=False) is None
    nobody = McpServers(people.db, people=False, allow_private=True)
    assert nobody.owner(None, current=False) == ""  # one list, the server's
    assert nobody.owner(auth.Identity(None, None, "link", link="p"), current=False) is None


def test_private_addresses_are_refused_unless_allowed():
    for url in ("http://127.0.0.1:9/mcp", "http://localhost/mcp", "http://10.1.2.3/mcp", "http://169.254.169.254/",
                "http://[::1]/mcp"):
        with pytest.raises(ValueError, match="not allowed here"):
            asyncio.run(check_reachable(url, allow_private=False))
        asyncio.run(check_reachable(url, allow_private=True))
    asyncio.run(check_reachable("https://93.184.215.14/mcp", allow_private=False))


def test_tool_names_fit_both_providers():
    taken = {"list_nodes"}
    assert _tool_name("linear", "create.issue", taken) == "linear__create_issue"
    long = _tool_name("srv", "x" * 100, taken)
    assert len(long) == 64 and _tool_name("srv", "x" * 100, taken) == long[:62] + "_2"


def test_results_become_text():
    from mcp_types import CallToolResult, ImageContent, TextContent

    ok, text = result_text(CallToolResult(content=[TextContent(type="text", text="hi"),
                                                  ImageContent(type="image", data="AA==", mime_type="image/png")]))
    assert ok and text.startswith("hi\n[image (image/png) not shown")
    ok, text = result_text(CallToolResult(content=[], structured_content={"a": 1}, is_error=True))
    assert not ok and text == '{"a": 1}'


# -- a real server -------------------------------------------------------------------
@pytest.fixture
def other(tmp_path, monkeypatch):
    """Another exuvia project on a real port, its API and /mcp behind a token:
    an outside MCP server that needs a header to let you in."""
    import uvicorn

    root = tmp_path / "other"
    Project(root).write_node("nums", "python", SRC)
    monkeypatch.setenv("EXUVIA_API_TOKEN", "s3cret")
    app = create_app(root)
    monkeypatch.delenv("EXUVIA_API_TOKEN")
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
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


def test_a_toolbox_connects_with_the_headers_and_calls_tools(other):
    async def go():
        box = Toolbox([Server("other", other, {"Authorization": "Bearer s3cret"}),
                       Server("locked", other, {"Authorization": "Bearer wrong"})], allow_private=True)
        await box.start({t["name"] for t in TOOLS})
        try:
            names = {t["name"] for t in box.tools}
            assert "other__list_nodes" in names and not any(n.startswith("locked__") for n in names)
            spec = next(t for t in box.tools if t["name"] == "other__read_node")
            assert spec["description"].startswith("[MCP server other]")
            assert spec["input_schema"]["type"] == "object" and "name" in spec["input_schema"]["properties"]
            ok, text = await box.call("other__list_nodes", {})
            assert ok and [n["name"] for n in json.loads(text)] == ["nums"]
            ok, text = await box.call("other__read_node", {"name": "nope"})
            assert not ok
            status = {s["name"]: s for s in box.status()}
            assert status["other"]["tools"] > 5
            assert "refused the credentials (HTTP 401)" in status["locked"]["error"]
            assert "`locked`: not available this turn" in box.instructions()
        finally:
            await box.close()

    asyncio.run(go())


def test_probe_reports_tools_or_why_not(other):
    good = asyncio.run(probe(Server("other", other, {"Authorization": "Bearer s3cret"}), allow_private=True))
    assert good["ok"] and "list_nodes" in {t["name"] for t in good["tools"]}
    bad = asyncio.run(probe(Server("other", other, {}), allow_private=True))
    assert not bad["ok"] and "HTTP 401" in bad["error"]
    private = asyncio.run(probe(Server("other", other, {}), allow_private=False))
    assert not private["ok"] and "private address" in private["error"]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}/mcp"
    gone = asyncio.run(probe(Server("dead", dead, {}), allow_private=True))
    assert not gone["ok"] and "connect" in gone["error"].lower()


# -- the agent ---------------------------------------------------------------------
class ScriptedProvider:
    name, model, label = "fake", "fake-1", "fake-1"

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    def add_user(self, history, text):
        history.append({"role": "user", "content": text})

    def add_tool_results(self, history, results):
        history.append({"role": "tool", "results": results})

    async def turn(self, system, tools, history):
        self.seen.append((system, [t["name"] for t in tools]))
        text, turn = self.turns.pop(0)
        if text:
            yield {"type": "text", "text": text}
        yield {"type": "turn_end", "turn": turn}


def chat(agent, ident, msg="hi"):
    async def go():
        agent.ws.bus.bind(asyncio.get_running_loop())
        with auth.signed_in(ident):
            return [ev async for ev in agent.chat(None, msg, None)]

    return asyncio.run(go())


def test_the_agent_uses_the_askers_servers_only(tmp_path, other):
    agent = Agent(Workspace(tmp_path / "here"))
    agent.mcp = store(tmp_path)
    agent.mcp.put("u-alice", "other", url=other, headers={"Authorization": "Bearer s3cret"})
    alice = auth.Identity("alice", None, "headers", uid="u-alice")
    bob = auth.Identity("bob", None, "headers", uid="u-bob")

    agent.provider = ScriptedProvider([
        ("", Turn("tool_use", [ToolCall("c1", "other__list_nodes", {}), ToolCall("c2", "other__nope", {})])),
        ("Done.", Turn("end")),
    ])
    events = chat(agent, alice)
    system, names = agent.provider.seen[0]
    assert "other__list_nodes" in names and "## The user's MCP servers" in system
    results = {e["id"]: e for e in events if e["type"] == "tool_result"}
    assert results["c1"]["ok"] and results["c1"]["mcp"] == "other"
    assert json.loads(results["c1"]["summary"])[0]["name"] == "nums"
    assert not results["c2"]["ok"] and "unknown tool" in results["c2"]["summary"]

    agent.provider = ScriptedProvider([("Hello.", Turn("end"))])
    chat(agent, bob)
    system, names = agent.provider.seen[0]
    assert not any(n.startswith("other__") for n in names) and "MCP servers" not in system


def test_a_server_that_fails_does_not_stop_the_turn(tmp_path):
    agent = Agent(Workspace(tmp_path / "here"))
    agent.mcp = store(tmp_path, people=False, allow_private=False)
    agent.mcp.put("", "lan", url="http://10.0.0.1/mcp")
    agent.provider = ScriptedProvider([("Hello.", Turn("end"))])
    events = chat(agent, None)
    assert [e["type"] for e in events][-1] == "done"
    system, names = agent.provider.seen[0]
    assert "`lan`: not available this turn (10.0.0.1 is a private address" in system
    assert set(names) == {t["name"] for t in TOOLS}


# -- the API -----------------------------------------------------------------------
def test_api_per_person_on_a_server_people_sign_in_to(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH", "headers")
    root = tmp_path / "proj"
    Project(root)
    with TestClient(create_app(root)) as client:
        r = client.put("/api/me/mcp/portkey", headers=ALICE, json={
            "url": "https://mcp.portkey.ai/linear/mcp", "headers": {"x-portkey-api-key": "pk-secret-value-7777"},
        })
        assert r.status_code == 200 and r.json()["headers"] == [{"name": "x-portkey-api-key", "hint": "7777"}]
        listing = client.get("/api/me/mcp", headers=ALICE).json()
        assert [s["name"] for s in listing["servers"]] == ["portkey"] and listing["shared"] is False
        assert "pk-secret" not in json.dumps(listing)
        assert listing["private"] is False  # people sign in: private addresses are off
        assert client.get("/api/me/mcp", headers=BOB).json()["servers"] == []

        r = client.put("/api/me/mcp/portkey", headers=ALICE, json={"enabled": False})
        assert r.json()["enabled"] is False and r.json()["headers"][0]["hint"] == "7777"
        assert client.put("/api/me/mcp/x", headers=ALICE, json={"url": "nope"}).status_code == 400
        assert client.put("/api/me/mcp/new", headers=ALICE, json={}).status_code == 400

        # An agent token reads its owner's list but can't change it.
        token = client.post("/api/me/tokens", headers=ALICE, json={"name": "t"}).json()["token"]
        bearer = {"Authorization": f"Bearer {token}"}
        assert [s["name"] for s in client.get("/api/me/mcp", headers=bearer).json()["servers"]] == ["portkey"]
        assert client.delete("/api/me/mcp/portkey", headers=bearer).status_code == 403

        assert client.delete("/api/me/mcp/portkey", headers=BOB).status_code == 404
        assert client.delete("/api/me/mcp/portkey", headers=ALICE).json() == {"ok": True}


def test_api_on_a_local_server_is_one_shared_list(tmp_path, other):
    root = tmp_path / "proj"
    Project(root)
    with TestClient(create_app(root)) as client:
        listing = client.get("/api/me/mcp").json()
        assert listing == {"servers": [], "shared": True, "private": True, "presets": [], "can_edit_presets": True}
        client.put("/api/me/mcp/other", json={"url": other, "headers": {"Authorization": "Bearer s3cret"}})
        r = client.post("/api/me/mcp/other/test").json()
        assert r["ok"] and "list_nodes" in {t["name"] for t in r["tools"]}
        assert client.post("/api/me/mcp/missing/test").status_code == 404


def test_api_on_a_hub_and_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH", "headers")
    with TestClient(create_hub_app(tmp_path / "hub")) as client:
        r = client.put("/api/me/mcp/a", headers=ALICE, json={"url": "https://x.example/mcp"})
        assert r.status_code == 200
        assert [s["name"] for s in client.get("/api/me/mcp", headers=ALICE).json()["servers"]] == ["a"]
        assert client.app.state.hub.mcp.people
    monkeypatch.setenv("EXUVIA_AGENT_MCP", "off")
    with TestClient(create_hub_app(tmp_path / "hub")) as client:
        assert client.get("/api/me/mcp", headers=ALICE).status_code == 404


# -- presets: servers an admin offers, which each person connects to ----------------
def test_presets_are_offered_and_connected_one_person_at_a_time(tmp_path):
    s = store(tmp_path)
    p = s.put_preset("linear", url="https://mcp.portkey.ai/linear/mcp", description="Our tracker, through Portkey",
                     headers=["x-portkey-api-key"])
    assert p["headers"] == ["x-portkey-api-key"] and p["description"] == "Our tracker, through Portkey"
    s.put_preset("docs", url="https://docs.example/mcp")
    assert [x["name"] for x in s.presets()] == ["docs", "linear"]
    # On offer is not connected: nobody's turns use it yet.
    assert s.active("u-alice") == [] and s.active("u-bob") == []

    with pytest.raises(ValueError, match="needs a value for x-portkey-api-key"):
        s.connect("u-alice", "linear")
    with pytest.raises(ValueError, match="does not ask for Authorization"):
        s.connect("u-alice", "linear", {"x-portkey-api-key": "pk-1", "Authorization": "Bearer x"})
    with pytest.raises(LookupError):
        s.connect("u-alice", "nope")
    row = s.connect("u-alice", "linear", {"X-Portkey-Api-Key": "pk-alice-secret-0001"})
    assert row["url"] == "https://mcp.portkey.ai/linear/mcp" and row["enabled"]
    assert s.get("u-alice", "linear").headers == {"x-portkey-api-key": "pk-alice-secret-0001"}
    assert s.active("u-bob") == []
    with pytest.raises(ValueError, match="already have"):
        s.connect("u-alice", "linear", {"x-portkey-api-key": "pk-2"})
    assert [x.name for x in s.active("u-alice")] == ["linear"]
    s.connect("u-bob", "docs")
    assert s.get("u-bob", "docs").headers == {}

    # A change to the offer, or taking it back, leaves the copies alone.
    s.put_preset("linear", url="https://mcp.example/linear", headers=[])
    assert s.get("u-alice", "linear").url == "https://mcp.portkey.ai/linear/mcp"
    assert s.delete_preset("linear") and not s.delete_preset("linear")
    assert [x.name for x in s.active("u-alice")] == ["linear"]


@pytest.mark.parametrize("kw,error", [
    ({"url": "nope"}, "http:// or https://"),
    ({"url": "https://x.example/mcp", "headers": ["Content-Type"]}, "sets Content-Type itself"),
    ({"url": "https://x.example/mcp", "headers": ["a b"]}, "not a header name"),
    ({"url": "https://x.example/mcp", "headers": ["X-Key", "x-key"]}, "twice"),
    ({"url": "https://x.example/mcp", "description": "two\nlines"}, "one line"),
])
def test_bad_presets_are_refused(tmp_path, kw, error):
    with pytest.raises(ValueError, match=error):
        store(tmp_path).put_preset("ok", **kw)


def test_presets_api_on_a_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH", "headers")
    monkeypatch.setenv("EXUVIA_ADMINS", "alice")
    with TestClient(create_hub_app(tmp_path / "hub")) as client:
        client.get("/api/me", headers=BOB)  # Bob signs in once: a member
        offer = {"url": "https://mcp.example/mcp", "description": "Docs", "headers": ["Authorization"]}
        assert client.put("/api/mcp/presets/docs", headers=BOB, json=offer).status_code == 403
        r = client.put("/api/mcp/presets/docs", headers=ALICE, json=offer)
        assert r.status_code == 200 and r.json()["headers"] == ["Authorization"]
        assert client.put("/api/mcp/presets/bad", headers=ALICE, json={"url": "nope"}).status_code == 400

        # Everyone sees it on offer; nobody has it until they connect.
        listing = client.get("/api/me/mcp", headers=BOB).json()
        assert [p["name"] for p in listing["presets"]] == ["docs"] and listing["servers"] == []
        assert listing["can_edit_presets"] is False
        assert client.get("/api/me/mcp", headers=ALICE).json()["can_edit_presets"] is True
        assert client.get("/api/projects", headers=BOB).json()["agent_mcp"] == {"presets": 1, "can_edit": False}
        assert client.get("/api/projects", headers=ALICE).json()["agent_mcp"] == {"presets": 1, "can_edit": True}

        assert client.post("/api/me/mcp/presets/docs/connect", headers=BOB, json={}).status_code == 400
        r = client.post("/api/me/mcp/presets/docs/connect", headers=BOB,
                        json={"headers": {"Authorization": "Bearer bob-secret-0042"}})
        assert r.status_code == 200 and r.json()["headers"] == [{"name": "Authorization", "hint": "0042"}]
        assert "bob-secret" not in json.dumps(client.get("/api/me/mcp", headers=BOB).json())
        assert client.get("/api/me/mcp", headers=ALICE).json()["servers"] == []
        assert client.post("/api/me/mcp/presets/missing/connect", headers=BOB, json={}).status_code == 404

        # An admin's agent token can't change the offer.
        token = client.post("/api/me/tokens", headers=ALICE, json={"name": "t"}).json()["token"]
        bearer = {"Authorization": f"Bearer {token}"}
        assert client.delete("/api/mcp/presets/docs", headers=bearer).status_code == 403
        assert client.delete("/api/mcp/presets/docs", headers=ALICE).json() == {"ok": True}
        assert [s["name"] for s in client.get("/api/me/mcp", headers=BOB).json()["servers"]] == ["docs"]
    monkeypatch.setenv("EXUVIA_AGENT_MCP", "off")
    with TestClient(create_hub_app(tmp_path / "hub")) as client:
        assert client.get("/api/mcp/presets", headers=ALICE).status_code == 404
        assert "agent_mcp" not in client.get("/api/projects", headers=ALICE).json()
