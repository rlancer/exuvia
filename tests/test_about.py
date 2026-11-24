"""GET /api/about: the version, how exuvia was installed, and the update check
the help menu shows. No test reaches PyPI: ``_fetch`` is replaced."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from exuvia import about
from exuvia.server import create_app, create_hub_app


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("EXUVIA_DEPLOYED", "EXUVIA_UPDATE_CHECK", "EXUVIA_API_TOKEN", "EXUVIA_AUTH", "EXUVIA_DB"):
        monkeypatch.delenv(name, raising=False)
    about._cache.update(latest=None, error=None, checked_at=0.0)
    about._lock = None
    calls: list[int] = []

    async def fetch() -> str:
        calls.append(1)
        return "0.4.0"

    monkeypatch.setattr(about, "_fetch", fetch)
    monkeypatch.setattr(about, "__version__", "0.3.5")
    return calls


def run(coro):
    return asyncio.run(coro)


def test_versions_compare_as_numbers():
    assert about.is_newer("0.3.10", "0.3.9")
    assert not about.is_newer("0.3.9", "0.3.10")
    assert not about.is_newer("0.3.9", "0.3.9")
    assert about.parse("0.3.9.dev2+g1a2b3c") is None
    assert about.parse("0.0.0") is None
    assert not about.is_newer("1.0.0rc1", "0.3.9")  # a pre-release is not offered
    assert not about.is_newer(None, "0.3.9")


def test_install_kind(monkeypatch):
    monkeypatch.setattr(about.metadata, "distribution", lambda _: type("D", (), {"read_text": lambda self, _n: None})())
    for prefix, kind in [
        ("/home/a/.local/share/uv/tools/exuvia", "uv-tool"),
        (r"C:\Users\a\AppData\Roaming\uv\tools\exuvia", "uv-tool"),
        ("/home/a/.cache/uv/archive-v0/AbC123", "uvx"),
        ("/home/a/.local/share/pipx/venvs/exuvia", "pipx"),
        ("/home/a/project/.venv", "pip"),
    ]:
        monkeypatch.setattr(about.sys, "prefix", prefix)
        assert about.install_kind() == kind, prefix
    monkeypatch.setenv("EXUVIA_DEPLOYED", "1")
    assert about.install_kind() == "deployed"


def test_editable_install_is_source(monkeypatch):
    raw = '{"url": "file:///src/exuvia", "dir_info": {"editable": true}}'
    monkeypatch.setattr(about.metadata, "distribution", lambda _: type("D", (), {"read_text": lambda self, _n: raw})())
    assert about.install_kind() == "source"
    assert run(about.about())["upgrade"] == "git pull && uv sync"


def test_a_newer_release_says_how_to_update(monkeypatch):
    monkeypatch.setattr(about, "install_kind", lambda: "uv-tool")
    a = run(about.about())
    assert a["update"] == "available" and a["latest"] == "0.4.0" and a["version"] == "0.3.5"
    assert a["upgrade"] == "uv tool upgrade exuvia"
    assert a["changes_url"].endswith("/compare/v0.3.5...v0.4.0")
    assert not a["deployed"]


def test_uvx_names_the_command_the_server_runs(monkeypatch):
    monkeypatch.setattr(about, "install_kind", lambda: "uvx")
    assert run(about.about(command="hub"))["upgrade"] == "uvx exuvia@latest hub <folder>"


def test_a_deployed_server_gives_no_command(monkeypatch):
    monkeypatch.setenv("EXUVIA_DEPLOYED", "true")
    a = run(about.about())
    assert a["deployed"] and a["install"] == "deployed"
    assert a["upgrade"] is None and a["image"] == "ghcr.io/rlancer/exuvia"
    assert a["update"] == "available"


def test_up_to_date(monkeypatch):
    monkeypatch.setattr(about, "__version__", "0.4.0")
    a = run(about.about())
    assert a["update"] == "current" and a["changes_url"] is None


def test_checks_are_cached(clean, monkeypatch):
    run(about.about())
    run(about.about())
    assert len(clean) == 1
    run(about.about(refresh=True))  # "Check again" right away: still the cached answer
    assert len(clean) == 1
    about._cache["checked_at"] -= about.REFRESH_MIN + 1
    run(about.about(refresh=True))
    assert len(clean) == 2


def test_a_failed_check_does_not_fail_the_answer(monkeypatch):
    async def offline() -> str:
        raise OSError("no route")

    monkeypatch.setattr(about, "_fetch", offline)
    a = run(about.about())
    assert a["update"] == "error" and "OSError" in a["error"] and a["version"] == "0.3.5"


def test_checks_can_be_turned_off(clean, monkeypatch):
    monkeypatch.setenv("EXUVIA_UPDATE_CHECK", "off")
    a = run(about.about())
    assert a["update"] == "off" and a["latest"] is None
    assert not clean


def test_a_development_build_is_not_compared(clean, monkeypatch):
    monkeypatch.setattr(about, "__version__", "0.3.6.dev3+g1a2b3c")
    assert run(about.about())["update"] == "dev"
    assert not clean


def test_the_endpoint_on_serve_and_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(about, "install_kind", lambda: "uvx")
    with TestClient(create_app(tmp_path / "p")) as client:
        a = client.get("/api/about").json()
        assert a["update"] == "available" and a["upgrade"] == "uvx exuvia@latest serve <folder>"
    with TestClient(create_hub_app(tmp_path / "hub")) as client:
        assert client.get("/api/about").json()["upgrade"] == "uvx exuvia@latest hub <folder>"


def test_the_endpoint_needs_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_API_TOKEN", "s3cret")
    with TestClient(create_app(tmp_path / "p")) as client:
        assert client.get("/api/about").status_code == 401
        assert client.get("/api/about", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_the_uvx_command_runs_what_this_process_runs(monkeypatch):
    monkeypatch.setattr(about, "install_kind", lambda: "uvx")
    a = run(about.about(command="mcp", target="--url https://hub.example.com"))
    assert a["upgrade"] == "uvx exuvia@latest mcp --url https://hub.example.com"
