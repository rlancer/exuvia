"""A project's environment variables (exuvia/envvars.py): set in the
workbench, given to every node's process, secrets never shown again and
masked in run logs."""

import asyncio
import json
import textwrap

import pytest
from fastapi.testclient import TestClient

from exuvia import envvars
from exuvia.agent import READ_TOOLS, Agent
from exuvia.envvars import EnvError, Redactor
from exuvia.server import create_app
from exuvia.workspace import Workspace

ENV_NODE = '''
import os

import exuvia

AT_IMPORT = os.environ.get("FRED_API_KEY")


@exuvia.node()
def env():
    """What the node sees, and a log line that prints the secret."""
    print("calling with key", os.environ.get("FRED_API_KEY"))
    return {"key": os.environ.get("FRED_API_KEY"), "at_import": AT_IMPORT,
            "region": os.environ.get("REGION"), "shared": os.environ.get("SHARED")}
'''

LEAKY = '''
import os

import exuvia


@exuvia.node()
def leaky():
    raise RuntimeError("bad key " + os.environ["FRED_API_KEY"])
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


def output(ws, node):
    last = ws.store.last_success(node, "")
    assert last is not None, ws.store.last_run(node)
    return json.loads((ws.project.root / last["artifact_path"]).read_text(encoding="utf-8"))


def test_a_node_gets_the_projects_variables_and_the_log_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED", "from the server")
    monkeypatch.setenv("REGION", "server-region")
    write(tmp_path, "nodes/env.py", ENV_NODE)
    ws = Workspace(tmp_path)
    ws.set_env_var("FRED_API_KEY", "fred-secret-123", secret=True)
    ws.set_env_var("REGION", "eu-west-1", secret=False)
    run(ws, ["env"])
    # Set before the module is imported; a project variable wins over the server's.
    assert output(ws, "env") == {
        "key": "fred-secret-123", "at_import": "fred-secret-123", "region": "eu-west-1", "shared": "from the server",
    }
    log = ws.store.run(ws.store.last_run("env", "")["id"])["log"]
    assert "calling with key ***" in log and "fred-secret-123" not in log


def test_a_failed_runs_error_hides_secrets(tmp_path):
    write(tmp_path, "nodes/leaky.py", LEAKY)
    ws = Workspace(tmp_path)
    ws.set_env_var("FRED_API_KEY", "fred-secret-123", secret=True)
    run(ws, ["leaky"])
    last = ws.store.run(ws.store.last_run("leaky", "")["id"])
    assert last["status"] == "failed"
    assert "bad key ***" in last["error"] and "fred-secret-123" not in last["error"] + last["log"]


def test_setting_and_listing_variables(tmp_path):
    ws = Workspace(tmp_path)
    ws.set_env_var("TOKEN", "abc-secret", secret=True)
    ws.set_env_var("MODE", "fast", secret=False)
    listed = {v["name"]: v for v in ws.env_vars()}
    assert listed["TOKEN"]["value"] is None and listed["TOKEN"]["secret"]
    assert listed["MODE"]["value"] == "fast" and not listed["MODE"]["secret"]
    # Making a plain variable secret keeps its value; the reverse needs a new one.
    ws.set_env_var("MODE", None, secret=True)
    mode = ws.store.env_vars()[0]
    assert (mode["name"], mode["value"], mode["secret"]) == ("MODE", "fast", True)
    with pytest.raises(EnvError, match="new value"):
        ws.set_env_var("TOKEN", None, secret=False)
    with pytest.raises(EnvError, match="not set yet"):
        ws.set_env_var("MISSING", None, secret=True)
    ws.set_env_var("TOKEN", "now-plain", secret=False)
    assert {v["name"]: v["value"] for v in ws.env_vars()}["TOKEN"] == "now-plain"
    ws.delete_env_var("TOKEN")
    assert [v["name"] for v in ws.env_vars()] == ["MODE"]


@pytest.mark.parametrize("name", [
    "", "1ABC", "MY-KEY", "A B", "EXUVIA_API_TOKEN", "exuvia_db", "PORTKEY_API_KEY", "UV_INDEX_CORP_PASSWORD",
    "PATH", "PYTHONPATH", "x" * 200,
])
def test_names_exuvia_keeps_for_itself_or_cannot_set_are_refused(name):
    with pytest.raises(EnvError):
        envvars.check_name(name)


def test_values_are_checked():
    assert envvars.check_value("-----BEGIN KEY-----\nabc\n-----END KEY-----").startswith("-----")
    for bad in ("a\x00b", "x" * (envvars.MAX_VALUE_CHARS + 1)):
        with pytest.raises(EnvError):
            envvars.check_value(bad)


def test_redactor():
    r = Redactor([
        {"name": "A", "value": "abcd1234", "secret": True},
        {"name": "B", "value": "abcd1234-longer", "secret": True},
        {"name": "C", "value": "plain-value", "secret": False},
        {"name": "D", "value": "on", "secret": True},  # too short to look for
        {"name": "PEM", "value": "line-one-xx\nline-two-yy", "secret": True},
    ])
    assert r("got abcd1234-longer and abcd1234") == "got *** and ***"
    assert r("plain-value stays, so does on") == "plain-value stays, so does on"
    assert r("line-two-yy\n") == "***\n"
    assert r(None) is None
    assert not Redactor([{"name": "C", "value": "plain", "secret": False}])


def test_the_api_never_sends_a_secret_back(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        assert c.get("/api/env").json() == {"variables": []}
        r = c.put("/api/env/FRED_API_KEY", json={"value": "fred-secret-123", "secret": True})
        assert r.status_code == 200 and "fred-secret-123" not in r.text
        c.put("/api/env/REGION", json={"value": "eu", "secret": False})
        got = c.get("/api/env")
        assert "fred-secret-123" not in got.text
        assert [(v["name"], v["value"], v["secret"]) for v in got.json()["variables"]] == [
            ("FRED_API_KEY", None, True), ("REGION", "eu", False),
        ]
        bad = c.put("/api/env/EXUVIA_API_TOKEN", json={"value": "x"})
        assert bad.status_code == 400 and "exuvia's own" in bad.json()["detail"]
        assert c.put("/api/env/FRED_API_KEY", json={"secret": False}).status_code == 400
        assert c.delete("/api/env/REGION").json()["variables"][0]["name"] == "FRED_API_KEY"


def test_the_agent_lists_names_but_never_a_secret(tmp_path):
    ws = Workspace(tmp_path)
    ws.set_env_var("FRED_API_KEY", "fred-secret-123", secret=True)
    ws.set_env_var("REGION", "eu", secret=False)
    agent = Agent(ws)

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        return await agent._call("list_env", {})

    ok, text, _ = asyncio.run(go())
    assert ok and "fred-secret-123" not in text
    assert json.loads(text) == {"variables": [
        {"name": "FRED_API_KEY", "secret": True, "value": None}, {"name": "REGION", "secret": False, "value": "eu"},
    ]}
    assert "list_env" in READ_TOOLS


def test_variables_are_per_project_in_a_shared_database(tmp_path, monkeypatch):
    from exuvia.db import close_shared

    monkeypatch.setenv("EXUVIA_DB", f"sqlite:///{(tmp_path / 'shared.db').as_posix()}")
    close_shared()
    try:
        a, b = Workspace(tmp_path / "a"), Workspace(tmp_path / "b")
        a.set_env_var("KEY", "for-a", secret=True)
        assert [v["name"] for v in a.env_vars()] == ["KEY"] and b.env_vars() == []
        a.store.delete_all()
        assert a.env_vars() == []
    finally:
        close_shared()
