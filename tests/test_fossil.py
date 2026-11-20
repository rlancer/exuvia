"""The Fossil mirror of code revisions (exuvia/fossil.py). Skipped where the
fossil binary is not installed."""

import asyncio
import base64
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from exuvia.fossil import fossil_binary
from exuvia.server import create_app, create_hub_app
from exuvia.workspace import Workspace, acting_as

FOSSIL = shutil.which("fossil")
pytestmark = pytest.mark.skipif(FOSSIL is None, reason="fossil is not installed")

NODE = '''import exuvia


@exuvia.node()
def nums():
    import pandas as pd

    return pd.DataFrame({"n": [1, 2, 3]})
'''
TOKEN = "s3cret"


@pytest.fixture(autouse=True)
def _fossil_on(monkeypatch):
    monkeypatch.setenv("EXUVIA_FOSSIL", "auto")
    monkeypatch.delenv("EXUVIA_DB", raising=False)


def write(ws: Workspace, name: str, source: str, origin: str) -> None:
    with acting_as(origin):
        with ws.editing():
            ws.project.write_node(name, source, "python")
            ws.reload()


def checkins(repo) -> list[tuple[str, str]]:
    """(user, comment) of every check-in, oldest first."""
    db = sqlite3.connect(f"file:{repo}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "select coalesce(euser, user), coalesce(ecomment, comment) from event where type='ci' order by mtime"
        ).fetchall()
    finally:
        db.close()
    return rows


def fossil(*args: str, cwd=None) -> str:
    return subprocess.run([FOSSIL, *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def tip(repo) -> str:
    db = sqlite3.connect(f"file:{repo}?mode=ro", uri=True)
    try:
        return db.execute(
            "select blob.uuid from event join blob on blob.rid=event.objid where type='ci' order by mtime desc limit 1"
        ).fetchone()[0]
    finally:
        db.close()


def test_binary_setting(monkeypatch):
    monkeypatch.setenv("EXUVIA_FOSSIL", "off")
    assert fossil_binary() is None
    monkeypatch.setenv("EXUVIA_FOSSIL", "auto")
    assert fossil_binary() == FOSSIL
    monkeypatch.setenv("EXUVIA_FOSSIL", FOSSIL)
    assert fossil_binary() == FOSSIL
    monkeypatch.setenv("EXUVIA_FOSSIL", "/no/such/fossil")
    assert fossil_binary() is None


def test_each_save_is_a_checkin_by_its_origin(tmp_path):
    ws = Workspace(tmp_path / "proj")
    assert ws.fossil is not None
    write(ws, "nums", NODE, "agent")
    write(ws, "nums", NODE.replace("[1, 2, 3]", "[4, 5]"), "api")
    with acting_as("ui"):
        with ws.editing():
            ws.project.delete_node("nums")
            ws.reload()
    assert ws.fossil.sync() == 3, ws.fossil.last_error
    assert ws.fossil.sync() == 0  # nothing new

    rows = checkins(ws.fossil.repo)
    assert rows[0] == ("exuvia", "initial empty check-in")
    assert rows[1:] == [
        ("agent", "agent: nodes/nums.py"),
        ("api", "api: nodes/nums.py"),
        ("ui", "ui: delete nodes/nums.py"),
    ]
    # The second check-in holds the second version; the last has no file.
    prev = fossil("cat", "nodes/nums.py", "-r", "prev", cwd=ws.fossil.checkout)
    assert "[4, 5]" in prev
    assert fossil("ls", cwd=ws.fossil.checkout).strip() == ""
    # Check-ins carry the time each revision was saved.
    saved = sorted(r["saved_at"] for r in ws.store.code_revisions(limit=10))
    db = sqlite3.connect(f"file:{ws.fossil.repo}?mode=ro", uri=True)
    mtimes = [(m - 2440587.5) * 86400 for (m,) in db.execute("select mtime from event where type='ci' order by mtime")]
    db.close()
    assert mtimes[1:] == pytest.approx(saved, abs=0.01)
    # The project directory itself never becomes a check-out.
    assert not (ws.project.root / ".fslckout").exists()


def test_rebuilds_the_same_history_from_the_records(tmp_path):
    ws = Workspace(tmp_path / "proj")
    write(ws, "nums", NODE, "agent")
    write(ws, "other", NODE.replace("nums", "other"), "api")
    ws.fossil.sync()
    before = tip(ws.fossil.repo)
    shutil.rmtree(ws.fossil.dir)
    assert ws.fossil.sync() == 2, ws.fossil.last_error
    assert tip(ws.fossil.repo) == before  # same users, times and files -> same hashes


def test_background_thread_follows_saves(tmp_path):
    async def go():
        ws = Workspace(tmp_path / "proj")
        await ws.start()
        try:
            write(ws, "nums", NODE, "agent")
            for _ in range(200):
                if ws.fossil.repo.is_file() and len(checkins(ws.fossil.repo)) == 2:
                    break
                await asyncio.sleep(0.05)
            assert checkins(ws.fossil.repo)[-1] == ("agent", "agent: nodes/nums.py"), ws.fossil.last_error
        finally:
            await ws.stop()

    asyncio.run(go())


def test_off_means_no_mirror(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_FOSSIL", "off")
    with TestClient(create_app(tmp_path / "proj")) as c:
        assert c.get("/api/config").json()["fossil"] is False
        assert c.get("/api/fossil/timeline").status_code == 404
    assert not (tmp_path / "proj" / ".exuvia" / "fossil").exists()


def test_web_ui_behind_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_API_TOKEN", TOKEN)
    app = create_app(tmp_path / "proj")
    write(app.state.ws, "nums", NODE, "agent")
    basic = {"Authorization": "Basic " + base64.b64encode(f"exuvia:{TOKEN}".encode()).decode()}
    with TestClient(app) as c:
        r = c.get("/api/fossil/timeline")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Basic")
        assert c.get("/api/config").headers.get("www-authenticate") is None  # only the Fossil UI asks
        bad = {"Authorization": "Basic " + base64.b64encode(b"exuvia:wrong").decode()}
        assert c.get("/api/fossil/timeline", headers=bad).status_code == 401

        assert c.get("/api/config", headers=basic).json()["fossil"] is True
        r = c.get("/api/fossil/timeline", headers=basic)
        assert r.status_code == 200, r.text
        assert "agent: nodes/nums.py" in r.text
        assert 'href="/api/fossil/' in r.text  # links stay under the route
        r = c.get("/api/fossil", headers={"Authorization": f"Bearer {TOKEN}"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "http://testserver/api/fossil/index"


def _serve(app):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server, thread, port


def test_clone_and_push_through_a_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_API_TOKEN", TOKEN)
    monkeypatch.setenv("EXUVIA_SCHEDULER", "external")
    app = create_hub_app(tmp_path / "hub")
    server, thread, port = _serve(app)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {TOKEN}"}) as c:
            assert c.post("/api/projects", json={"id": "demo"}).status_code == 200
            r = c.put("/p/demo/api/nodes/nums", json={"source": NODE}, headers={"X-Exuvia-Origin": "agent"})
            assert r.status_code == 200, r.text

        url = f"http://127.0.0.1:{port}/p/demo/api/fossil"
        out = tmp_path / "clone.fossil"
        env_home = tmp_path / "home"
        env_home.mkdir()
        env = {**os.environ, "FOSSIL_HOME": str(env_home), "FOSSIL_USER": "me"}
        def run(*a, cwd=None, check=True):
            # No stdin: fossil asks for credentials when it gets a 401.
            return subprocess.run([FOSSIL, *a], cwd=cwd, env=env, capture_output=True, text=True,
                                  check=check, stdin=subprocess.DEVNULL, timeout=60)
        # Without the token the hub turns fossil away.
        assert run("clone", url, str(tmp_path / "no.fossil"), check=False).returncode != 0
        run("clone", "-B", f"exuvia:{TOKEN}", url, str(out))
        assert ("agent", "agent: nodes/nums.py") in checkins(out)

        # The mirror is read-only: a check-in pushed back is refused.
        co = tmp_path / "co"
        co.mkdir()
        run("open", str(out), "--workdir", str(co), cwd=co)
        (co / "nodes" / "nums.py").write_text(NODE + "\n# pushed\n", encoding="utf-8")
        run("commit", "-m", "from outside", "--no-warnings", "--nosync", cwd=co)
        push = run("push", "-B", f"exuvia:{TOKEN}", url, cwd=co, check=False)
        assert "not authorized to write" in push.stdout + push.stderr
        hub_repo = tmp_path / "hub" / "demo" / ".exuvia" / "fossil" / "project.fossil"
        assert "from outside" not in [c for _, c in checkins(hub_repo)]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
