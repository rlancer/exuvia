"""Groups of hub projects (exuvia/groups.py): anyone signed in creates one,
its owner adds people with roles, a group role is also a role on each of its
projects, and its projects inherit its environment variables and secrets.
"""

import io
import json
import os
import tarfile
import uuid

import pytest
from fastapi.testclient import TestClient

from exuvia import envvars
from exuvia.auth import Identity
from exuvia.db import close_shared
from exuvia.groups import open_groups
from exuvia.server import create_hub_app
from exuvia.sharing import open_sharing
from exuvia.users import open_users

ENV_NODE = '''import os

import exuvia


@exuvia.node()
def env():
    print("calling with key", os.environ.get("FRED_API_KEY"), "and", os.environ.get("SHARED_TOKEN"))
    return {"key": os.environ.get("FRED_API_KEY"), "region": os.environ.get("REGION"),
            "shared": os.environ.get("SHARED_TOKEN"), "server": os.environ.get("FROM_SERVER")}
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

PG_URL = os.environ.get("EXUVIA_TEST_POSTGRES_URL", "")
BACKENDS = ["sqlite-local", "sqlite-shared"] + (["postgres"] if PG_URL else [])


@pytest.fixture(autouse=True)
def headers_mode(monkeypatch):
    for name in ("EXUVIA_API_TOKEN", "EXUVIA_BROWSER_AUTH", "EXUVIA_AUTH_USER_HEADER", "EXUVIA_AUTH_EMAIL_HEADER",
                 "EXUVIA_AUTH_TRUSTED_PROXIES", "EXUVIA_DB", "EXUVIA_DB_SCOPE", "EXUVIA_DEFAULT_ACCESS",
                 envvars.KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXUVIA_AUTH", "headers")
    monkeypatch.setenv("EXUVIA_ADMINS", "root")
    close_shared()
    yield
    close_shared()


def backend(name, tmp_path, monkeypatch):
    if name == "sqlite-shared":
        monkeypatch.setenv("EXUVIA_DB", f"sqlite:///{(tmp_path / 'shared.db').as_posix()}")
    elif name == "postgres":
        monkeypatch.setenv("EXUVIA_DB", PG_URL)
    if name != "sqlite-local":
        monkeypatch.setenv("EXUVIA_DB_SCOPE", f"t{uuid.uuid4().hex[:8]}/")


@pytest.fixture(params=BACKENDS)
def hub(request, tmp_path, monkeypatch):
    """A hub where people sign in, on each backend, with alice, bob, carol
    and root (an admin) in its user list."""
    backend(request.param, tmp_path, monkeypatch)
    with TestClient(create_hub_app(tmp_path / "hub")) as client:
        for who in (ALICE, BOB, CAROL, ROOT):
            client.get("/api/me", headers=who)
        yield client


def uid(client, who):
    return client.get("/api/me", headers=who).json()["user"]["uid"]


def ids(client, who):
    return [p["id"] for p in client.get("/api/projects", headers=who).json()["projects"]]


def new_group(client, who=ALICE, name="Research"):
    r = client.post("/api/groups", json={"name": name}, headers=who)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def ws_of(client, pid):
    return client.app.state.hub._ctxs[pid].ws


def run_env_node(client, pid, who=ALICE):
    assert client.put(f"/p/{pid}/api/nodes/env", json={"source": ENV_NODE, "kind": "python"}, headers=who).status_code == 200
    done = client.post(f"/p/{pid}/api/nodes/env/run", json={"wait": 60, "force": True}, headers=who).json()
    assert done["ok"], done
    ws = ws_of(client, pid)
    last = ws.store.last_success("env", "")
    log = ws.store.run(done["run_id"])["log"] or ""
    return json.loads((ws.project.root / last["artifact_path"]).read_text(encoding="utf-8")), log


# -- the store -----------------------------------------------------------------------


@pytest.mark.parametrize("name", BACKENDS)
def test_roles_from_the_owner_people_and_email_invites(name, tmp_path, monkeypatch):
    backend(name, tmp_path, monkeypatch)
    users = open_users(tmp_path / "hub.db")
    sharing = open_sharing(tmp_path / "hub.db", users)
    groups = open_groups(tmp_path / "hub.db", users, sharing)
    sharing.groups = groups

    def person(n):
        u = users.sign_in(Identity(user=n, email=f"{n}@example.com", method="headers"))
        return Identity(user=n, email=f"{n}@example.com", method="headers", uid=u.id, role=u.role)

    alice, bob, carol, dave = (person(n) for n in ("alice", "bob", "carol", "dave"))
    gid = groups.create("Research", alice.uid)
    groups.grant(gid, "editor", user_id=bob.uid)
    groups.grant(gid, "viewer", email="Carol@Example.com")  # in the user list: stored as her user
    groups.grant(gid, "viewer", email="erin@example.com")  # not signed in yet: waits for her
    assert [groups.role(gid, p) for p in (alice, bob, carol, dave)] == ["owner", "editor", "viewer", None]
    assert groups.role(gid, None) == "owner"  # the operator
    assert [m["pending"] for m in groups.members(gid)] == [False, False, True]
    erin = users.sign_in(Identity(user="erin", email="erin@example.com", method="headers"))
    assert groups.role(gid, Identity(user="erin", email="erin@example.com", method="headers", uid=erin.id)) == "viewer"

    # Folder-style: the group's roles reach its projects; the owner edits.
    sharing.init_project("p", dave.uid, "private")
    assert sharing.role("p", alice) is None
    groups.set_project_group("p", gid)
    assert [sharing.role("p", x) for x in (alice, bob, carol, dave)] == ["editor", "editor", "viewer", "owner"]

    # A project is in one group at most.
    other = groups.create("Other", dave.uid)
    groups.set_project_group("p", other)
    assert groups.group_of("p", fresh=True)["id"] == other and groups.projects(gid) == []
    assert sharing.role("p", alice) is None

    groups.set_owner(gid, bob.uid)
    assert groups.role(gid, bob) == "owner" and groups.role(gid, alice) == "editor"
    with pytest.raises(ValueError):
        groups.grant(gid, "owner", user_id=carol.uid)
    assert groups.delete(other) == ["p"]
    assert groups.group_of("p", fresh=True) is None and not groups.exists(other)
    users.close()
    close_shared()


# -- the API -------------------------------------------------------------------------


def test_anyone_signed_in_creates_a_group_and_owns_it(hub):
    gid = new_group(hub, BOB)
    got = hub.get(f"/api/groups/{gid}", headers=BOB).json()
    assert got["owner"]["name"] == "bob" and got["you"] == "owner" and got["can_manage"]
    assert [g["id"] for g in hub.get("/api/groups", headers=BOB).json()["groups"]] == [gid]
    # Others don't see it, and can't tell it exists.
    assert hub.get("/api/groups", headers=ALICE).json()["groups"] == []
    assert hub.get(f"/api/groups/{gid}", headers=ALICE).status_code == 404
    # Admins see every group.
    assert hub.get(f"/api/groups/{gid}", headers=ROOT).json()["you"] == "owner"
    assert hub.post("/api/groups", json={"name": "  "}, headers=BOB).status_code == 400


def test_only_the_owner_manages_people_and_anyone_can_leave(hub):
    gid = new_group(hub)
    bob, carol = uid(hub, BOB), uid(hub, CAROL)
    assert hub.put(f"/api/groups/{gid}/people", json={"user": bob, "role": "editor"}, headers=ALICE).status_code == 200
    assert hub.put(f"/api/groups/{gid}/people", json={"user": carol, "role": "viewer"}, headers=BOB).status_code == 403
    assert hub.patch(f"/api/groups/{gid}", json={"name": "Renamed"}, headers=BOB).status_code == 403
    assert hub.delete(f"/api/groups/{gid}", headers=BOB).status_code == 403
    assert hub.get(f"/api/groups/{gid}", headers=BOB).json()["you"] == "editor"
    assert hub.delete(f"/api/groups/{gid}/people/user:{bob}", headers=BOB).json() == {"ok": True, "left": True}
    assert hub.get(f"/api/groups/{gid}", headers=BOB).status_code == 404
    # Handing it over keeps the old owner on as an editor.
    hub.patch(f"/api/groups/{gid}", json={"owner": carol}, headers=ALICE)
    assert hub.get(f"/api/groups/{gid}", headers=ALICE).json()["you"] == "editor"
    assert hub.get(f"/api/groups/{gid}", headers=CAROL).json()["you"] == "owner"


def test_a_group_role_is_a_role_on_its_projects(hub):
    hub.post("/api/projects", json={"id": "a", "access": "private"}, headers=ALICE)
    gid = new_group(hub)
    hub.put(f"/api/groups/{gid}/people", json={"user": uid(hub, BOB), "role": "viewer"}, headers=ALICE)
    hub.put(f"/api/groups/{gid}/people", json={"user": uid(hub, CAROL), "role": "editor"}, headers=ALICE)
    assert ids(hub, BOB) == [] and hub.get("/p/a/api/project", headers=BOB).status_code == 403
    assert hub.put("/api/projects/a/group", json={"group": gid}, headers=ALICE).status_code == 200

    # The viewer opens it read-only; the editor changes it.
    assert ids(hub, BOB) == ["a"] and hub.get("/p/a/api/project", headers=BOB).status_code == 200
    node = {"source": ENV_NODE, "kind": "python"}
    assert hub.put("/p/a/api/nodes/env", json=node, headers=BOB).status_code == 403
    assert hub.put("/p/a/api/nodes/env", json=node, headers=CAROL).status_code == 200
    listed = {p["id"]: p for p in hub.get("/api/projects", headers=CAROL).json()["projects"]}
    assert listed["a"]["access"] == "editor" and listed["a"]["group"] == {"id": gid, "name": "Research"}
    access = hub.get("/p/a/api/access", headers=BOB).json()
    assert access["role"] == "viewer" and access["group"] == {"id": gid, "name": "Research", "access": "viewer"}

    # A group's owner edits its projects but does not own them: deleting one
    # and its sharing stay with the project's owner.
    bobs = new_group(hub, BOB, "Bob's")
    hub.post("/api/projects", json={"id": "b", "access": "private"}, headers=CAROL)
    hub.put(f"/api/groups/{bobs}/people", json={"user": uid(hub, CAROL), "role": "editor"}, headers=BOB)
    assert hub.put("/api/projects/b/group", json={"group": bobs}, headers=CAROL).status_code == 200
    assert hub.get("/p/b/api/access", headers=BOB).json()["role"] == "editor"
    assert hub.delete("/api/projects/b", headers=BOB).status_code == 403
    assert hub.patch("/api/projects/b/sharing", json={"general": "edit"}, headers=BOB).status_code == 403


def test_adding_a_project_needs_its_owner_and_an_editor_of_the_group(hub):
    hub.post("/api/projects", json={"id": "a", "access": "edit"}, headers=ALICE)  # everyone can edit
    gid = new_group(hub, BOB)
    hub.put(f"/api/groups/{gid}/people", json={"user": uid(hub, CAROL), "role": "viewer"}, headers=BOB)
    # Bob edits the project and owns the group, but the project is Alice's.
    r = hub.put("/api/projects/a/group", json={"group": gid}, headers=BOB)
    assert r.status_code == 403 and "owner of project a" in r.json()["detail"]
    # Alice owns the project but has no role on the group.
    assert hub.put("/api/projects/a/group", json={"group": gid}, headers=ALICE).status_code == 404
    hub.put(f"/api/groups/{gid}/people", json={"user": uid(hub, ALICE), "role": "viewer"}, headers=BOB)
    r = hub.put("/api/projects/a/group", json={"group": gid}, headers=ALICE)
    assert r.status_code == 403 and "editor of group" in r.json()["detail"]
    hub.put(f"/api/groups/{gid}/people", json={"user": uid(hub, ALICE), "role": "editor"}, headers=BOB)
    assert hub.put("/api/projects/a/group", json={"group": gid}, headers=ALICE).json()["group"]["id"] == gid
    assert [p["id"] for p in hub.get(f"/api/groups/{gid}", headers=CAROL).json()["projects"]] == ["a"]

    # Taking it out: the group's editor (Bob) or the project's owner, not a group viewer.
    assert hub.put("/api/projects/a/group", json={"group": None}, headers=CAROL).status_code == 403
    assert hub.put("/api/projects/a/group", json={"group": None}, headers=BOB).json()["group"] is None
    hub.put("/api/projects/a/group", json={"group": gid}, headers=ALICE)
    assert hub.put("/api/projects/a/group", json={"group": None}, headers=ALICE).json()["group"] is None


def test_group_variables_editors_set_viewers_see_names_and_secrets_stay_hidden(hub):
    gid = new_group(hub)
    hub.put(f"/api/groups/{gid}/people", json={"user": uid(hub, BOB), "role": "viewer"}, headers=ALICE)
    hub.put(f"/api/groups/{gid}/people", json={"user": uid(hub, CAROL), "role": "editor"}, headers=ALICE)
    assert hub.put(f"/api/groups/{gid}/env/SHARED_TOKEN", json={"value": "x" * 8}, headers=BOB).status_code == 403
    r = hub.put(f"/api/groups/{gid}/env/SHARED_TOKEN", json={"value": "  group-secret-777\r\n"}, headers=CAROL)
    assert r.status_code == 200 and r.json()["variables"][0]["value"] is None
    hub.put(f"/api/groups/{gid}/env/REGION", json={"value": "eu-west-1", "secret": False}, headers=CAROL)
    seen = hub.get(f"/api/groups/{gid}", headers=BOB).json()
    assert {v["name"]: v["value"] for v in seen["variables"]} == {"REGION": "eu-west-1", "SHARED_TOKEN": None}
    assert "group-secret-777" not in json.dumps(seen)
    # The same rules as a project's.
    assert hub.put(f"/api/groups/{gid}/env/EXUVIA_X", json={"value": "1"}, headers=CAROL).status_code == 400
    assert hub.put(f"/api/groups/{gid}/env/SHARED_TOKEN", json={"secret": False}, headers=CAROL).status_code == 400
    assert hub.delete(f"/api/groups/{gid}/env/REGION", headers=BOB).status_code == 403
    assert [v["name"] for v in hub.delete(f"/api/groups/{gid}/env/REGION", headers=CAROL).json()["variables"]] == ["SHARED_TOKEN"]


def test_projects_inherit_group_variables_and_their_own_win(hub, monkeypatch):
    monkeypatch.setenv("FROM_SERVER", "server")
    monkeypatch.setenv("REGION", "server-region")
    hub.post("/api/projects", json={"id": "a"}, headers=ALICE)
    gid = new_group(hub)
    hub.put(f"/api/groups/{gid}/env/SHARED_TOKEN", json={"value": "group-secret-777"}, headers=ALICE)
    hub.put(f"/api/groups/{gid}/env/REGION", json={"value": "group-region", "secret": False}, headers=ALICE)
    hub.put(f"/api/groups/{gid}/env/FRED_API_KEY", json={"value": "group-fred-key"}, headers=ALICE)
    hub.put("/p/a/api/env/FRED_API_KEY", json={"value": "project-fred-key"}, headers=ALICE)

    out, _ = run_env_node(hub, "a")
    assert out["shared"] is None and out["region"] == "server-region"  # not in the group yet

    hub.put("/api/projects/a/group", json={"group": gid}, headers=ALICE)
    out, log = run_env_node(hub, "a")
    # The project's own wins, then the group's, then the server's environment.
    assert out == {"key": "project-fred-key", "region": "group-region", "shared": "group-secret-777", "server": "server"}
    assert "group-secret-777" not in log and "project-fred-key" not in log and "calling with key *** and ***" in log

    state = hub.get("/p/a/api/env", headers=ALICE).json()
    assert [v["name"] for v in state["variables"]] == ["FRED_API_KEY"]
    assert state["group"] == {"id": gid, "name": "Research"}
    inherited = {v["name"]: v for v in state["inherited"]}
    assert inherited["FRED_API_KEY"]["overridden"] and not inherited["SHARED_TOKEN"]["overridden"]
    assert inherited["SHARED_TOKEN"]["value"] is None and inherited["REGION"]["value"] == "group-region"
    assert "group-secret-777" not in json.dumps(state) and "group-fred-key" not in json.dumps(state)

    # The agent's list_env says where each comes from.
    listed = hub.post("/p/a/api/tools/list_env", json={}, headers=ALICE).json()
    variables = {v["name"]: v for v in json.loads(listed["text"])["variables"]}
    assert variables["FRED_API_KEY"] == {"name": "FRED_API_KEY", "secret": True, "value": None, "source": "project",
                                         "overrides_group": "Research"}
    assert variables["SHARED_TOKEN"]["source"] == "group" and variables["SHARED_TOKEN"]["group"] == "Research"
    # A request for a variable the group has is answered at once.
    asked = hub.post("/p/a/api/tools/request_secret", json={"name": "SHARED_TOKEN", "reason": "x"}, headers=ALICE).json()
    assert json.loads(asked["text"])["status"] == "set"

    # Out of the group, the next run goes without.
    hub.put("/api/projects/a/group", json={"group": None}, headers=ALICE)
    out, _ = run_env_node(hub, "a")
    assert out["shared"] is None and out["key"] == "project-fred-key"


def test_group_secrets_are_encrypted_with_the_hubs_key(hub, tmp_path):
    gid = new_group(hub)
    hub.put(f"/api/groups/{gid}/env/SHARED_TOKEN", json={"value": "group-secret-777"}, headers=ALICE)
    h = hub.app.state.hub
    stored = h.groups.env_rows(gid)[0]["value"]
    assert stored.startswith("enc:v1:") and "group-secret-777" not in stored
    assert (tmp_path / "hub" / ".exuvia" / "secrets.key").is_file()
    assert h.group_env(gid).rows()[0]["value"] == "group-secret-777"


def test_a_server_key_encrypts_group_secrets_and_rotates(tmp_path, monkeypatch):
    old, new = envvars.new_key(), envvars.new_key()
    monkeypatch.setenv(envvars.KEY_ENV, old)
    with TestClient(create_hub_app(tmp_path / "hub")) as client:
        client.get("/api/me", headers=ALICE)
        gid = new_group(client)
        client.put(f"/api/groups/{gid}/env/SHARED_TOKEN", json={"value": "group-secret-777"}, headers=ALICE)
    assert not (tmp_path / "hub" / ".exuvia" / "secrets.key").exists()
    monkeypatch.setenv(envvars.KEY_ENV, f"{new},{old}")
    with TestClient(create_hub_app(tmp_path / "hub")) as client:
        h = client.app.state.hub
        assert h.groups.env_rows(gid)[0]["value"].startswith(f"enc:v1:{envvars.key_id(new.encode())}:")
        assert h.group_env(gid).rows()[0]["value"] == "group-secret-777"


def test_deleting_projects_and_groups(hub):
    hub.post("/api/projects", json={"id": "a"}, headers=ALICE)
    gid = new_group(hub)
    hub.put("/api/projects/a/group", json={"group": gid}, headers=ALICE)
    hub.delete("/api/projects/a", headers=ALICE)
    hub.post("/api/projects", json={"id": "a"}, headers=BOB)  # the id again, someone else's
    assert hub.get(f"/api/groups/{gid}", headers=ALICE).json()["projects"] == []
    assert hub.get("/p/a/api/env", headers=BOB).json()["group"] is None

    hub.post("/api/projects", json={"id": "b"}, headers=ALICE)
    hub.put("/api/projects/b/group", json={"group": gid}, headers=ALICE)
    assert hub.delete(f"/api/groups/{gid}", headers=ALICE).json() == {"ok": True, "projects": ["b"]}
    assert hub.get("/p/b/api/env", headers=ALICE).json()["group"] is None and "b" in ids(hub, ALICE)


def test_hub_export_and_import_carry_groups_and_the_hubs_key(tmp_path):
    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        for who in (ALICE, BOB, ROOT):
            hub.get("/api/me", headers=who)
        hub.post("/api/projects", json={"id": "a", "access": "private"}, headers=ALICE)
        gid = new_group(hub)
        hub.put(f"/api/groups/{gid}/people", json={"user": uid(hub, BOB), "role": "viewer"}, headers=ALICE)
        hub.put(f"/api/groups/{gid}/env/SHARED_TOKEN", json={"value": "group-secret-777"}, headers=ALICE)
        hub.put("/api/projects/a/group", json={"group": gid}, headers=ALICE)
        blob = hub.get("/api/hub/export", headers=ROOT).content
    names = tarfile.open(fileobj=io.BytesIO(blob)).getnames()
    assert ".exuvia/hub.db" in names and ".exuvia/secrets.key" in names
    with TestClient(create_hub_app(tmp_path / "fresh")) as fresh:
        assert fresh.post("/api/hub/import", content=blob, headers=ROOT).status_code == 200
        assert ids(fresh, BOB) == ["a"]
        assert fresh.get(f"/api/groups/{gid}", headers=BOB).json()["you"] == "viewer"
        out, _ = run_env_node(fresh, "a")
        assert out["shared"] == "group-secret-777"


def test_with_a_server_key_the_export_carries_no_key(tmp_path, monkeypatch):
    monkeypatch.setenv(envvars.KEY_ENV, envvars.new_key())
    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        hub.get("/api/me", headers=ALICE)
        gid = new_group(hub)
        hub.put(f"/api/groups/{gid}/env/SHARED_TOKEN", json={"value": "group-secret-777"}, headers=ALICE)
        hub.get("/api/me", headers=ROOT)
        blob = hub.get("/api/hub/export", headers=ROOT).content
    names = tarfile.open(fileobj=io.BytesIO(blob)).getnames()
    assert ".exuvia/hub.db" in names and ".exuvia/secrets.key" not in names


def test_a_hub_nobody_signs_in_to_still_shares_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH", "token")
    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        hub.post("/api/projects", json={"id": "a"})
        gid = new_group(hub, {})
        assert hub.get("/api/groups").json()["people"] is False
        assert hub.put(f"/api/groups/{gid}/people", json={"email": "x@example.com"}).status_code == 400
        hub.put(f"/api/groups/{gid}/env/SHARED_TOKEN", json={"value": "group-secret-777"})
        hub.put("/api/projects/a/group", json={"group": gid})
        out, _ = run_env_node(hub, "a", {})
        assert out["shared"] == "group-secret-777"


def test_exuvia_env_sets_a_groups_variables_over_the_hubs_api(tmp_path, monkeypatch):
    from tests.test_env_cli import cli
    from tests.test_mcp import _serve

    monkeypatch.setenv("EXUVIA_AUTH", "token")
    monkeypatch.setenv("EXUVIA_API_TOKEN", "s3cret")
    url, server, thread = _serve(create_hub_app(tmp_path / "hub"))
    try:
        import httpx

        op = {"Authorization": "Bearer s3cret"}
        httpx.post(f"{url}/api/projects", json={"id": "a"}, headers=op)
        gid = httpx.post(f"{url}/api/groups", json={"name": "Research"}, headers=op).json()["id"]
        httpx.put(f"{url}/api/projects/a/group", json={"group": gid}, headers=op)
        auth = {"EXUVIA_API_TOKEN": "s3cret"}
        out = cli("set", "SHARED_TOKEN", "--url", url, "--group", gid, "--stdin", stdin="group-secret-777", env=auth).stdout
        assert "Every project in the group gets it" in out
        assert "SHARED_TOKEN  secret (hidden)" in cli("list", "--url", url, "--group", gid, env=auth).stdout
        listing = cli("list", "--url", f"{url}/p/a", env=auth).stdout
        assert "SHARED_TOKEN  secret (hidden)  (from group Research)" in listing and "group-secret" not in listing
        assert "--url" in cli("list", "--group", gid, check=False).stderr
        assert "Removed SHARED_TOKEN" in cli("rm", "SHARED_TOKEN", "--url", url, "--group", gid, env=auth).stdout
    finally:
        server.should_exit = True
        thread.join(timeout=10)
