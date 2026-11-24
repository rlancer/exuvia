"""npm packages for views: package.json, fetching a package's modules from
the CDN through the cache, and the routes, agent tool and view hash around
them. The CDN is a fake that answers like esm.sh."""

import asyncio
import io
import json
import os
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exuvia import npm
from exuvia.agent import Agent
from exuvia.npm import CDN, QUERY, Npm, NpmError, bare_imports, declared
from exuvia.server import create_app
from exuvia.workspace import Workspace

STUB = f"/chartlib@1.2.3?{QUERY}"
BUNDLE = "/chartlib@1.2.3/X-ZXh0/es2022/chartlib.bundle.mjs"
HELPER = "/chartlib@1.2.3/X-ZXh0/es2022/helper.mjs"
POLYFILL = "/node/process.mjs"
CDN_FILES = {
    STUB: f'/* esm.sh - chartlib@1.2.3 */\nexport * from "{BUNDLE}";\nexport {{ default }} from "{BUNDLE}";\n',
    f"/chartlib@latest?{QUERY}": f'/* esm.sh - chartlib@1.2.3 */\nexport * from "{BUNDLE}";\n',
    f"/chartlib@%5E1?{QUERY}": f'/* esm.sh - chartlib@1.2.3 */\nexport * from "{BUNDLE}";\n',
    # Minified, with an import in a string that is not one and a relative import.
    BUNDLE: (
        'import{useState as u}from"react";import{twice}from"./helper.mjs";import"/node/process.mjs";'
        'var s=" from "+x;export function Chart(){return u(twice(2))}'
    ),
    HELPER: "export const twice=n=>n*2;",
    POLYFILL: "export default {};",
    "/chartlib@1.2.3/dist/chart.css": ".chart{color:red}",
}


class FakeCdn:
    def __init__(self, files: dict[str, str]):
        self.files = dict(files)
        self.calls: list[str] = []
        self.down = False

    def __call__(self, url: str) -> tuple[str, dict[str, str]]:
        self.calls.append(url)
        if self.down:
            raise NpmError(f"could not reach {CDN}: down")
        path = url[len(CDN):]
        if path not in self.files:
            raise NpmError(f"{url}: 404 Not Found")
        return self.files[path], {}


@pytest.fixture
def cdn(monkeypatch):
    fake = FakeCdn(CDN_FILES)
    monkeypatch.setattr(npm, "_fetch", fake)
    return fake


APP = 'import "chartlib/dist/chart.css";\nimport { Chart } from "chartlib";\nimport x from "leftpad";\nexport default Chart;\n'


def project(root: Path, deps: dict[str, str] | None = None) -> Path:
    (root / "nodes").mkdir(parents=True)
    (root / "exuvia.json").write_text(json.dumps({"name": "p"}), encoding="utf-8")
    view = root / "views" / "board"
    view.mkdir(parents=True)
    (view / "view.json").write_text(json.dumps({"title": "Board"}), encoding="utf-8")
    (view / "App.tsx").write_text(APP, encoding="utf-8")
    if deps is not None:
        (root / "package.json").write_text(json.dumps({"name": "p", "dependencies": deps}), encoding="utf-8")
    return root


def test_add_pins_a_bare_name_keeps_a_range_and_refuses_what_it_cannot_serve(tmp_path, cdn):
    root = project(tmp_path, {})
    n = Npm(root)
    st = n.add(["chartlib"])
    assert st["packages"] == [{"name": "chartlib", "version": "1.2.3", "exact": True}]
    n.add(["chartlib@^1"])
    assert declared(root) == {"chartlib": "^1"}
    before = (root / "package.json").read_text(encoding="utf-8")
    for bad, why in (("react", "comes with exuvia"), ("Not A Name", "not an npm package name"),
                     ("nope", "404"), ("chartlib@github:x/y", "not a version")):
        with pytest.raises(NpmError, match=why):
            n.add([bad])
    assert (root / "package.json").read_text(encoding="utf-8") == before  # nothing half-written
    n.remove(["chartlib"])
    assert declared(root) == {}
    assert json.loads((root / "package.json").read_text(encoding="utf-8"))["name"] == "p"  # other keys stay
    with pytest.raises(NpmError, match="not in package.json"):
        n.remove(["chartlib"])


def test_modules_walks_a_package_and_leaves_out_what_is_not_listed(tmp_path, cdn):
    root = project(tmp_path, {"chartlib": "1.2.3", "gone": "1.0.0"})
    specs = bare_imports({"App.tsx": APP + 'import "gone";\nimport React from "react";\n', "s.css": 'from "x"'})
    assert specs == {"chartlib/dist/chart.css", "chartlib", "leftpad", "gone", "react"}
    got = Npm(root).modules(specs)
    assert got["declared"] == ["chartlib", "gone"]
    assert got["entries"] == {"chartlib": STUB, "chartlib/dist/chart.css": "/chartlib@1.2.3/dist/chart.css"}
    assert set(got["modules"]) == {STUB, BUNDLE, HELPER, POLYFILL, "/chartlib@1.2.3/dist/chart.css"}
    assert "404" in got["errors"]["gone"]
    assert "leftpad" not in got["errors"] and "react" not in got["errors"]  # the frame reports those


def test_the_cache_keeps_exact_versions_and_refreshes_ranges(tmp_path, cdn):
    root = project(tmp_path, {"chartlib": "^1"})
    n = Npm(root)
    n.modules({"chartlib"})
    first = len(cdn.calls)
    n.modules({"chartlib"})
    assert len(cdn.calls) == first  # all cached, the range for an hour
    for f in n.store.iterdir():  # an hour later
        os.utime(f, (time.time() - npm.REFRESH_S - 1,) * 2)
    n.modules({"chartlib"})
    refetched = {c[len(CDN):] for c in cdn.calls[first:]}
    assert f"/chartlib@%5E1?{QUERY}" in refetched and POLYFILL in refetched
    assert BUNDLE not in refetched and HELPER not in refetched  # exact versions never change
    cdn.down = True
    for f in n.store.iterdir():
        os.utime(f, (time.time() - npm.REFRESH_S - 1,) * 2)
    assert set(n.modules({"chartlib"})["modules"]) == {STUB.replace("1.2.3", "%5E1"), BUNDLE, HELPER, POLYFILL}


def test_routes_view_hash_revisions_and_export(tmp_path, cdn):
    root = project(tmp_path)
    with TestClient(create_app(root)) as client:
        before = client.get("/api/views").json()["views"][0]["hash"]
        assert client.get("/api/npm").json()["packages"] == []
        r = client.post("/api/npm", json={"add": ["chartlib"]})
        assert r.status_code == 200, r.text
        assert r.json()["packages"][0]["version"] == "1.2.3"
        assert client.post("/api/npm", json={"add": ["react"]}).status_code == 400
        # A view that may import new packages is a new view: its frame rebuilds.
        assert client.get("/api/views").json()["views"][0]["hash"] != before
        got = client.get("/api/views/board/packages").json()
        assert got["entries"]["chartlib"] == STUB and BUNDLE in got["modules"]
        assert client.get("/api/views/nope/packages").status_code == 404
        revs = client.get("/api/code/revisions").json()
        assert any(r.get("path") == "package.json" for r in revs)
        archive = zipfile.ZipFile(io.BytesIO(client.get("/api/code/archive").content))
        assert any(n.endswith("package.json") for n in archive.namelist())


def test_agent_adds_view_packages_and_sees_them(tmp_path, cdn, monkeypatch):
    ws = Workspace(project(tmp_path))
    agent = Agent(ws)
    ok, text, _ = asyncio.run(agent.call_tool("add_view_packages", {"packages": ["chartlib"]}))
    assert ok and "chartlib@1.2.3" in text
    ok, text, _ = asyncio.run(agent.call_tool("list_packages", {}))
    assert json.loads(text)["view_packages"] == [{"name": "chartlib", "version": "1.2.3", "exact": True}]
    assert "`chartlib` 1.2.3" in agent.instructions()
    ok, text, _ = asyncio.run(agent.call_tool("add_view_packages", {"packages": ["react-dom"]}))
    assert not ok and "comes with exuvia" in text
    monkeypatch.setenv("EXUVIA_AGENT_PACKAGES", "off")
    ok, text, _ = asyncio.run(agent.call_tool("add_view_packages", {"packages": ["chartlib"]}))
    assert not ok and "turned off" in text
