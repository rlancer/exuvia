"""The user list: everyone who signs in is recorded once, whatever method
they sign in with, and admins manage roles and disabled accounts.

The store tests run on each backend (as in test_revisions.py): a SQLite file,
a shared SQLite file and, with ``EXUVIA_TEST_POSTGRES_URL``, Postgres.
"""

import io
import os
import sqlite3
import tarfile
import uuid

import pytest
from fastapi.testclient import TestClient

from exuvia.auth import Identity
from exuvia.db import SQLiteDatabase, close_shared
from exuvia.server import create_app, create_hub_app
from exuvia.users import SEEN_EVERY, UserStore, open_users

NODE = '''import exuvia


@exuvia.node()
def nums():
    return {"n": 1}
'''
PG_URL = os.environ.get("EXUVIA_TEST_POSTGRES_URL", "")
BACKENDS = ["sqlite-local", "sqlite-shared"] + (["postgres"] if PG_URL else [])


def headers(user=None, email=None):
    h = {}
    if user:
        h["X-Forwarded-User"] = user
    if email:
        h["X-Forwarded-Email"] = email
    return h


ALICE = headers("alice", "alice@example.com")
BOB = headers("bob", "Bob@Example.com")


@pytest.fixture(autouse=True)
def headers_mode(monkeypatch):
    for name in ("EXUVIA_API_TOKEN", "EXUVIA_BROWSER_AUTH", "EXUVIA_AUTH_USER_HEADER", "EXUVIA_ADMINS",
                 "EXUVIA_AUTH_EMAIL_HEADER", "EXUVIA_AUTH_TRUSTED_PROXIES", "EXUVIA_DB", "EXUVIA_DB_SCOPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXUVIA_AUTH", "headers")


@pytest.fixture(params=BACKENDS)
def users(request, tmp_path, monkeypatch):
    close_shared()
    if request.param == "sqlite-shared":
        monkeypatch.setenv("EXUVIA_DB", f"sqlite:///{(tmp_path / 'shared.db').as_posix()}")
    elif request.param == "postgres":
        monkeypatch.setenv("EXUVIA_DB", PG_URL)
    monkeypatch.setenv("EXUVIA_DB_SCOPE", f"t{uuid.uuid4().hex[:8]}/")
    store = open_users(tmp_path / "hub.db")
    yield store
    store.close()
    close_shared()


def ident(user=None, email=None, method="headers"):
    return Identity(user=user, email=email, method=method)


def test_a_sign_in_is_recorded_once_and_kept_up_to_date(users):
    a = users.sign_in(ident("alice", "Alice@Example.com"), now=100)
    assert (a.name, a.email, a.role, a.disabled) == ("alice", "alice@example.com", "member", False)
    assert users.sign_in(ident("alice", "alice@example.com"), now=100 + SEEN_EVERY + 1).id == a.id
    [row] = users.list()
    assert row["id"] == a.id and row["created_at"] == 100 and row["last_seen"] == 100 + SEEN_EVERY + 1
    assert [(i["method"], i["subject"]) for i in row["identities"]] == [("headers", "alice")]


def test_last_seen_is_written_at_most_once_a_minute(users):
    a = users.sign_in(ident("alice"), now=100)
    assert users.recent(ident("alice"), now=130) == a  # from memory: no write
    users.sign_in(ident("alice"), now=130)
    assert users.list()[0]["last_seen"] == 100
    assert users.recent(ident("alice"), now=100 + SEEN_EVERY) is None
    # A new name or email from the proxy is not answered from memory.
    assert users.recent(ident("alice", "new@example.com"), now=110) is None


def test_another_sign_in_method_with_the_same_email_is_the_same_person(users):
    a = users.sign_in(ident("alice", "alice@example.com"))
    # A later method (say OIDC) naming her by another subject joins her user.
    b = users.sign_in(ident("00u1abc", "ALICE@example.com", method="oidc"))
    assert b.id == a.id
    [row] = users.list()
    assert sorted((i["method"], i["subject"]) for i in row["identities"]) == [("headers", "alice"), ("oidc", "00u1abc")]
    # Without a shared email, a different person.
    assert users.sign_in(ident("carol")).id != a.id
    assert len(users.list()) == 2


def test_exuvia_admins_are_admins_and_cannot_be_demoted_or_disabled(users, monkeypatch):
    monkeypatch.setenv("EXUVIA_ADMINS", "root@example.com, Alice")
    a = users.sign_in(ident("alice"))
    r = users.sign_in(ident(email="root@example.com"))
    m = users.sign_in(ident("mallory"))
    assert (a.role, r.role, m.role) == ("admin", "admin", "member")
    with pytest.raises(ValueError, match="EXUVIA_ADMINS"):
        users.update(a.id, role="member")
    with pytest.raises(ValueError, match="EXUVIA_ADMINS"):
        users.update(r.id, disabled=True)
    with pytest.raises(ValueError, match="role must be"):
        users.update(m.id, role="owner")
    with pytest.raises(KeyError):
        users.update("nope", disabled=True)
    assert users.update(m.id, role="admin").role == "admin"
    assert users.update(m.id, disabled=True).disabled is True
    # The change is read on the next sign-in, not served from memory.
    assert users.sign_in(ident("mallory")).disabled is True


def test_the_user_list_is_scoped_per_hub_in_a_shared_database(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_DB", f"sqlite:///{(tmp_path / 'shared.db').as_posix()}")
    try:
        monkeypatch.setenv("EXUVIA_DB_SCOPE", "hub-a/")
        a = open_users(tmp_path / "unused.db")
        monkeypatch.setenv("EXUVIA_DB_SCOPE", "hub-b/")
        b = open_users(tmp_path / "unused.db")
        a.sign_in(ident("alice"))
        assert [u["name"] for u in a.list()] == ["alice"] and b.list() == []
        assert not (tmp_path / "unused.db").exists()
    finally:
        close_shared()


def test_runs_and_revisions_link_to_the_user(tmp_path):
    with TestClient(create_app(tmp_path / "p")) as client:
        uid = client.get("/api/me", headers=ALICE).json()["user"]["uid"]
        client.put("/api/nodes/nums", json={"source": NODE, "kind": "python"}, headers=ALICE)
        assert client.post("/api/nodes/nums/run", json={"wait": 60}, headers=ALICE).status_code == 200
        [rev] = client.get("/api/code/revisions", params={"node": "nums"}, headers=ALICE).json()
        assert (rev["author"], rev["author_user"]) == ("alice", uid)
        [run] = client.get("/api/nodes/nums/runs", headers=ALICE).json()
        assert (run["started_by"], run["started_by_user"]) == ("alice", uid)
    # `exuvia serve` keeps the user list in the project's own database.
    con = sqlite3.connect(tmp_path / "p" / ".exuvia" / "exuvia.db")
    assert [r[0] for r in con.execute("select name from users")] == ["alice"]
    con.close()


def test_admin_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_ADMINS", "alice")
    with TestClient(create_app(tmp_path / "p")) as client:
        assert client.get("/api/me", headers=ALICE).json()["user"]["role"] == "admin"
        bob = client.get("/api/me", headers=BOB).json()["user"]
        assert bob["role"] == "member"
        assert client.get("/api/users", headers=BOB).status_code == 403
        assert client.patch(f"/api/users/{bob['uid']}", json={"role": "admin"}, headers=BOB).status_code == 403

        listed = client.get("/api/users", headers=ALICE).json()["users"]
        assert {(u["name"], u["email"], u["role"], u["pinned_admin"]) for u in listed} == {
            ("alice", "alice@example.com", "admin", True),
            ("bob", "bob@example.com", "member", False),
        }
        me = client.get("/api/me", headers=ALICE).json()["user"]["uid"]
        assert client.patch(f"/api/users/{me}", json={"disabled": True}, headers=ALICE).status_code == 400
        assert client.patch("/api/users/nope", json={"disabled": True}, headers=ALICE).status_code == 404
        r = client.patch(f"/api/users/{bob['uid']}", json={"role": "boss"}, headers=ALICE)
        assert r.status_code == 400 and "role must be" in r.json()["detail"]

        # Disabled: turned away even though the proxy lets bob through.
        assert client.patch(f"/api/users/{bob['uid']}", json={"disabled": True}, headers=ALICE).json()["disabled"]
        r = client.get("/api/project", headers=BOB)
        assert r.status_code == 403 and "disabled" in r.json()["detail"]
        assert client.get("/", headers=BOB).status_code in (403, 404)  # no UI built in tests: 404 once let in
        assert client.patch(f"/api/users/{bob['uid']}", json={"disabled": False}, headers=ALICE).status_code == 200
        assert client.get("/api/project", headers=BOB).status_code == 200

        # A promoted admin can manage users too.
        client.patch(f"/api/users/{bob['uid']}", json={"role": "admin"}, headers=ALICE)
        assert client.get("/api/users", headers=BOB).status_code == 200


def test_the_api_token_is_the_operator(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_API_TOKEN", "s3cret")
    with TestClient(create_app(tmp_path / "p")) as client:
        bob = client.get("/api/me", headers=BOB).json()["user"]["uid"]
        token = {"Authorization": "Bearer s3cret"}
        assert [u["name"] for u in client.get("/api/users", headers=token).json()["users"]] == ["bob"]
        assert client.patch(f"/api/users/{bob}", json={"role": "admin"}, headers=token).json()["role"] == "admin"


def test_no_user_list_without_sign_in(tmp_path, monkeypatch):
    monkeypatch.delenv("EXUVIA_AUTH")
    with TestClient(create_app(tmp_path / "p")) as client:
        r = client.get("/api/users")
        assert r.status_code == 404 and "EXUVIA_AUTH" in r.json()["detail"]
    con = sqlite3.connect(tmp_path / "p" / ".exuvia" / "exuvia.db")
    assert not list(con.execute("select name from sqlite_master where name='users'"))
    con.close()


def test_hub_keeps_one_user_list_and_carries_it_in_exports(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_ADMINS", "alice")
    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        assert hub.post("/api/projects", json={"id": "a"}, headers=ALICE).status_code == 200
        assert hub.post("/api/projects", json={"id": "b"}, headers=ALICE).status_code == 200
        hub.put("/p/a/api/nodes/nums", json={"source": NODE, "kind": "python"}, headers=ALICE)
        hub.get("/p/a/api/project", headers=BOB)
        hub.get("/p/b/api/project", headers=BOB)  # the same bob in every project
        listed = hub.get("/api/users", headers=ALICE).json()["users"]
        assert sorted(u["name"] for u in listed) == ["alice", "bob"]
        bob = next(u["id"] for u in listed if u["name"] == "bob")
        hub.patch(f"/api/users/{bob}", json={"disabled": True}, headers=ALICE)
        assert hub.get("/api/projects", headers=BOB).status_code == 403
        assert (tmp_path / "hub" / ".exuvia" / "hub.db").is_file()
        assert [p["id"] for p in hub.get("/api/projects", headers=ALICE).json()["projects"]] == ["a", "b"]

        export = hub.get("/api/hub/export", headers=ALICE).content
        assert ".exuvia/hub.db" in tarfile.open(fileobj=io.BytesIO(export), mode="r:gz").getnames()

    # A fresh host (no disk kept) restores the archive: bob is still disabled.
    with TestClient(create_hub_app(tmp_path / "fresh")) as hub:
        assert hub.post("/api/hub/import", content=export, headers=ALICE).status_code == 200
        assert hub.get("/api/projects", headers=BOB).status_code == 403
        assert sorted(u["name"] for u in hub.get("/api/users", headers=ALICE).json()["users"]) == ["alice", "bob"]


def test_a_vanished_user_starts_over(tmp_path):
    store = UserStore(SQLiteDatabase(tmp_path / "u.db"), owns_db=True)
    a = store.sign_in(ident("alice"), now=1)
    store.db.query("delete from users where id=?", (a.id,))
    b = store.sign_in(ident("alice"), now=1 + SEEN_EVERY + 1)
    assert b.id != a.id and [u["id"] for u in store.list()] == [b.id]
    store.close()


def test_a_server_starting_on_an_existing_list_takes_no_table_locks(users):
    """`create ... if not exists` still locks the table in Postgres, and that
    deadlocked against a sign-in on another replica. Once the tables exist, a
    server starting up must run no DDL at all."""
    from exuvia import users as users_mod
    from exuvia.db import open_database

    users.sign_in(ident("alice"))
    url = os.environ.get("EXUVIA_DB")
    db = open_database(url) if url else SQLiteDatabase(users.db.file)
    seen: list[str] = []
    query = db.query
    db.query = lambda sql, args=(): (seen.append(sql), query(sql, args))[1]
    try:
        users_mod._prepare(db)
    finally:
        db.query = query
        db.close()
    assert not [s for s in seen if s.lstrip().lower().startswith("create")]


# -- agent tokens -----------------------------------------------------------------


def test_an_agent_token_signs_in_as_its_user_until_revoked(users):
    a = users.sign_in(ident("alice", "alice@example.com"), now=1)
    row, token = users.create_token(a.id, "  my   laptop ", now=2)
    assert token.startswith("exv_") and len(token) > 40
    assert (row["name"], row["hint"], row["last_used"]) == ("my laptop", token[-4:], None)
    assert users.by_token(token, now=3).id == a.id
    assert users.by_token(token + "x", now=3) is None
    assert users.by_token("s3cret", now=3) is None  # not an agent token: no lookup at all
    [listed] = users.tokens(a.id)
    assert listed == {**row, "last_used": 3}
    assert token not in str(users.db.query("select * from user_tokens"))  # only its hash is kept
    assert users.create_token(a.id, "", now=4)[0]["name"] == "agent"
    with pytest.raises(KeyError):
        users.create_token("nobody", "x")

    b = users.sign_in(ident("bob"), now=5)
    assert not users.revoke_token(b.id, row["id"])  # not bob's to revoke
    assert users.by_token(token, now=6) is not None
    assert users.revoke_token(a.id, row["id"])
    assert users.by_token(token, now=6) is None  # at once, on this replica
    assert [t["name"] for t in users.tokens(a.id)] == ["agent"]


def test_agent_token_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_ADMINS", "alice")
    with TestClient(create_app(tmp_path / "p")) as client:
        assert client.get("/api/me", headers=BOB).json()["tokens"] is True
        assert client.get("/api/me/tokens").status_code == 401
        r = client.post("/api/me/tokens", json={"name": "Claude Code"}, headers=BOB)
        assert r.status_code == 201
        made = r.json()
        token = {"Authorization": f"Bearer {made['token']}"}
        assert [t["name"] for t in client.get("/api/me/tokens", headers=BOB).json()["tokens"]] == ["Claude Code"]
        assert "token" not in client.get("/api/me/tokens", headers=BOB).json()["tokens"][0]
        assert client.get("/api/me/tokens", headers=ALICE).json()["tokens"] == []  # each person sees their own

        # No proxy headers: the token alone signs the caller in as bob.
        me = client.get("/api/me", headers=token).json()["user"]
        assert (me["id"], me["method"], me["role"]) == ("bob", "token", "member")
        client.put("/api/nodes/nums", json={"source": NODE, "kind": "python"}, headers=token)
        assert client.post("/api/nodes/nums/run", json={"wait": 60}, headers=token).status_code == 200
        [run] = client.get("/api/nodes/nums/runs", headers=token).json()
        assert (run["started_by"], run["started_by_user"]) == ("bob", me["uid"])
        assert client.get("/api/users", headers=token).status_code == 403  # a member's token is a member

        # A token can't mint or revoke tokens: a leaked one stays one.
        assert client.post("/api/me/tokens", json={}, headers=token).status_code == 403
        assert client.delete(f"/api/me/tokens/{made['id']}", headers=token).status_code == 403

        # A disabled user's tokens stop working with them.
        alice_token = client.post("/api/me/tokens", json={}, headers=ALICE).json()["token"]
        client.patch(f"/api/users/{me['uid']}", json={"disabled": True}, headers=ALICE)
        assert client.get("/api/project", headers=token).status_code == 403
        client.patch(f"/api/users/{me['uid']}", json={"disabled": False}, headers=ALICE)
        assert client.get("/api/project", headers=token).status_code == 200
        # An admin's token is an admin.
        assert client.get("/api/users", headers={"Authorization": f"Bearer {alice_token}"}).status_code == 200

        assert client.delete("/api/me/tokens/nope", headers=BOB).status_code == 404
        assert client.delete(f"/api/me/tokens/{made['id']}", headers=ALICE).status_code == 404  # not alice's
        assert client.delete(f"/api/me/tokens/{made['id']}", headers=BOB).json() == {"ok": True}
        r = client.get("/api/project", headers=token)
        assert r.status_code == 401 and "agent token" in r.json()["detail"]


def test_agent_tokens_need_a_user_list(tmp_path, monkeypatch):
    monkeypatch.delenv("EXUVIA_AUTH")
    with TestClient(create_app(tmp_path / "p")) as client:
        assert client.get("/api/me").json()["tokens"] is False
        assert client.post("/api/me/tokens", json={}).status_code == 404


def test_a_hub_takes_agent_tokens_everywhere_and_exports_them(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_ADMINS", "alice")  # export and import cover every project: admins only
    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        token = {"Authorization": f"Bearer {hub.post('/api/me/tokens', json={}, headers=ALICE).json()['token']}"}
        assert hub.post("/api/projects", json={"id": "a"}, headers=token).status_code == 200
        assert hub.get("/p/a/api/project", headers=token).status_code == 200
        assert hub.get("/api/me", headers=token).json()["user"]["id"] == "alice"
        export = hub.get("/api/hub/export", headers=token).content

    with TestClient(create_hub_app(tmp_path / "fresh")) as hub:
        assert hub.post("/api/hub/import", content=export, headers=ALICE).status_code == 200
        assert hub.get("/api/projects", headers=token).status_code == 200


def test_an_archive_from_before_agent_tokens_imports(tmp_path):
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    from exuvia.users import TABLES

    for name in ("users", "user_identities"):
        con.execute(TABLES[name])
    con.execute("insert into users values ('', 'u1', 'alice', null, 'admin', 0, 1, 1)")
    con.commit()
    con.close()
    store = UserStore(SQLiteDatabase(tmp_path / "u.db"), owns_db=True)
    store.create_token(store.sign_in(ident("bob")).id, "x")
    assert store.replace_from(old) == 1
    assert store.db.query("select count(*) as n from user_tokens")[0]["n"] == 0
    store.close()


def test_a_user_list_from_before_agent_tokens_gains_the_table(tmp_path):
    db = tmp_path / "u.db"
    con = sqlite3.connect(db)
    from exuvia.users import TABLES

    for name in ("users", "user_identities"):
        con.execute(TABLES[name])
    con.commit()
    con.close()
    store = UserStore(SQLiteDatabase(db), owns_db=True)
    a = store.sign_in(ident("alice"))
    _, token = store.create_token(a.id)
    assert store.by_token(token).id == a.id
    store.close()
