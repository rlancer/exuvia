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
from exuvia.cli import _dist_name
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
    assert _dist_name("...") == "exuvia-project"
