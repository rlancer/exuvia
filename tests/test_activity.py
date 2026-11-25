"""When each person last opened and changed each hub project
(exuvia/activity.py), which the hub's home page sorts and groups by."""

import pytest
from fastapi.testclient import TestClient

from exuvia import activity
from exuvia.activity import ActivityStore
from exuvia.db import SQLiteDatabase, close_shared
from exuvia.server import create_hub_app

NODE = '''import exuvia


@exuvia.node()
def nums():
    return {"n": 1}
'''


def headers(user):
    return {"X-Forwarded-User": user, "X-Forwarded-Email": f"{user}@example.com"}


ALICE = headers("alice")
BOB = headers("bob")


@pytest.fixture(autouse=True)
def headers_mode(monkeypatch):
    for name in ("EXUVIA_API_TOKEN", "EXUVIA_BROWSER_AUTH", "EXUVIA_AUTH_USER_HEADER", "EXUVIA_AUTH_EMAIL_HEADER",
                 "EXUVIA_AUTH_TRUSTED_PROXIES", "EXUVIA_DB", "EXUVIA_DB_SCOPE", "EXUVIA_DEFAULT_ACCESS"):
        monkeypatch.delenv(name, raising=False)
    close_shared()
    yield
    close_shared()


def test_the_store_notes_opens_and_changes_once_a_minute(tmp_path):
    s = ActivityStore(SQLiteDatabase(tmp_path / "hub.db"), owns_db=True)
    s.touch("a", "u1", now=100)
    s.touch("a", "u1", now=130)  # within the minute: not written
    assert s.of("u1") == {"a": {"opened_at": 100, "edited_at": None}}
    s.touch("a", "u1", edited=True, now=140)  # a change is written even so
    s.touch("a", "u1", now=200)  # opening again keeps the change
    s.touch("b", None, edited=True, now=150)  # nobody signed in
    assert s.of("u1") == {"a": {"opened_at": 200, "edited_at": 140}}
    assert s.of(None) == {"b": {"opened_at": 150, "edited_at": 150}}
    s.touch("a", "u2", edited=True, now=300)
    assert s.changed() == {"a": 300, "b": 150}
    s.forget("a")
    assert s.of("u1") == {} and s.changed() == {"b": 150}
    s.touch("a", "u1", now=210)  # a new project of the same id starts clean
    assert s.of("u1") == {"a": {"opened_at": 210, "edited_at": None}}
    s.close()


def test_the_project_list_says_who_used_and_changed_what(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH", "headers")
    monkeypatch.setattr(activity, "TOUCH_EVERY", 0)
    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        for who in (ALICE, BOB):
            hub.get("/api/me", headers=who)
        hub.post("/api/projects", json={"id": "a", "access": "edit"}, headers=ALICE)
        hub.post("/api/projects", json={"id": "b", "access": "edit"}, headers=ALICE)

        def listed(who):
            return {p["id"]: p for p in hub.get("/api/projects", headers=who).json()["projects"]}

        # Creating one is working on it.
        mine = listed(ALICE)
        assert mine["a"]["edited_at"] and mine["b"]["edited_at"] >= mine["a"]["edited_at"]
        assert mine["a"]["changed_at"] >= mine["a"]["edited_at"]
        # Bob only looks at a, then changes b.
        assert listed(BOB)["a"]["opened_at"] is None
        hub.get("/p/a/api/project", headers=BOB)
        bobs = listed(BOB)
        assert bobs["a"]["opened_at"] and bobs["a"]["edited_at"] is None and bobs["b"]["opened_at"] is None
        assert hub.put("/p/b/api/nodes/nums", json={"source": NODE}, headers=BOB).status_code == 200
        bobs = listed(BOB)
        assert bobs["b"]["edited_at"] >= bobs["a"]["opened_at"]
        # Bob's change is a change for Alice too, but not her own.
        alices = listed(ALICE)
        assert alices["b"]["changed_at"] >= bobs["b"]["edited_at"] and alices["b"]["edited_at"] == mine["b"]["edited_at"]
        # Deleting forgets it.
        hub.delete("/api/projects/b", headers=ALICE)
        hub.post("/api/projects", json={"id": "b"}, headers=BOB)
        assert listed(ALICE)["b"]["edited_at"] is None


def test_a_hub_nobody_signs_in_to_notes_use_for_everyone(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "TOUCH_EVERY", 0)
    with TestClient(create_hub_app(tmp_path / "hub")) as hub:
        hub.post("/api/projects", json={"id": "a"})
        hub.get("/p/a/api/project")
        [p] = hub.get("/api/projects").json()["projects"]
        assert p["opened_at"] >= p["edited_at"] > 0 and p["changed_at"] >= p["edited_at"]
