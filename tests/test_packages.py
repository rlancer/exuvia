"""Packages a project declares in its pyproject.toml (exuvia/packages.py).

The package comes from a private, flat index in a local folder, configured
the way a team configures its own registry ([[tool.uv.index]] plus
[tool.uv.sources]), and uv runs offline: no test here touches the network.
"""

import asyncio
import textwrap
import zipfile
from pathlib import Path

import pytest

from exuvia import packages
from exuvia.workspace import Workspace


def write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def make_wheel(folder: Path, name: str, version: str, source: str) -> None:
    """A minimal pure-Python wheel of one module."""
    mod, dist = name.replace("-", "_"), f"{name.replace('-', '_')}-{version}.dist-info"
    files = {
        f"{mod}/__init__.py": source,
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    files[f"{dist}/RECORD"] = "".join(f"{f},,\n" for f in [*files, f"{dist}/RECORD"])
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(folder / f"{mod}-{version}-py3-none-any.whl", "w") as z:
        for path, text in files.items():
            z.writestr(path, text)


def pyproject(root: Path, index: Path, deps: list[str], extra: str = "") -> None:
    listed = ", ".join(f'"{d}"' for d in deps)
    write(root, "pyproject.toml", f'''
        [project]
        name = "research"
        version = "0"
        requires-python = ">=3.11"
        dependencies = [{listed}]
        {extra}

        [[tool.uv.index]]
        name = "internal"
        url = "{index.as_posix()}"
        format = "flat"
        explicit = true

        [tool.uv.sources]
        acme-internal = {{ index = "internal" }}
    ''')


def run(ws: Workspace, targets: list[str]) -> dict:
    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        return await ws.run(targets, cascade=False, force=set(targets))

    return asyncio.run(go())


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")
    index = tmp_path / "wheels"
    make_wheel(index, "acme-internal", "1.0", "ANSWER = 42\n")
    root = tmp_path / "proj"
    pyproject(root, index, ["acme-internal"])
    write(root, "nodes/answer.py", '''
        import exuvia

        @exuvia.node()
        def answer():
            import acme_internal
            import pandas as pd

            return pd.DataFrame({"answer": [acme_internal.ANSWER]})
    ''')
    ws = Workspace(root)
    yield root, index, ws
    ws.executor.close()


def installs(root: Path) -> list[Path]:
    cache = root / ".exuvia" / "packages"
    return sorted(p for p in cache.iterdir() if not p.name.startswith(".")) if cache.is_dir() else []


def test_declared_package_from_a_private_index(project):
    root, _, ws = project
    finals = run(ws, ["answer"])
    assert finals["answer"]["status"] == "success", finals["answer"].get("error")
    assert ws.preview("answer")["rows"] == [[42]]
    assert "installing the packages in pyproject.toml: acme-internal" in ws.store.run(finals["answer"]["id"])["log"]
    [install] = installs(root)
    names = {p.name for p in install.iterdir()}
    assert "acme_internal" in names
    assert "pandas" not in names  # exuvia's own packages are not installed again


def test_install_is_reused_then_replaced_when_pyproject_changes(project):
    root, index, ws = project
    run(ws, ["answer"])
    [first] = installs(root)
    finals = run(ws, ["answer"])
    assert "installing the packages" not in ws.store.run(finals["answer"]["id"])["log"]
    make_wheel(index, "acme-internal", "2.0", "ANSWER = 43\n")
    pyproject(root, index, ["acme-internal>=2"])
    finals = run(ws, ["answer"])
    assert finals["answer"]["status"] == "success", finals["answer"].get("error")
    assert ws.preview("answer")["rows"] == [[43]]
    [second] = installs(root)
    assert second != first  # the old install is pruned


def test_conflict_with_exuvias_own_versions_fails_the_run(project):
    root, index, ws = project
    pyproject(root, index, ["acme-internal", "pandas<1"])
    finals = run(ws, ["answer"])
    assert finals["answer"]["status"] == "failed"
    error = finals["answer"]["error"]
    assert error.startswith("the packages in pyproject.toml could not be installed: ")
    assert "pandas<1" in error and "the == versions are the ones exuvia runs with" in error
    assert installs(root) == []


def test_no_dependencies_installs_nothing(tmp_path):
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\nversion = "0"\ndependencies = []\n')
    assert packages.ensure(tmp_path, tmp_path / "cache", print) is None
    assert not (tmp_path / "cache").exists()


def test_broken_pyproject_is_reported(tmp_path):
    write(tmp_path, "pyproject.toml", "[project\n")
    with pytest.raises(packages.PackagesError, match="does not parse"):
        packages.declared(tmp_path)


def test_init_writes_a_pyproject(tmp_path, monkeypatch, capsys):
    import sys

    from exuvia import cli

    monkeypatch.setattr(sys, "argv", ["exuvia", "init", str(tmp_path / "My Research")])
    cli.main()
    root = tmp_path / "My Research"
    assert 'name = "my-research"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert packages.declared(root) == []
    assert ".venv/" in (root / ".gitignore").read_text(encoding="utf-8")
    assert packages.dist_name("...") == "exuvia-project"


# -- the live manager: edits, requests, pre-building ------------------------------
def wait_settled(pk: packages.Packages, timeout: float = 60) -> dict:
    import time

    end = time.time() + timeout
    while time.time() < end:
        st = pk.state()
        if st["status"] not in ("installing", "pending"):
            return st
        time.sleep(0.05)
    raise AssertionError(f"still {st['status']}")


def test_add_pins_a_bare_name_and_prebuilds(project, monkeypatch):
    from importlib import metadata

    root, index, ws = project
    pyproject(root, index, [])
    # One of exuvia's own packages, served locally at its installed version
    # (uv runs offline, so resolving it cannot reach PyPI).
    links = root.parent / "links"
    make_wheel(links, "tzdata", metadata.version("tzdata"), "")
    monkeypatch.setenv("UV_FIND_LINKS", str(links))
    events = []
    pk = packages.Packages(root, publish=events.append)
    st = pk.add(["acme-internal", "tzdata"])
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"acme-internal==1.0"' in text  # pinned to the version that resolved
    assert '"tzdata"' in text  # exuvia's own: left to follow exuvia
    st = wait_settled(pk)  # installed in the background, before any run
    assert st["status"] == "ready" and st["installed"] == 1
    rows = {p["name"]: (p["version"], p["source"]) for p in st["packages"]}
    assert rows["acme-internal"] == ("1.0", "project")
    assert rows["tzdata"][1] == "exuvia"
    assert {"type": "packages"} in events


def test_an_edit_that_does_not_resolve_changes_nothing(project):
    root, index, ws = project
    before = (root / "pyproject.toml").read_text(encoding="utf-8")
    pk = packages.Packages(root)
    with pytest.raises(packages.PackagesError, match="pandas"):
        pk.add(["pandas<1"])
    with pytest.raises(packages.PackagesError, match="not a package requirement"):
        pk.add(["--index-url=https://evil.example/simple"])
    with pytest.raises(packages.PackagesError, match="not in pyproject.toml"):
        pk.remove(["scipy"])
    assert (root / "pyproject.toml").read_text(encoding="utf-8") == before


def test_remove_and_a_project_without_pyproject(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_OFFLINE", "1")
    index = tmp_path / "wheels"
    make_wheel(index, "acme-internal", "1.0", "ANSWER = 42\n")
    root = tmp_path / "hub-project"
    root.mkdir()
    pk = packages.Packages(root)
    assert pk.state()["status"] == "none" and pk.state()["manifest"] is None
    # A project with no pyproject.toml (one a hub created) gets one on the first add.
    with pytest.raises(packages.PackagesError):
        pk.add(["acme-internal"])  # no index configured: rejected, and no file left behind
    assert not (root / "pyproject.toml").exists()
    pyproject(root, index, [])
    pk.add(["acme-internal"])
    assert packages.declared(root) == ["acme-internal==1.0"]
    pk.remove(["acme-internal"])
    assert packages.declared(root) == []


def test_a_failed_list_fails_runs_fast_until_retried(project, monkeypatch):
    root, index, ws = project
    pyproject(root, index, ["acme-internal>=9"])  # no such version
    first = run(ws, ["answer"])["answer"]
    assert first["status"] == "failed" and "could not be installed" in first["error"]
    calls = []
    monkeypatch.setattr(packages, "ensure", lambda *a, **k: calls.append(1))
    assert run(ws, ["answer"])["answer"]["status"] == "failed"
    assert calls == []  # the earlier failure, without running uv again
    assert ws.packages.state()["status"] == "failed"
    monkeypatch.undo()
    monkeypatch.setenv("UV_OFFLINE", "1")
    make_wheel(index, "acme-internal", "9.0", "ANSWER = 9\n")
    ws.packages.retry()
    assert wait_settled(ws.packages)["status"] == "ready"


def test_agent_requests_wait_for_approval(project, monkeypatch):
    root, index, ws = project
    pyproject(root, index, [])
    pk = packages.Packages(root)
    res = pk.request(["acme-internal"], "the answer node imports it", origin="agent")
    assert res["status"] == "requested"
    assert packages.declared(root) == []  # nothing changes until a person approves
    [req] = pk.state()["requests"]
    assert req["packages"] == ["acme-internal"] and req["reason"] == "the answer node imports it"
    pk.approve(req["id"])
    assert packages.declared(root) == ["acme-internal==1.0"] and pk.state()["requests"] == []
    pk.request(["six"], "")
    pk.dismiss(pk.state()["requests"][0]["id"])
    assert pk.state()["requests"] == []

    monkeypatch.setenv("EXUVIA_AGENT_PACKAGES", "off")
    with pytest.raises(packages.PackagesError, match="turned off"):
        pk.request(["six"], "")
    monkeypatch.setenv("EXUVIA_AGENT_PACKAGES", "install")
    pyproject(root, index, [])
    assert pk.request(["acme-internal"], "")["status"] == "added"
    assert packages.declared(root) == ["acme-internal==1.0"]


def test_agent_tools(project):
    from exuvia.agent import Agent

    root, index, ws = project
    pyproject(root, index, [])
    agent = Agent(ws)

    async def call(name, args):
        return await agent.call_tool(name, args)

    ok, text, _ = asyncio.run(call("request_packages", {"packages": "acme-internal", "reason": "x"}))
    assert not ok and "list" in text
    ok, text, _ = asyncio.run(call("request_packages", {"packages": ["acme-internal"], "reason": "the answer node"}))
    assert ok and "approves it in the workbench" in text
    ok, text, _ = asyncio.run(call("list_packages", {}))
    assert ok and '"requests"' in text and "acme-internal" in text
    assert "request_packages" in agent.instructions()


def test_missing_module_is_offered_as_a_package(project):
    root, index, ws = project
    write(root, "nodes/answer.py", '''
        import exuvia

        @exuvia.node()
        def answer():
            import sklearn
            return None
    ''')
    ws.reload()
    finals = run(ws, ["answer"])
    assert finals["answer"]["status"] == "failed"
    [node] = [n for n in ws.snapshot()["nodes"] if n["name"] == "answer"]
    assert node["missing_package"] == {"module": "sklearn", "package": "scikit-learn"}
    assert packages.missing_package("No module named 'lib.helpers'") is None  # the project's own code
    assert packages.missing_package("ValueError: nope") is None


def test_packages_api_and_revision(project):
    from fastapi.testclient import TestClient

    from exuvia.server import create_app

    root, index, ws = project
    ws.executor.close()
    pyproject(root, index, [])
    with TestClient(create_app(root)) as c:
        assert c.get("/api/packages").json()["status"] == "none"
        bad = c.post("/api/packages", json={"add": ["pandas<1"]})
        assert bad.status_code == 400 and "pandas" in bad.json()["detail"]
        st = c.post("/api/packages", json={"add": ["acme-internal"]}).json()
        assert st["packages"][0]["requirement"] == "acme-internal==1.0"
        revs = c.get("/api/code/revisions", params={"path": "pyproject.toml"}).json()
        assert revs, "pyproject.toml edits are kept as code revisions"
        assert c.put("/api/packages/manifest", json={"text": "[project\n"}).status_code == 400
        assert c.post("/api/packages/requests/99/approve").status_code == 404
        assert c.post("/api/packages", json={"remove": ["acme-internal"]}).json()["packages"] == []


def test_hub_export_leaves_installed_packages_out(project):
    from exuvia.hub import Hub

    root, index, ws = project
    run(ws, ["answer"])
    assert installs(root)
    import tarfile

    hub = Hub(root.parent)
    with tarfile.open(hub.export()) as tar:
        names = tar.getnames()
    assert any(n.endswith("pyproject.toml") for n in names)
    assert not [n for n in names if "/.exuvia/packages/" in n]


def test_index_credentials_from_files(tmp_path):
    from exuvia.cli import load_secret_files

    secret = tmp_path / "pw"
    secret.write_text("s3cret\n", encoding="utf-8")
    env = {"UV_INDEX_INTERNAL_PASSWORD_FILE": str(secret), "UV_INDEX_INTERNAL_URL_FILE": str(secret)}
    load_secret_files(env)
    assert env["UV_INDEX_INTERNAL_PASSWORD"] == "s3cret"
    assert "UV_INDEX_INTERNAL_URL" not in env  # only credentials read files


def test_a_server_wide_index_stays_out_of_the_project(project, monkeypatch):
    """UV_INDEX on the server applies to installs, but `uv add` must not copy
    it into each project's pyproject.toml."""
    root, index, ws = project
    write(root, "pyproject.toml", '[project]\nname = "r"\nversion = "0"\nrequires-python = ">=3.11"\ndependencies = []\n')
    # The wheel comes from a local folder (UV_FIND_LINKS; uv runs offline), so the
    # index itself is never reached: only whether it is written down matters.
    monkeypatch.setenv("UV_FIND_LINKS", str(index))
    monkeypatch.setenv("UV_INDEX", "internal=https://pypi.invalid/simple")
    pk = packages.Packages(root)
    pk.add(["acme-internal"])
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "tool.uv" not in text and "pypi.invalid" not in text
    assert packages.declared(root) == ["acme-internal==1.0"]
