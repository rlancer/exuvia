"""`exuvia env`: setting secrets from a terminal, never from an argument."""

import io
import os
import socket
import subprocess
import sys
import threading
import time
import types

import pytest

from exuvia import envcli
from exuvia.project import Project
from exuvia.workspace import Workspace

TOKEN = "cli-test-token"


def project(tmp_path):
    root = tmp_path / "proj"
    (root / "nodes").mkdir(parents=True)
    Project(root)
    return root


def cli(*args, stdin=None, env=None, check=True):
    run = subprocess.run(
        [sys.executable, "-m", "exuvia.cli", "env", *args],
        input=stdin, capture_output=True, text=True, env={**os.environ, **(env or {})},
    )
    if check and run.returncode:
        raise AssertionError(run.stderr)
    return run


@pytest.fixture(autouse=True)
def no_server_key(monkeypatch):
    monkeypatch.delenv("EXUVIA_SECRETS_KEY", raising=False)
    monkeypatch.delenv("EXUVIA_API_TOKEN", raising=False)


def test_a_local_project_from_a_file_and_stdin(tmp_path):
    root = project(tmp_path)
    pem = tmp_path / "key.pem"
    pem.write_bytes(b"-----BEGIN PRIVATE KEY-----\r\nAAAABBBBCCCC\r\n-----END PRIVATE KEY-----\r\n")
    out = cli("set", "SIGNING_KEY", str(root), "--from-file", str(pem)).stdout
    assert "Set SIGNING_KEY (secret, encrypted)" in out
    cli("set", "REGION", str(root), "--plain", "--stdin", stdin="eu-west-1\n")
    listing = cli("list", str(root)).stdout
    assert "SIGNING_KEY  secret (hidden)" in listing and "REGION       eu-west-1" in listing
    assert "AAAABBBB" not in listing
    rows = {r["name"]: r for r in Workspace(root)._env_rows()}
    assert rows["SIGNING_KEY"]["value"] == "-----BEGIN PRIVATE KEY-----\nAAAABBBBCCCC\n-----END PRIVATE KEY-----\n"
    assert "Removed REGION" in cli("rm", "REGION", str(root)).stdout
    assert "was not set" in cli("rm", "REGION", str(root)).stdout


def test_a_value_is_never_an_argument_and_never_read_without_a_terminal(tmp_path):
    root = project(tmp_path)
    r = cli("set", "KEY=abc123", str(root), check=False)
    assert r.returncode and "give the name only" in r.stderr
    # No terminal, no --stdin / --from-file: refused rather than read.
    r = cli("set", "KEY", str(root), stdin="piped-secret\n", check=False)
    assert r.returncode and "no terminal" in r.stderr
    assert "KEY" not in cli("list", str(root)).stdout
    r = cli("set", "EXUVIA_API_TOKEN", str(root), "--stdin", stdin="x", check=False)
    assert r.returncode and "exuvia's own settings" in r.stderr
    bad = tmp_path / "bin.key"
    bad.write_bytes(b"\xff\xfe\x00binary")
    r = cli("set", "KEY", str(root), "--from-file", str(bad), check=False)
    assert r.returncode and "not a text file" in r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal is POSIX")
def test_the_value_is_asked_for_without_echo(tmp_path):
    import pty

    root = project(tmp_path)
    # The real command on a pseudo-terminal, started without forking this
    # (threaded) test process.
    leader, follower = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "exuvia.cli", "env", "set", "TYPED_KEY", str(root)],
        stdin=follower, stdout=follower, stderr=follower, start_new_session=True,
    )
    os.close(follower)
    seen = b""
    deadline = time.monotonic() + 30
    while b"not shown" not in seen and time.monotonic() < deadline:
        seen += os.read(leader, 1024)
    os.write(leader, b"typed-secret-42\n")
    while True:
        try:
            chunk = os.read(leader, 1024)
        except OSError:  # the command exited and closed the terminal
            break
        if not chunk:
            break
        seen += chunk
    proc.wait(timeout=30)
    os.close(leader)
    assert b"Set TYPED_KEY" in seen and b"typed-secret-42" not in seen  # not echoed
    assert Workspace(root)._env_rows()[0]["value"] == "typed-secret-42"


def test_read_value():
    args = types.SimpleNamespace(name="K", from_file=None, stdin=True)
    assert envcli.read_value(args, io.StringIO("v\n")) == "v\n"


def test_urls_must_be_https_off_this_machine():
    assert envcli.check_url("https://exuvia.example.com/p/a/", False) == "https://exuvia.example.com/p/a"
    assert envcli.check_url("http://127.0.0.1:7420", False) == "http://127.0.0.1:7420"
    assert envcli.check_url("http://localhost:7420", False)
    with pytest.raises(SystemExit, match="plain http"):
        envcli.check_url("http://exuvia.example.com", False)
    assert envcli.check_url("http://10.0.0.5:7420", True)
    with pytest.raises(SystemExit, match="http"):
        envcli.check_url("ftp://x", False)


@pytest.fixture
def served(tmp_path, monkeypatch):
    import uvicorn

    from exuvia.server import create_app

    monkeypatch.setenv("EXUVIA_API_TOKEN", TOKEN)
    root = project(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(root), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    monkeypatch.delenv("EXUVIA_API_TOKEN")
    yield f"http://127.0.0.1:{port}", root
    server.should_exit = True
    thread.join(timeout=10)


def test_a_running_server_with_its_token(served):
    import httpx

    url, _root = served
    r = cli("set", "FRED_API_KEY", "--url", url, "--stdin", stdin="fred-secret-123", check=False)
    assert r.returncode and "needs a token" in r.stderr
    auth = {"EXUVIA_API_TOKEN": TOKEN}
    # A request from the agent waits until the value is set.
    httpx.post(f"{url}/api/tools/request_secret", json={"name": "FRED_API_KEY", "reason": "rates"},
               headers={"Authorization": f"Bearer {TOKEN}"})
    assert "waiting to be set: FRED_API_KEY: rates" in cli("list", "--url", url, env=auth).stdout
    out = cli("set", "FRED_API_KEY", "--url", url, "--stdin", stdin="fred-secret-123", env=auth).stdout
    assert f"Set FRED_API_KEY (secret, encrypted) in {url}" in out
    listing = cli("list", "--url", url, env=auth).stdout
    assert "FRED_API_KEY  secret (hidden)" in listing and "waiting" not in listing and "fred-secret" not in listing
    state = httpx.get(f"{url}/api/env", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert state["requests"] == [] and state["variables"][0]["value"] is None


def test_setting_from_the_folder_while_a_server_runs_it(served):
    """The folder's records are opened without a workspace: the server's runs
    are left alone, and it sees the new variable at once."""
    import httpx

    url, root = served
    cli("set", "LOCAL_KEY", str(root), "--stdin", stdin="local-secret-1")
    state = httpx.get(f"{url}/api/env", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert [(v["name"], v["readable"]) for v in state["variables"]] == [("LOCAL_KEY", True)]
