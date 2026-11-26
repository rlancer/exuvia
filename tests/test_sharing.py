"""Sharing hub projects: owners, people with roles, general access, share
links and access requests, enforced on the API, on MCP and on the project
list (exuvia/sharing.py).
"""

import asyncio
import io
import json
import os
import tarfile
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from exuvia.auth import Identity
from exuvia.db import close_shared
from exuvia.server import create_hub_app
from exuvia.sharing import SEEN_EVERY, open_sharing
from exuvia.users import open_users

NODE = '''import exuvia


@exuvia.node()
def nums():
    return {"n": 1}
'''


def headers(user, email=None):
    h = {"X-Forwarded-User": user}
    if email:
        h["X-Forwarded-Email"] = email
    return h


ALICE = headers("alice", "alice@example.com")
BOB = headers("bob", "bob@example.com")
CAROL = headers("carol", "carol@example.com")
ROOT = headers("root", "root@example.com")


@pytest.fixture(autouse=True)
def headers_mode(monkeypatch):
    for name in ("EXUVIA_API_TOKEN", "EXUVIA_BROWSER_AUTH", "EXUVIA_AUTH_USER_HEADER", "EXUVIA_AUTH_EMAIL_HEADER",
                 "EXUVIA_AUTH_TRUSTED_PROXIES", "EXUVIA_DB", "EXUVIA_DB_SCOPE", "EXUVIA_DEFAULT_ACCESS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXUVIA_AUTH", "headers")
    monkeypatch.setenv("EXUVIA_ADMINS", "root")
    close_shared()
    yield
    close_shared()


@pytest.fixture
def hub(tmp_path):
    with TestClient(create_hub_app(tmp_path / "hub")) as client:
        for who in (ALICE, BOB, ROOT):  # everyone in the user list
            client.get("/api/me", headers=who)
        yield client


def uid(client, who):
    return client.get("/api/me", headers=who).json()["user"]["uid"]


def ids(client, who):
    return [p["id"] for p in client.get("/api/projects", headers=who).json()["projects"]]


# -- the store ------------------------------------------------------------------


PG_URL = os.environ.get("EXUVIA_TEST_POSTGRES_URL", "")
BACKENDS = ["sqlite-local", "sqlite-shared"] + (["postgres"] if PG_URL else [])


@pytest.fixture(params=BACKENDS)
def stores(request, tmp_path, monkeypatch):
    """A user list and sharing rules on each backend, as test_users.py does."""
    close_shared()
    if request.param == "sqlite-shared":
        monkeypatch.setenv("EXUVIA_DB", f"sqlite:///{(tmp_path / 'shared.db').as_posix()}")
    elif request.param == "postgres":
        monkeypatch.setenv("EXUVIA_DB", PG_URL)
    monkeypatch.setenv("EXUVIA_DB_SCOPE", f"t{uuid.uuid4().hex[:8]}/")
    users = open_users(tmp_path / "hub.db")
    yield users, open_sharing(tmp_path / "hub.db", users)
    users.close()
    close_shared()


def person(users, name, role=None):
    u = users.sign_in(Identity(user=name, email=f"{name}@example.com", method="headers"))
    return Identity(user=name, email=f"{name}@example.com", method="headers", uid=u.id, role=role or u.role)


def test_roles_come_from_the_owner_people_general_access_and_links(stores):
    users, sh = stores
    alice, bob, carol = person(users, "alice"), person(users, "bob"), person(users, "carol")
    sh.init_project("p", alice.uid, "private")
    assert (sh.role("p", alice), sh.role("p", bob)) == ("owner", None)
    sh.grant("p", "viewer", user_id=bob.uid)
    assert sh.role("p", bob) == "viewer"
    sh.set_general("p", "edit")
    assert (sh.role("p", bob), sh.role("p", carol)) == ("editor", "editor")  # the highest rule wins
    sh.set_general("p", "view")
    assert sh.role("p", carol) == "viewer"
    # Admins and the operator (nobody signed in) can do anything; a link only reads its own project.
    assert sh.role("p", person(users, "dan", role="admin")) == "owner" and sh.role("p", None) == "owner"
    link = Identity(user=None, email=None, method="link", link="p")
    assert (sh.role("p", link), sh.role("q", link)) == ("viewer", None)


def test_an_email_grant_waits_for_its_person_to_sign_in(stores):
    users, sh = stores
    alice = person(users, "alice")
    sh.init_project("p", alice.uid, "private")
    assert sh.grant("p", "editor", email="Erin@Example.com") == "email:erin@example.com"
    [m] = sh.members("p")
    assert m["pending"] and m["user"]["email"] == "erin@example.com"
    assert sh.role("p", person(users, "erin")) == "editor"
    # Someone already signed in is stored as their user.
    bob = person(users, "bob")
    assert sh.grant("p", "viewer", email="bob@example.com") == f"user:{bob.uid}"


def test_ownership_moves_and_the_old_owner_stays_an_editor(stores):
    users, sh = stores
    alice, bob = person(users, "alice"), person(users, "bob")
    sh.init_project("p", alice.uid, "private")
    sh.grant("p", "viewer", user_id=bob.uid)
    sh.set_owner("p", bob.uid)
    assert (sh.role("p", bob), sh.role("p", alice)) == ("owner", "editor")
    assert [m["user"]["name"] for m in sh.members("p")] == ["alice"]
    with pytest.raises(ValueError, match="owns"):
        sh.grant("p", "viewer", user_id=bob.uid)
    with pytest.raises(ValueError, match="ownership"):
        sh.grant("p", "owner", user_id=alice.uid)


def test_links_open_one_project_until_they_expire_or_are_revoked(stores):
    _, sh = stores
    sh.init_project("p", None)
    row, token = sh.create_link("p", "board", expires_at=time.time() + 100)
    assert sh.link_project(token) == "p" and sh.link_project("exs_" + "x" * 30) is None
    assert sh.link_project(token, now=time.time() + 101) is None  # expired, even from memory
    assert sh.revoke_link("p", row["id"]) and sh.link_project(token) is None
    _, forever = sh.create_link("p")
    assert sh.link_project(forever, now=time.time() + SEEN_EVERY + 10**6) == "p"
    sh.forget("p")
    assert sh.link_project(forever) is None


def test_a_project_without_settings_follows_the_hub_default(stores, monkeypatch):
    users, sh = stores
    bob = person(users, "bob")
    assert sh.role("old", bob) == "editor"  # as before sharing: everyone signed in can change it
    monkeypatch.setenv("EXUVIA_DEFAULT_ACCESS", "view")
    sh._changed()
    assert sh.role("old", bob) == "viewer"


def test_a_new_project_someone_owns_is_view_only_for_everyone_else(stores, monkeypatch):
    users, sh = stores
    alice, bob = person(users, "alice"), person(users, "bob")
    sh.init_project("mine", alice.uid)
    assert sh.describe("mine")["general"] == "view"
    assert (sh.role("mine", alice), sh.role("mine", bob)) == ("owner", "viewer")
    # Nobody owns one made with the hub token: at view, only an admin could change it.
    sh.init_project("scripted", None)
    assert sh.role("scripted", bob) == "editor"
    # EXUVIA_DEFAULT_ACCESS replaces both defaults.
    monkeypatch.setenv("EXUVIA_DEFAULT_ACCESS", "edit")
    sh.init_project("open", alice.uid)
    assert sh.role("open", bob) == "editor"
    monkeypatch.setenv("EXUVIA_DEFAULT_ACCESS", "private")
    sh.init_project("closed", None)
    assert sh.role("closed", bob) is None


# -- the API ----------------------------------------------------------------------


def test_a_private_project_is_hidden_and_closed_to_others(hub):
    assert hub.post("/api/projects", json={"id": "a", "access": "private"}, headers=ALICE).status_code == 200
    assert ids(hub, ALICE) == ["a"] and ids(hub, BOB) == [] and ids(hub, ROOT) == ["a"]
    [row] = hub.get("/api/projects", headers=ALICE).json()["projects"]
    assert (row["access"], row["owner"]["name"], row["general"], row["shared"]) == ("owner", "alice", "private", False)
    r = hub.get("/p/a/api/project", headers=BOB)
    assert r.status_code == 403 and "request access" in r.json()["detail"]
    access = hub.get("/p/a/api/access", headers=BOB).json()
    assert access["role"] is None and access["can_request"] and "owner" not in access
    assert hub.delete("/api/projects/a", headers=BOB).status_code == 403
    assert hub.put("/api/projects/a/sharing/people", json={"user": uid(hub, BOB), "role": "editor"}, headers=BOB).status_code == 403


def test_new_projects_default_to_everyone_viewing_and_the_owner_editing(hub):
    assert hub.get("/api/projects", headers=ALICE).json()["sharing"]["default_access"] == "view"
    hub.post("/api/projects", json={"id": "a"}, headers=ALICE)
    assert ids(hub, BOB) == ["a"]
    assert hub.put("/p/a/api/nodes/nums", json={"source": NODE}, headers=BOB).status_code == 403
    assert hub.put("/p/a/api/nodes/nums", json={"source": NODE}, headers=ALICE).status_code == 200
    assert hub.get("/api/projects/a/sharing", headers=BOB).json()["you"] == "viewer"
    # Its creator can still open it to everyone editing.
    hub.post("/api/projects", json={"id": "b", "access": "edit"}, headers=ALICE)
    assert hub.put("/p/b/api/nodes/nums", json={"source": NODE}, headers=BOB).status_code == 200


def test_a_viewer_reads_but_changes_nothing(hub):
    hub.post("/api/projects", json={"id": "a", "access": "private"}, headers=ALICE)
    hub.put("/p/a/api/nodes/nums", json={"source": NODE}, headers=ALICE)
    shared = hub.put("/api/projects/a/sharing/people", json={"email": "bob@example.com", "role": "viewer"}, headers=ALICE)
    assert shared.status_code == 200 and [m["user"]["name"] for m in shared.json()["people"]] == ["bob"]
    assert ids(hub, BOB) == ["a"]
    assert hub.get("/p/a/api/project", headers=BOB).status_code == 200
    assert hub.get("/p/a/api/nodes/nums", headers=BOB).status_code == 200
    assert hub.post("/p/a/api/tools/read_node", json={"name": "nums"}, headers=BOB).json()["ok"]
    for method, path, body in [
        ("PUT", "/p/a/api/nodes/nums", {"source": NODE}),
        ("POST", "/p/a/api/nodes/nums/run", {}),
        ("POST", "/p/a/api/tools/write_node", {"name": "x", "kind": "python", "source": NODE}),
        ("POST", "/p/a/api/agent", {"message": "hi"}),
        ("DELETE", "/p/a/api/nodes/nums", None),
        ("PUT", "/p/a/api/env/API_KEY", {"value": "k", "secret": True}),
        ("DELETE", "/p/a/api/env/API_KEY", None),
        ("POST", "/p/a/api/tools/request_secret", {"name": "API_KEY", "reason": "x"}),
        ("DELETE", "/p/a/api/env/requests/API_KEY", None),
    ]:
        r = hub.request(method, path, json=body, headers=BOB)
        assert r.status_code == 403 and "not change it" in r.json()["detail"], (method, path)
    view = hub.get("/api/projects/a/sharing", headers=BOB).json()
    assert view["you"] == "viewer" and not view["can_manage"] and "links" not in view


def test_an_editor_changes_code_but_not_sharing(hub):
    hub.post("/api/projects", json={"id": "a", "access": "private"}, headers=ALICE)
    hub.put("/api/projects/a/sharing/people", json={"user": uid(hub, BOB), "role": "editor"}, headers=ALICE)
    assert hub.put("/p/a/api/nodes/nums", json={"source": NODE}, headers=BOB).status_code == 200
    assert hub.patch("/api/projects/a/sharing", json={"general": "edit"}, headers=BOB).status_code == 403
    assert hub.delete("/api/projects/a", headers=BOB).status_code == 403
    assert hub.post("/api/projects/a/sharing/links", json={}, headers=BOB).status_code == 403


def test_general_access_ownership_and_leaving(hub):
    hub.post("/api/projects", json={"id": "a", "access": "private"}, headers=ALICE)
    assert hub.patch("/api/projects/a/sharing", json={"general": "view"}, headers=ALICE).json()["general"] == "view"
    assert hub.get("/p/a/api/access", headers=BOB).json()["role"] == "viewer"
    assert hub.patch("/api/projects/a/sharing", json={"general": "public"}, headers=ALICE).status_code == 400
    # Hand it to bob: alice stays on as an editor and can take herself off.
    moved = hub.patch("/api/projects/a/sharing", json={"owner": uid(hub, BOB)}, headers=ALICE).json()
    assert moved["owner"]["name"] == "bob" and moved["you"] == "editor"
    assert hub.delete("/api/projects/a", headers=ALICE).status_code == 403
    left = hub.delete(f"/api/projects/a/sharing/people/user:{uid(hub, ALICE)}", headers=ALICE)
    assert left.status_code == 200 and hub.get("/p/a/api/access", headers=ALICE).json()["role"] == "viewer"  # general view
    assert hub.delete("/api/projects/a", headers=BOB).status_code == 200


def test_an_email_invite_lets_someone_in_on_their_first_sign_in(hub):
    hub.post("/api/projects", json={"id": "a", "access": "private"}, headers=ALICE)
    r = hub.put("/api/projects/a/sharing/people", json={"email": "carol@example.com", "role": "editor"}, headers=ALICE)
    [m] = r.json()["people"]
    assert m["pending"] and m["principal"] == "email:carol@example.com"
    assert ids(hub, CAROL) == ["a"] and hub.put("/p/a/api/nodes/nums", json={"source": NODE}, headers=CAROL).status_code == 200


def test_access_requests_are_approved_or_dismissed(hub):
    hub.post("/api/projects", json={"id": "a", "access": "private"}, headers=ALICE)
    r = hub.post("/api/projects/a/sharing/requests", json={"role": "editor", "message": "for the Q3 deck"}, headers=BOB)
    assert r.status_code == 201 and r.json()["requested"]["role"] == "editor"
    assert hub.get("/p/a/api/access", headers=BOB).json()["requested"]["message"] == "for the Q3 deck"
    assert hub.get("/api/projects", headers=ALICE).json()["projects"][0]["requests"] == 1
    [req] = hub.get("/api/projects/a/sharing", headers=ALICE).json()["requests"]
    assert req["user"]["name"] == "bob"
    after = hub.post(f"/api/projects/a/sharing/requests/{uid(hub, BOB)}/approve", json={"role": "viewer"}, headers=ALICE)
    assert after.json()["requests"] == [] and hub.get("/p/a/api/access", headers=BOB).json()["role"] == "viewer"
    # Asking for what you already have is pointless; asking for more works.
    assert hub.post("/api/projects/a/sharing/requests", json={"role": "viewer"}, headers=BOB).status_code == 400
    hub.post("/api/projects/a/sharing/requests", json={"role": "editor"}, headers=BOB)
    assert hub.delete(f"/api/projects/a/sharing/requests/{uid(hub, BOB)}", headers=ALICE).json()["requests"] == []


def test_a_share_link_opens_one_project_read_only_without_signing_in(hub):
    hub.post("/api/projects", json={"id": "a", "access": "private"}, headers=ALICE)
    hub.post("/api/projects", json={"id": "b", "access": "private"}, headers=ALICE)
    hub.put("/p/a/api/nodes/nums", json={"source": NODE}, headers=ALICE)
    made = hub.post("/api/projects/a/sharing/links", json={"name": "board", "expires_in_days": 7}, headers=ALICE)
    assert made.status_code == 201 and made.json()["url"].startswith("/s/exs_")
    assert hub.post("/api/projects/a/sharing/links", json={"expires_in_days": 0}, headers=ALICE).status_code == 400

    anon = hub  # no proxy headers: a stranger with the link (cookies kept by the client)
    assert anon.get("/p/a/api/project").status_code == 401
    opened = anon.get(made.json()["url"], follow_redirects=False)
    assert opened.status_code == 303 and opened.headers["location"] == "/p/a/"
    assert "path=/p/a/" in opened.headers["set-cookie"].lower()
    # With ?app=<view> the link lands on that view as an app; anything else is dropped.
    as_app = anon.get(made.json()["url"] + "?app=board_view", follow_redirects=False)
    assert as_app.status_code == 303 and as_app.headers["location"] == "/p/a/?app=board_view"
    odd = anon.get(made.json()["url"] + "?app=//evil.example/x", follow_redirects=False)
    assert odd.headers["location"] == "/p/a/"
    assert anon.get("/p/a/api/project").status_code == 200
    access = anon.get("/p/a/api/access").json()
    assert (access["role"], access["link"], access["user"]) == ("viewer", True, None)
    assert anon.put("/p/a/api/nodes/nums", json={"source": NODE}).status_code == 403
    assert anon.post("/p/a/api/query", json={"sql": "select 1 as x"}).status_code == 200
    # The cookie is for that project's paths; sent elsewhere it opens nothing.
    token = made.json()["token"]
    assert anon.get("/p/b/api/project", headers={"X-Exuvia-Link": token}).status_code == 401
    assert anon.get("/api/projects", headers={"X-Exuvia-Link": token}).status_code == 401
    assert anon.post("/p/a/mcp", json={}, headers={"X-Exuvia-Link": token}).status_code == 401
    hub.delete(f"/api/projects/a/sharing/links/{made.json()['id']}", headers=ALICE)
    assert anon.get("/p/a/api/project").status_code == 401
    assert anon.get(made.json()["url"]).status_code == 404

    hub.cookies.clear()
    # Someone signed in with a link gets at least a viewer's access too.
    _, token = hub.app.state.hub.sharing.create_link("a")
    assert hub.get("/p/a/api/project", headers={**BOB, "X-Exuvia-Link": token}).status_code == 200
    assert hub.get("/p/a/api/project", headers=BOB).status_code == 403


def test_hub_export_and_import_carry_sharing_and_are_for_admins(hub, tmp_path):
    hub.post("/api/projects", json={"id": "a", "access": "private"}, headers=ALICE)
    hub.put("/api/projects/a/sharing/people", json={"user": uid(hub, BOB), "role": "editor"}, headers=ALICE)
    assert hub.get("/api/hub/export", headers=ALICE).status_code == 403
    blob = hub.get("/api/hub/export", headers=ROOT).content
    names = tarfile.open(fileobj=io.BytesIO(blob)).getnames()
    assert ".exuvia/hub.db" in names
    with TestClient(create_hub_app(tmp_path / "fresh")) as fresh:
        assert fresh.post("/api/hub/import", content=blob, headers=ALICE).status_code == 403
        assert fresh.post("/api/hub/import", content=blob, headers=ROOT).status_code == 200
        assert ids(fresh, BOB) == ["a"] and ids(fresh, CAROL) == []
        assert fresh.get("/api/projects/a/sharing", headers=BOB).json()["you"] == "editor"


def test_deleting_a_project_forgets_its_sharing(hub):
    hub.post("/api/projects", json={"id": "a", "access": "private"}, headers=ALICE)
    _, token = hub.app.state.hub.sharing.create_link("a")
    hub.delete("/api/projects/a", headers=ALICE)
    hub.post("/api/projects", json={"id": "a"}, headers=BOB)  # the id again, someone else's
    assert hub.get("/api/projects/a/sharing", headers=BOB).json()["owner"]["name"] == "bob"
    assert hub.app.state.hub.sharing.link_project(token) is None


def test_only_admins_create_projects_for_someone_else(hub):
    bob = uid(hub, BOB)
    assert hub.post("/api/projects", json={"id": "a", "owner": bob}, headers=ALICE).status_code == 403
    assert hub.post("/api/projects", json={"id": "a", "owner": bob, "access": "private"}, headers=ROOT).status_code == 200
    assert ids(hub, BOB) == ["a"] and ids(hub, ALICE) == []
    assert hub.post("/api/projects", json={"id": "b", "owner": "nobody"}, headers=ROOT).status_code == 400


def test_people_lookup_is_for_signed_in_people(hub):
    found = hub.get("/api/people", params={"q": "bo"}, headers=ALICE).json()["people"]
    assert [p["name"] for p in found] == ["bob"] and set(found[0]) == {"id", "name", "email"}


def test_a_token_hub_shares_by_link_only(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH", "token")
    monkeypatch.setenv("EXUVIA_API_TOKEN", "s3cret")
    op = {"Authorization": "Bearer s3cret"}
    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        hub.post("/api/projects", json={"id": "a"}, headers=op)
        listing = hub.get("/api/projects", headers=op).json()
        assert listing["sharing"]["people"] is False and listing["projects"][0]["access"] == "owner"
        assert hub.patch("/api/projects/a/sharing", json={"general": "view"}, headers=op).status_code == 400
        link = hub.post("/api/projects/a/sharing/links", json={}, headers=op).json()
        anon = hub  # without the token
        assert anon.get("/p/a/api/project").status_code == 401
        anon.get(link["url"])
        assert anon.get("/p/a/api/project").status_code == 200
        assert anon.put("/p/a/api/nodes/nums", json={"source": NODE}).status_code == 403
        assert anon.get("/api/hub/export").status_code == 401


def test_a_hub_without_auth_has_no_sharing(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH", "token")
    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        hub.post("/api/projects", json={"id": "a", "access": "private"})
        assert hub.get("/p/a/api/project").status_code == 200
        assert hub.get("/api/projects/a/sharing").status_code == 404
        assert hub.get("/p/a/api/access").json()["sharing"] is False


# -- MCP --------------------------------------------------------------------------


def test_mcp_organizes_groups_and_sharing_as_its_person(tmp_path):
    """<hub>/mcp's group and sharing tools act as the agent token's person,
    with the API's checks: Alice puts a project in her group and adds Bob,
    who then can edit it but not manage the group."""
    import httpx

    from tests.test_mcp import _serve, http_client, text

    url, server, thread = _serve(create_hub_app(tmp_path / "hub"))
    try:
        for who in (ALICE, BOB):
            httpx.get(f"{url}/api/me", headers=who)
        alice = httpx.post(f"{url}/api/me/tokens", json={"name": "a"}, headers=ALICE).json()["token"]
        bob = httpx.post(f"{url}/api/me/tokens", json={"name": "b"}, headers=BOB).json()["token"]

        async def call(client, name, args):
            r = await client.call_tool(name, args)
            return r.is_error, text(r)

        async def as_alice():
            async with http_client(f"{url}/mcp", alice) as c:
                _, people = await call(c, "list_people", {"query": "bob"})
                bob_uid = json.loads(people)["people"][0]["id"]
                _, made = await call(c, "create_group", {"name": "Team"})
                gid = json.loads(made)["id"]
                await call(c, "create_project", {"id": "a", "access": "private"})
                await call(c, "create_project", {"id": "solo", "access": "private"})
                moved = await call(c, "set_project_group", {"project": "a", "group": gid})
                added = await call(c, "set_group_member", {"group": gid, "user": bob_uid, "role": "editor"})
                shared = await call(c, "set_project_member", {"project": "solo", "email": "Bob@Example.com", "role": "viewer"})
                opened = await call(c, "set_project_access", {"project": "solo", "access": "view"})
                sharing = await call(c, "get_project_sharing", {"project": "solo"})
                unshared = await call(c, "set_project_member", {"project": "solo", "email": "bob@example.com", "role": "none"})
            return locals()

        a = asyncio.run(as_alice())
        assert not a["moved"][0] and not a["added"][0]
        assert [(p["user"]["name"], p["role"]) for p in json.loads(a["added"][1])["people"]] == [("bob", "editor")]
        assert not a["shared"][0] and not a["opened"][0]
        assert json.loads(a["sharing"][1])["general"] == "view"
        assert not a["unshared"][0]

        async def as_bob():
            async with http_client(f"{url}/mcp", bob) as c:
                listing = json.loads(text(await c.call_tool("list_projects", {})))
                wrote = await call(c, "write_node", {"project": "a", "name": "nums", "kind": "python", "source": NODE})
                rename = await call(c, "update_group", {"group": a["gid"], "name": "Mine"})
                delete = await call(c, "delete_group", {"group": a["gid"]})
                take = await call(c, "set_project_group", {"project": "solo", "group": a["gid"]})
                share = await call(c, "set_project_member", {"project": "a", "email": "eve@example.com", "role": "editor"})
                left = await call(c, "set_group_member", {"group": a["gid"], "user": a["bob_uid"], "role": "none"})
                after = await call(c, "get_group", {"group": a["gid"]})
            return locals()

        b = asyncio.run(as_bob())
        assert {p["id"] for p in b["listing"]["projects"]} == {"a", "solo"}  # a by the group, solo by general access
        assert not b["wrote"][0]
        assert b["rename"][0] and "HTTP 403" in b["rename"][1] and "owner" in b["rename"][1]
        assert b["delete"][0] and "HTTP 403" in b["delete"][1]
        assert b["take"][0] and "only the owner of project solo" in b["take"][1]
        assert b["share"][0] and "HTTP 403" in b["share"][1]
        assert not b["left"][0] and json.loads(b["left"][1]) == {"ok": True, "left": True}  # anyone can leave
        assert b["after"][0] and "HTTP 404" in b["after"][1]
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_mcp_shows_a_viewer_only_what_they_may_do(tmp_path):
    import httpx

    from tests.test_mcp import _serve, http_client, text

    url, server, thread = _serve(create_hub_app(tmp_path / "hub"))
    try:
        for who in (ALICE, BOB):
            httpx.get(f"{url}/api/me", headers=who)
        bob_uid = httpx.get(f"{url}/api/me", headers=BOB).json()["user"]["uid"]
        httpx.post(f"{url}/api/projects", json={"id": "a", "access": "private"}, headers=ALICE)
        httpx.post(f"{url}/api/projects", json={"id": "secret", "access": "private"}, headers=ALICE)
        httpx.put(f"{url}/p/a/api/nodes/nums", json={"source": NODE}, headers=ALICE)
        httpx.put(f"{url}/api/projects/a/sharing/people", json={"user": bob_uid, "role": "viewer"}, headers=ALICE)
        token = httpx.post(f"{url}/api/me/tokens", json={"name": "codex"}, headers=BOB).json()["token"]

        async def one_project():
            async with http_client(f"{url}/p/a/mcp", token) as client:
                tools = {t.name for t in (await client.list_tools()).tools}
                read = await client.call_tool("read_node", {"name": "nums"})
                write = await client.call_tool("write_node", {"name": "x", "kind": "python", "source": NODE})
                return tools, read, write

        tools, read, write = asyncio.run(one_project())
        assert "read_node" in tools and "write_node" not in tools and "run_node" not in tools
        assert not read.is_error and write.is_error and "PERMISSION" in text(write)

        async def whole_hub():
            async with http_client(f"{url}/mcp", token) as client:
                listing = json.loads(text(await client.call_tool("list_projects", {})))
                write = await client.call_tool("write_node", {"project": "a", "name": "x", "kind": "python", "source": NODE})
                hidden = await client.call_tool("list_nodes", {"project": "secret"})
                return listing, write, hidden, client.instructions

        listing, write, hidden, instructions = asyncio.run(whole_hub())
        assert [p["id"] for p in listing["projects"]] == ["a"]
        assert write.is_error and "PERMISSION" in text(write)
        assert hidden.is_error and "don't have access" in text(hidden)
        assert "secret" not in instructions

        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
        r = httpx.post(f"{url}/p/secret/mcp", json=init,
                       headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}"})
        assert r.status_code == 403
    finally:
        server.should_exit = True
        thread.join(timeout=10)
