"""EXUVIA_AUTH=headers: a sign-in proxy says who each request is from, and
runs and code revisions record that person."""

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from exuvia import auth, server
from exuvia.server import create_app, create_hub_app
from exuvia.workspace import Workspace

NODE = '''import exuvia


@exuvia.node()
def nums():
    return {"n": 1}
'''
ALICE = {"X-Forwarded-User": "alice", "X-Forwarded-Email": "alice@example.com"}


@pytest.fixture
def headers_mode(monkeypatch):
    for name in ("EXUVIA_API_TOKEN", "EXUVIA_BROWSER_AUTH", "EXUVIA_AUTH_USER_HEADER",
                 "EXUVIA_AUTH_EMAIL_HEADER", "EXUVIA_AUTH_TRUSTED_PROXIES", "EXUVIA_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXUVIA_AUTH", "headers")


def fake_ui(tmp_path, monkeypatch):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>workbench</title>", encoding="utf-8")
    monkeypatch.setattr(server, "STATIC", static)


def test_nobody_gets_in_without_the_headers(headers_mode, tmp_path, monkeypatch):
    fake_ui(tmp_path, monkeypatch)
    with TestClient(create_app(tmp_path / "p")) as client:
        r = client.get("/api/project")
        assert r.status_code == 401 and "X-Forwarded-User" in r.json()["detail"]
        assert client.get("/").status_code == 401
        assert client.get("/api/me").status_code == 401
        assert client.get("/healthz").status_code == 200  # liveness stays open

        assert client.get("/api/project", headers=ALICE).status_code == 200
        assert client.get("/", headers=ALICE).text.endswith("<title>workbench</title>")
        me = client.get("/api/me", headers=ALICE).json()
        assert me["auth"] == "headers"
        assert {k: me["user"][k] for k in ("user", "email", "method", "id", "role")} == {
            "user": "alice", "email": "alice@example.com", "method": "headers", "id": "alice", "role": "member",
        }
        assert len(me["user"]["uid"]) == 12  # recorded in the user list (test_users.py)
        # Either header is enough; the email names the person when there is no user name.
        me = client.get("/api/me", headers={"X-Forwarded-Email": "bob@example.com"}).json()["user"]
        assert me["id"] == "bob@example.com" and me["user"] is None
        # Blank or malformed values are not an identity.
        assert client.get("/api/project", headers={"X-Forwarded-User": "  "}).status_code == 401
        assert client.get("/api/project", headers={"X-Forwarded-User": "a" * 300}).status_code == 401


def test_runs_and_revisions_record_who(headers_mode, tmp_path):
    with TestClient(create_app(tmp_path / "p")) as client:
        assert client.put("/api/nodes/nums", json={"source": NODE, "kind": "python"}, headers=ALICE).status_code == 200
        bob = {"X-Forwarded-Email": "bob@example.com"}
        r = client.post("/api/nodes/nums/run", json={"wait": 60}, headers=bob)
        assert r.status_code == 200

        revs = client.get("/api/code/revisions", params={"node": "nums"}, headers=ALICE).json()
        assert [(x["origin"], x["author"]) for x in revs] == [("api", "alice")]
        runs = client.get("/api/nodes/nums/runs", headers=ALICE).json()
        assert [(x["status"], x["started_by"]) for x in runs] == [("success", "bob@example.com")]
        recent = client.get("/api/runs", headers=ALICE).json()
        assert recent[0]["started_by"] == "bob@example.com"


def test_a_hand_edit_names_nobody(headers_mode, tmp_path):
    root = tmp_path / "p"
    (root / "nodes").mkdir(parents=True)
    ws = Workspace(root)
    (root / "nodes" / "nums.py").write_text(NODE, encoding="utf-8")
    with auth.signed_in(auth.Identity("alice", None, "headers")):
        ws.reload()  # no origin claimed: an edit made to the file, not by alice
    assert [(r["origin"], r["author"]) for r in ws.code_revisions("nums")] == [("file", None)]

    ws.store.close()


def test_a_scheduled_run_names_nobody_whoever_ticks(headers_mode, tmp_path):
    root = tmp_path / "p"
    (root / "nodes").mkdir(parents=True)
    (root / "nodes" / "nums.py").write_text(NODE.replace("node()", 'node(schedule="* * * * *")'), encoding="utf-8")
    ws = Workspace(root)
    started: list[str | None] = []

    async def fake_run(targets, **kw):
        started.append(auth.current_id())
        return {}

    ws.run = fake_run

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        with auth.signed_in(auth.Identity("alice", None, "headers")):  # alice calls /api/schedule/tick
            assert await ws.tick(0) == []  # first sight: starts the clock
            assert await ws.tick(120) == ["nums"]
        await asyncio.sleep(0)  # let the run task start

    asyncio.run(go())
    ws.store.close()
    assert started == [None]


def test_the_api_token_still_lets_agents_in(headers_mode, tmp_path, monkeypatch):
    fake_ui(tmp_path, monkeypatch)
    monkeypatch.setenv("EXUVIA_API_TOKEN", "s3cret")
    with TestClient(create_app(tmp_path / "p")) as client:
        assert client.get("/api/project").status_code == 401
        bearer = {"Authorization": "Bearer s3cret"}
        assert client.get("/api/project", headers=bearer).status_code == 200
        assert client.get("/api/me", headers=bearer).json() == {"auth": "headers", "user": None}
        assert client.get("/api/project", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/project", headers=ALICE).status_code == 200
        # The token's browser sign-in still works, for someone outside the proxy.
        assert client.get("/?token=s3cret", follow_redirects=False).status_code in (302, 307)
        assert client.get("/api/project").status_code == 200


def test_custom_header_names(headers_mode, tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH_USER_HEADER", "Remote-User")
    monkeypatch.setenv("EXUVIA_AUTH_EMAIL_HEADER", "Remote-Email")
    with TestClient(create_app(tmp_path / "p")) as client:
        assert client.get("/api/project", headers=ALICE).status_code == 401
        me = client.get("/api/me", headers={"remote-user": "carol", "remote-email": "c@example.com"}).json()
        assert me["user"]["id"] == "carol" and me["user"]["email"] == "c@example.com"


def test_headers_from_an_untrusted_peer_are_ignored(headers_mode, tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH_TRUSTED_PROXIES", "10.0.0.0/8, 192.168.1.5")
    with TestClient(create_app(tmp_path / "p")) as client:  # its peer is "testclient"
        assert client.get("/api/project", headers=ALICE).status_code == 401
    h = auth.header_auth()
    assert h is not None
    assert h.identify(ALICE, "10.1.2.3") is not None
    assert h.identify(ALICE, "192.168.1.5") is not None
    assert h.identify(ALICE, "192.168.1.6") is None
    assert h.identify(ALICE, None) is None


def test_bad_settings_stop_the_server(headers_mode, tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH_TRUSTED_PROXIES", "10.0.0.0/33")
    with pytest.raises(SystemExit, match="EXUVIA_AUTH_TRUSTED_PROXIES"):
        create_app(tmp_path / "p")
    monkeypatch.delenv("EXUVIA_AUTH_TRUSTED_PROXIES")
    monkeypatch.setenv("EXUVIA_AUTH", "saml")
    with pytest.raises(SystemExit, match="EXUVIA_AUTH must be one of token, headers"):
        create_app(tmp_path / "p")


def test_me_without_auth(tmp_path, monkeypatch):
    for name in ("EXUVIA_AUTH", "EXUVIA_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(tmp_path / "p")) as client:
        assert client.get("/api/me", headers=ALICE).json() == {"auth": "none", "user": None}  # headers not trusted


def test_hub_with_header_auth(headers_mode, tmp_path, monkeypatch):
    fake_ui(tmp_path, monkeypatch)
    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        assert hub.get("/api/projects").status_code == 401
        assert hub.get("/").status_code == 401
        assert hub.post("/api/projects", json={"id": "wf"}, headers=ALICE).status_code == 200
        assert hub.get("/p/wf/api/project", headers=ALICE).status_code == 200
        assert hub.get("/p/wf/api/project").status_code == 401
        assert hub.get("/p/nope/", headers=ALICE).status_code == 404  # signed in: a missing project is reported
        assert hub.get("/api/me", headers=ALICE).json()["user"]["id"] == "alice"
        hub.put("/p/wf/api/nodes/nums", json={"source": NODE, "kind": "python"}, headers=ALICE)
        revs = hub.get("/p/wf/api/code/revisions", params={"node": "nums"}, headers=ALICE).json()
        assert revs[0]["author"] == "alice"


def test_a_database_from_before_users_gains_the_columns(tmp_path, monkeypatch):
    monkeypatch.delenv("EXUVIA_DB", raising=False)
    root = tmp_path / "p"
    (root / "nodes").mkdir(parents=True)
    (root / "nodes" / "nums.py").write_text(NODE, encoding="utf-8")
    Workspace(root).store.close()
    con = sqlite3.connect(root / ".exuvia" / "exuvia.db")
    con.execute("alter table code_revisions drop column author")
    con.execute("alter table runs drop column started_by")
    con.commit()
    con.close()

    ws = Workspace(root)
    assert [r["author"] for r in ws.code_revisions("nums")] == [None]
    run_id = ws.store.create_run("nums", "b", "manual", started_by="dave")
    assert ws.store.run(run_id)["started_by"] == "dave"
    ws.store.close()
