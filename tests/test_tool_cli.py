"""`exuvia tool`: the agent's tools from a terminal, for agents whose MCP
connection does not work. Also the proxy-aware HTTP settings (nethttp.py)
that it and the other remote commands share."""

import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time

import pytest

from exuvia.nethttp import client_options, loopback, proxy_hint
from exuvia.project import Project
from exuvia.toolcli import parse_input

SRC = '''import exuvia


@exuvia.node()
def nums():
    """Three numbers."""
    import pandas as pd

    return pd.DataFrame({"n": [1, 2, 3]})
'''

# A proxy that answers nothing: a workbench on this machine must not go through it.
DEAD_PROXY = "http://user:pw@proxy.invalid:3128"


def _serve(app):
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
def served(tmp_path):
    from exuvia.server import create_app

    Project(tmp_path)
    url, server, thread = _serve(create_app(tmp_path))
    yield url, tmp_path
    server.should_exit = True
    thread.join(timeout=10)


def tool(*args, env=None, stdin=None, cwd=None):
    """Run `exuvia tool ...` as a separate process, behind a dead proxy."""
    full = {k: v for k, v in os.environ.items() if k not in ("EXUVIA_URL", "NO_PROXY", "no_proxy")}
    full |= {"HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY, **(env or {})}
    return subprocess.run(
        [sys.executable, "-m", "exuvia.cli", "tool", *args],
        capture_output=True, text=True, env=full, input=stdin, cwd=cwd, timeout=300,
    )


def test_input_from_json_stdin_and_fields():
    assert parse_input(None, []) == {}
    assert parse_input('{"name": "a", "wait_seconds": 5}', ["name=b"]) == {"name": "b", "wait_seconds": 5}
    assert parse_input("-", [], stdin=io.StringIO('{"sql": "select 1"}')) == {"sql": "select 1"}
    fields = parse_input(None, ["n=42", "flag=true", "params={\"as_of\": \"2026-01-02\"}", "name=prices", "empty="])
    assert fields == {"n": 42, "flag": True, "params": {"as_of": "2026-01-02"}, "name": "prices", "empty": ""}
    with pytest.raises(SystemExit, match="not JSON"):
        parse_input("{nope", [])
    with pytest.raises(SystemExit, match="JSON object"):
        parse_input("[1]", [])
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        parse_input(None, ["novalue"])


def test_a_workbench_on_this_machine_skips_the_proxy(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    for url in ("http://127.0.0.1:7420", "http://localhost:7420/p/x", "http://[::1]:7420"):
        assert loopback(url.split("//")[1].split("/")[0].rsplit(":", 1)[0])
        assert client_options(url) == {"trust_env": False}
    remote = client_options("https://exuvia.example.com/p/research")
    assert isinstance(remote["verify"], ssl.SSLContext)  # the system trust store
    assert "trust_env" not in remote  # HTTPS_PROXY and NO_PROXY still apply
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/corp-ca.pem")
    assert client_options("https://exuvia.example.com") == {}  # httpx reads SSL_CERT_FILE itself


def test_the_proxy_hint_names_the_proxy_without_its_password(monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    assert proxy_hint("https://exuvia.example.com") == ""
    monkeypatch.setenv("HTTPS_PROXY", DEAD_PROXY)
    hint = proxy_hint("https://exuvia.example.com")
    assert "user:***@proxy.invalid" in hint and "pw" not in hint and "NO_PROXY" in hint
    assert proxy_hint("http://127.0.0.1:7420") == ""
    assert "trust store" in proxy_hint("https://exuvia.example.com", ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))


def test_tools_over_http_behind_a_proxy(served):
    url, root = served
    listed = tool("--url", url)
    assert listed.returncode == 0, listed.stderr
    names = [line for line in listed.stdout.splitlines() if not line.startswith(" ")]
    assert names[0] == "guide" and {"write_node", "run_node", "query_data"} <= set(names)

    schema = json.loads(tool("--url", url, "run_node", "--schema").stdout)
    assert schema["name"] == "run_node" and schema["input_schema"]["required"] == ["name"]

    wrote = tool("--url", url, "write_node", "-", "-a", "kind=python", stdin=json.dumps({"name": "nums", "source": SRC}))
    assert wrote.returncode == 0, wrote.stderr
    assert (root / "nodes" / "nums.py").read_text(encoding="utf-8") == SRC

    ran = tool("--url", url, "run_node", "-a", "name=nums")
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout)["rows"] == 3

    queried = tool(env={"EXUVIA_URL": url}, *["query_data", '{"sql": "select sum(n) as s from nums"}'])
    assert json.loads(queried.stdout) == [{"s": 6}]

    assert "How exuvia works" in tool("--url", url, "guide").stdout

    missing = tool("--url", url, "run_node", "-a", "name=nope")
    assert missing.returncode == 1 and missing.stdout == "" and "no node named nope" in missing.stderr
    raw = json.loads(tool("--url", url, "run_node", "-a", "name=nope", "--json").stdout)
    assert raw["ok"] is False and "no node named nope" in raw["text"]


def test_a_server_that_is_not_running(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    down = tool("--url", f"http://127.0.0.1:{port}", "run_node", "-a", "name=x")
    assert down.returncode == 2 and "could not reach exuvia" in down.stderr and "exuvia serve" in down.stderr
    listing = tool("--url", f"http://127.0.0.1:{port}")
    assert listing.returncode == 2 and listing.stdout == ""
    behind = tool("--url", "https://exuvia.invalid/p/x", "list_nodes")
    assert behind.returncode == 2 and "user:***@proxy.invalid" in behind.stderr and "pw@" not in behind.stderr
    plain = tool("--url", "http://exuvia.invalid", "list_nodes")
    assert plain.returncode != 0 and "--allow-http" in plain.stderr


def test_a_project_folder_without_a_server(tmp_path):
    Project(tmp_path)
    (tmp_path / "nodes" / "nums.py").write_text(SRC, encoding="utf-8")
    ran = tool("run_node", "-a", "name=nums", cwd=tmp_path)
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout)["rows"] == 3  # stdout holds the answer and nothing else
    elsewhere = tool("list_nodes", cwd=tmp_path / "nodes")
    assert elsewhere.returncode != 0 and "--url" in elsewhere.stderr


def test_a_hub_with_its_token(tmp_path, monkeypatch):
    from exuvia.server import create_hub_app

    monkeypatch.setenv("EXUVIA_API_TOKEN", "s3cret")
    url, server, thread = _serve(create_hub_app(tmp_path / "hub"))
    try:
        no_token = tool("--url", url, env={"EXUVIA_API_TOKEN": ""})
        assert no_token.returncode == 2 and "EXUVIA_API_TOKEN" in no_token.stderr
        made = tool("--url", url, "create_project", "-a", "id=books")
        assert made.returncode == 0, made.stderr
        tool("--url", url, "write_node", "-a", "project=books", "-a", "name=nums", "-a", "kind=python", "-a", f"source={SRC}")
        ran = tool("--url", url, "run_node", "-a", "project=books", "-a", "name=nums")
        assert ran.returncode == 0, ran.stderr
        assert json.loads(ran.stdout)["rows"] == 3
        assert "How exuvia works" in tool("--url", url, "guide", "-a", "project=books").stdout
        direct = tool("--url", f"{url}/p/books", "list_nodes")  # one project of the hub
        assert [n["name"] for n in json.loads(direct.stdout)] == ["nums"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
