"""Per-project secrets: set through the API, injected into the project's own
node runs only, never exported, and masked in logs and agent transcripts."""

import asyncio
import io
import json
import os
import stat
import sys
import tarfile
import textwrap

import pytest
from fastapi.testclient import TestClient

from exuvia.agent import Agent
from exuvia.project_secrets import ProjectSecrets, Redactor, node_environ
from exuvia.server import create_app, create_hub_app
from exuvia.workspace import Workspace

ENV_NODE = '''
import os

import exuvia


@exuvia.node()
def env():
    """Which variables this node sees."""
    print("the key is", os.environ.get("SHOP_API_KEY"))
    names = ("SHOP_API_KEY", "EXUVIA_API_TOKEN", "ANTHROPIC_API_KEY", "UV_INDEX_CORP_PASSWORD", "PLAIN_SETTING")
    return {n: os.environ.get(n) for n in names}
'''


def write(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def run(ws, targets):
    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        return await ws.run(targets, cascade=False)

    return asyncio.run(go())


@pytest.fixture
def server_env(monkeypatch):
    """exuvia's own settings and one plain variable in the server's environment."""
    monkeypatch.setenv("EXUVIA_API_TOKEN", "hub-token-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-exuvias-own")
    monkeypatch.setenv("UV_INDEX_CORP_PASSWORD", "index-pass")
    monkeypatch.setenv("PLAIN_SETTING", "shared")


def test_a_node_gets_its_projects_secrets_and_not_exuvias(tmp_path, server_env):
    write(tmp_path, "nodes/env.py", ENV_NODE)
    ws = Workspace(tmp_path)
    ws.secrets.set("SHOP_API_KEY", "shop-secret-value")
    run(ws, ["env"])
    last = ws.store.last_success("env", "")
    seen = json.loads((tmp_path / last["artifact_path"]).read_text(encoding="utf-8"))
    assert seen == {
        "SHOP_API_KEY": "shop-secret-value",
        "EXUVIA_API_TOKEN": None,
        "ANTHROPIC_API_KEY": None,
        "UV_INDEX_CORP_PASSWORD": None,
        "PLAIN_SETTING": "shared",  # the rest of the environment is unchanged
    }
    log = ws.store.run(last["id"])["log"]
    assert "the key is ***" in log and "shop-secret-value" not in log


def test_a_project_may_set_its_own_provider_key(tmp_path, server_env):
    write(tmp_path, "nodes/env.py", ENV_NODE)
    ws = Workspace(tmp_path)
    ws.secrets.set("ANTHROPIC_API_KEY", "sk-ant-the-projects")
    run(ws, ["env"])
    last = ws.store.last_success("env", "")
    seen = json.loads((tmp_path / last["artifact_path"]).read_text(encoding="utf-8"))
    assert seen["ANTHROPIC_API_KEY"] == "sk-ant-the-projects"


def test_a_secret_in_an_error_is_masked(tmp_path):
    write(tmp_path, "nodes/boom.py", '''
        import os

        import exuvia


        @exuvia.node()
        def boom():
            raise ValueError("bad key " + os.environ["DB_PASSWORD"])
    ''')
    ws = Workspace(tmp_path)
    ws.secrets.set("DB_PASSWORD", "hunter2-but-longer")
    run(ws, ["boom"])
    failed = ws.store.last_run("boom")
    assert failed["status"] == "failed"
    assert failed["error"] == "ValueError: bad key ***"
    log = ws.store.run(failed["id"])["log"]
    assert "bad key ***" in log and "hunter2-but-longer" not in log


def test_secrets_never_reach_another_project(tmp_path):
    for pid in ("a", "b"):
        write(tmp_path / pid, "nodes/env.py", ENV_NODE)
    a, b = Workspace(tmp_path / "a"), Workspace(tmp_path / "b")
    a.secrets.set("SHOP_API_KEY", "only-for-a")
    run(b, ["env"])
    seen = json.loads((tmp_path / "b" / b.store.last_success("env", "")["artifact_path"]).read_text(encoding="utf-8"))
    assert seen["SHOP_API_KEY"] is None


def test_the_api_sets_lists_and_deletes_but_never_reads_back(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/secrets").json() == {"secrets": []}
        listed = client.put("/api/secrets/SHOP_API_KEY", json={"value": "abc123xyz"}).json()["secrets"]
        assert [s["name"] for s in listed] == ["SHOP_API_KEY"] and "value" not in listed[0]
        assert "abc123xyz" not in client.get("/api/secrets").text
        assert client.put("/api/secrets/1BAD", json={"value": "x"}).status_code == 400
        assert client.put("/api/secrets/EXUVIA_API_TOKEN", json={"value": "x"}).status_code == 400
        assert client.put("/api/secrets/EMPTY", json={"value": ""}).status_code == 400
        assert client.delete("/api/secrets/NOPE").status_code == 404
        assert client.delete("/api/secrets/SHOP_API_KEY").json() == {"secrets": []}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_file_is_private_and_outside_the_code(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.put("/api/secrets/TOKEN", json={"value": "t0ken-value"})
        revisions = client.get("/api/code/revisions").json()
    path = tmp_path / ".exuvia" / "secrets.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "t0ken-value" not in json.dumps(revisions)


def test_hub_exports_leave_secrets_out_and_restores_keep_them(tmp_path):
    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        hub.post("/api/projects", json={"id": "p"})
        hub.put("/p/p/api/secrets/SHOP_API_KEY", json={"value": "kept-on-this-server"})
        blob = hub.get("/api/hub/export").content
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            names = tar.getnames()
            assert "p/.exuvia/secrets.json" not in names
            assert not any(b"kept-on-this-server" in tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())
        assert hub.post("/api/hub/import", content=blob).status_code == 200
        assert [s["name"] for s in hub.get("/p/p/api/secrets").json()["secrets"]] == ["SHOP_API_KEY"]
    with TestClient(create_hub_app(tmp_path / "elsewhere")) as fresh:
        fresh.post("/api/hub/import", content=blob)
        assert fresh.get("/p/p/api/secrets").json() == {"secrets": []}


def test_the_agent_sees_names_and_masked_values(tmp_path):
    write(tmp_path, "nodes/env.py", ENV_NODE)
    ws = Workspace(tmp_path)
    agent = Agent(ws)
    assert "This project has no secrets yet" in agent.instructions()
    ws.secrets.set("SHOP_API_KEY", "shop-secret-value")
    assert "`SHOP_API_KEY`" in agent.instructions() and "shop-secret-value" not in agent.instructions()

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        return await agent.call_tool("run_node", {"name": "env"})

    ok, text, extra = asyncio.run(go())
    assert ok and "shop-secret-value" not in text + json.dumps(extra)
    assert "***" in text



def test_a_saved_conversation_is_masked(tmp_path):
    from exuvia.llm import Turn
    from test_llm import ScriptedProvider, collect

    ws = Workspace(tmp_path)
    ws.secrets.set("SHOP_API_KEY", "shop-secret-value")
    agent = Agent(ws)
    agent.provider = ScriptedProvider([("Got shop-secret-value, thanks.", Turn("end"))])
    events = collect(agent, "my key is shop-secret-value")
    session = next(e["session_id"] for e in events if e["type"] == "session")
    saved = json.dumps(ws.store.agent_session(session))
    assert "shop-secret-value" not in saved and "my key is ***" in saved and "Got ***" in saved


def test_redactor():
    r = Redactor(["long-secret", "abc", "-----BEGIN KEY-----\nAAAABBBBCCCC\n-----END KEY-----", 'quo"te"value'])
    assert r.text("x long-secret y") == "x *** y"
    assert r.text("abc stays") == "abc stays"  # too short to mask
    assert r.text("AAAABBBBCCCC") == "***"  # a line of a multi-line value
    assert r.text(json.dumps({"v": 'quo"te"value'})) == '{"v": "***"}'
    assert not Redactor([]) and Redactor([]).text("same") == "same"


def test_node_environ_strips_exuvias_own_settings():
    env = {
        "PATH": "/bin", "HTTPS_PROXY": "http://proxy", "EXUVIA_DB": "postgresql://x", "PORTKEY_API_KEY": "k",
        "OPENROUTER_API_KEY": "k", "AWS_SECRET_ACCESS_KEY": "k", "EXUVIA_API_TOKEN_FILE": "/run/secrets/t",
        "ANTHROPIC_API_KEY_FILE": "/run/secrets/a", "UV_INDEX_CORP_USERNAME": "u", "UV_INDEX_URL": "https://idx",
    }
    assert node_environ(env) == {"PATH": "/bin", "HTTPS_PROXY": "http://proxy", "UV_INDEX_URL": "https://idx"}
    # uv keeps the index credentials it needs to install packages.
    assert "UV_INDEX_CORP_USERNAME" in node_environ(env, index=False)


def test_secrets_file_survives_a_corrupt_write(tmp_path):
    s = ProjectSecrets(tmp_path)
    (tmp_path / "secrets.json").write_text("{not json", encoding="utf-8")
    assert s.names() == []
    s.set("A_KEY", "value-1")
    assert s.values() == {"A_KEY": "value-1"}
    assert s.delete("A_KEY") and not (tmp_path / "secrets.json").exists()
    assert os.listdir(tmp_path) == []
