"""The one client for commands that call a running exuvia (exuvia/remote.py):
address checks, the token, and one "could not reach" message, with the
proxy hint, wherever the call came from."""

import asyncio

import pytest

from exuvia import remote
from exuvia.envcli import Remote
from exuvia.mcp_server import HubBackend, RemoteBackend
from exuvia.mirror import MirrorError, RemoteSource
from exuvia.pushing import PushError, cancel_run, push

DEAD_PROXY = "http://user:pw@proxy.invalid:3128"  # answers nothing
FAR = "https://exuvia.invalid"


@pytest.fixture
def behind_a_dead_proxy(monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", DEAD_PROXY)


def said_proxy(text):
    return text.startswith("could not reach") and "user:***@proxy.invalid" in text and "pw@" not in text


def test_check_url_raises_value_error():
    assert remote.check_url("https://exuvia.example.com/p/a/") == "https://exuvia.example.com/p/a"
    assert remote.check_url("http://[::1]:7420") == "http://[::1]:7420"
    with pytest.raises(ValueError, match="plain http"):
        remote.check_url("http://exuvia.example.com")
    assert remote.check_url("http://10.0.0.5:7420", allow_http=True)
    with pytest.raises(ValueError, match="http"):
        remote.check_url("ftp://x")


def test_the_token_comes_from_the_environment_or_its_file(tmp_path, monkeypatch):
    monkeypatch.delenv("EXUVIA_API_TOKEN", raising=False)
    monkeypatch.delenv("EXUVIA_API_TOKEN_FILE", raising=False)
    assert remote.token() is None and remote.token("given") == "given"
    (tmp_path / "tok").write_text("from-file\n")
    monkeypatch.setenv("EXUVIA_API_TOKEN_FILE", str(tmp_path / "tok"))
    assert remote.token() == "from-file"
    monkeypatch.setenv("EXUVIA_API_TOKEN", "from-env")
    assert remote.token() == "from-env"
    monkeypatch.delenv("EXUVIA_API_TOKEN")
    monkeypatch.setenv("EXUVIA_API_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="EXUVIA_API_TOKEN_FILE"):
        remote.token()


def test_connect_sends_the_token_and_origin(monkeypatch):
    monkeypatch.setenv("EXUVIA_API_TOKEN", "s3cret")
    with remote.connect("http://127.0.0.1:7420/p/a/", origin="test") as http:
        assert http.where == "http://127.0.0.1:7420/p/a"
        assert http.headers["Authorization"] == "Bearer s3cret" and http.headers["X-Exuvia-Origin"] == "test"
        assert str(http.build_request("GET", "/api/guide").url) == "http://127.0.0.1:7420/p/a/api/guide"
    with remote.connect("http://127.0.0.1:7420", origin="test", token=None) as http:
        assert "Authorization" not in http.headers


def test_every_command_names_the_proxy_when_it_cannot_reach_exuvia(behind_a_dead_proxy):
    with pytest.raises(PushError) as e:
        push(f"{FAR}/p/x", "prices", {"a": 1})
    assert said_proxy(str(e.value))
    with pytest.raises(PushError) as e:
        cancel_run(f"{FAR}/p/x", "r1")
    assert said_proxy(str(e.value))
    with pytest.raises(MirrorError) as e:
        RemoteSource(f"{FAR}/p/x").revisions_after(None)
    assert said_proxy(str(e.value))
    with pytest.raises(SystemExit) as e:
        Remote(f"{FAR}/p/x", False).state()
    assert said_proxy(str(e.value))

    async def relay():
        hub, project = HubBackend(FAR), RemoteBackend(f"{FAR}/p/x")
        try:
            return [
                await hub.guide({"project": "a"}),
                await hub._hub_about(),
                await hub.call("list_projects", {}),
                await hub.call("list_nodes", {"project": "a"}),
                await project.call("list_nodes", {}),
            ]
        finally:
            await hub.stop()
            await project.stop()

    for ok, text in asyncio.run(relay()):
        assert not ok and said_proxy(text), text
