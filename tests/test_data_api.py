"""The data API: read-only keys scoped to chosen hub projects, and SQL across
them (exuvia/datakeys.py, exuvia/dataapi.py)."""

import io

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from exuvia import dataapi
from exuvia.db import close_shared
from exuvia.server import create_hub_app

ORDERS = '''import exuvia


@exuvia.node()
def orders():
    """One row per order."""
    import pandas as pd

    return pd.DataFrame({"account": [1, 1, 2], "amount": [10.0, 5.0, 7.5]})
'''

ACCOUNTS = '''import exuvia


@exuvia.node()
def accounts():
    import pandas as pd

    return pd.DataFrame({"account": [1, 2], "name": ["Acme", "Globex"]})
'''

JOIN = (
    'select a.name, sum(o.amount) as total from "sales".orders o join crm.accounts a using (account) '
    "group by a.name order by a.name"
)
ARROW = {"Accept": "application/vnd.apache.arrow.stream"}


def who(user):
    return {"X-Forwarded-User": user, "X-Forwarded-Email": f"{user}@example.com"}


ALICE, BOB, ROOT = who("alice"), who("bob"), who("root")


@pytest.fixture(autouse=True)
def headers_mode(monkeypatch):
    for name in ("EXUVIA_API_TOKEN", "EXUVIA_BROWSER_AUTH", "EXUVIA_AUTH_TRUSTED_PROXIES", "EXUVIA_DB",
                 "EXUVIA_DB_SCOPE", "EXUVIA_DEFAULT_ACCESS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXUVIA_AUTH", "headers")
    monkeypatch.setenv("EXUVIA_ADMINS", "root")
    monkeypatch.setattr(dataapi, "ACCESS_TTL", 0.0)  # see sharing changes at once
    close_shared()
    yield
    close_shared()


def project(client, pid, node, source, as_=ALICE, access="view"):
    assert client.post("/api/projects", json={"id": pid, "access": access}, headers=as_).status_code == 200
    put = client.put(f"/p/{pid}/api/nodes/{node}", json={"source": source}, headers=as_)
    assert put.status_code == 200, put.text
    run = client.post(f"/p/{pid}/api/nodes/{node}/run", json={"wait": 120}, headers=as_).json()
    assert run["ok"], run


@pytest.fixture
def hub(tmp_path):
    with TestClient(create_hub_app(tmp_path / "hub")) as client:
        for person in (ALICE, BOB, ROOT):
            client.get("/api/me", headers=person)
        project(client, "sales", "orders", ORDERS)
        project(client, "crm", "accounts", ACCOUNTS)
        project(client, "secret", "accounts", ACCOUNTS, as_=BOB, access="private")
        yield client


def make_key(client, projects, as_=ALICE, name="dashboard"):
    r = client.post("/api/data-keys", json={"name": name, "projects": projects}, headers=as_)
    assert r.status_code == 201, r.text
    return r.json()


def bearer(key):
    return {"Authorization": f"Bearer {key['key']}"}


def test_a_key_lists_its_projects_and_queries_across_them(hub):
    key = make_key(hub, ["sales", "crm"])
    assert key["key"].startswith("exd_") and key["projects"] == ["crm", "sales"]
    listed = hub.get("/api/data-keys", headers=ALICE).json()["keys"]
    assert [k["id"] for k in listed] == [key["id"]] and "key" not in listed[0]

    got = hub.get("/api/data/projects", headers=bearer(key)).json()
    assert [p["id"] for p in got["projects"]] == ["crm", "sales"]
    [orders] = got["projects"][1]["nodes"]
    assert (orders["name"], orders["kind"], orders["rows"], orders["description"]) == ("orders", "table", 3, "One row per order.")
    assert [c["name"] for c in orders["columns"]] == ["account", "amount"]

    r = hub.post("/api/data/query", json={"sql": JOIN}, headers=bearer(key))
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [["Acme", 15.0], ["Globex", 7.5]]
    assert sorted(r.json()["projects"]) == ["crm", "sales"]  # attached because the SQL names them

    r = hub.post("/api/data/query", json={"sql": JOIN}, headers={**bearer(key), **ARROW})
    table = pa.ipc.open_stream(io.BytesIO(r.content)).read_all()
    assert table.to_pydict() == {"name": ["Acme", "Globex"], "total": [15.0, 7.5]}

    rows = hub.get("/api/data/projects/sales/nodes/orders", headers=bearer(key)).json()
    assert rows["total"] == 3
    r = hub.get("/api/data/projects/crm/nodes/accounts", headers={**bearer(key), **ARROW})
    assert pa.ipc.open_stream(io.BytesIO(r.content)).read_all().num_rows == 2


def test_the_query_sees_a_new_run_at_once(hub):
    key = make_key(hub, ["crm"])
    sql = {"sql": "select count(*) from crm.accounts"}
    assert hub.post("/api/data/query", json=sql, headers=bearer(key)).json()["rows"] == [[2]]
    three = ACCOUNTS.replace('[1, 2], "name": ["Acme", "Globex"]', '[1, 2, 3], "name": ["Acme", "Globex", "Initech"]')
    hub.put("/p/crm/api/nodes/accounts", json={"source": three}, headers=ALICE)
    assert hub.post("/p/crm/api/nodes/accounts/run", json={"wait": 120}, headers=ALICE).json()["ok"]
    assert hub.post("/api/data/query", json=sql, headers=bearer(key)).json()["rows"] == [[3]]


@pytest.mark.parametrize("sql", [
    "drop view crm.accounts",
    "create table crm.main.x as select 1",
    "attach ':memory:' as other",
    "set threads = 1",
    "select 1; select 2",
    "select * from read_csv('/etc/passwd')",
    "copy (select 1) to 'out.csv'",
    # EXPLAIN ANALYZE runs what it explains: the transaction is read-only too.
    "explain analyze drop view crm.main.accounts",
    "explain analyze create table crm.main.x as select 1",
    "explain analyze create table memory.main.x as select 1",
    "explain analyze create macro crm.main.m(a) as a + 1",
])
def test_a_key_only_reads(hub, sql):
    key = make_key(hub, ["crm"])
    r = hub.post("/api/data/query", json={"sql": sql, "projects": ["crm"]}, headers=bearer(key))
    assert r.status_code == 400, r.text
    # and the shared connection still answers
    ok = hub.post("/api/data/query", json={"sql": "select count(*) from crm.accounts"}, headers=bearer(key))
    assert ok.json()["rows"] == [[2]]


def test_explain_still_answers_and_a_limit_is_never_negative(hub):
    key = make_key(hub, ["crm"])
    plan = hub.post("/api/data/query", json={"sql": "explain analyze select * from crm.accounts"}, headers=bearer(key))
    assert plan.status_code == 200 and plan.json()["rows"]
    assert hub.post("/api/data/query", json={"sql": "select 1", "limit": -1}, headers=bearer(key)).status_code == 422


def test_a_project_is_named_by_a_word_of_its_own():
    from exuvia.dataapi import named_in

    projects = ("crm", "main", "sales-eu")
    assert named_in("select domain from crm.accounts", projects) == ["crm"]  # `domain` is not `main`
    assert named_in('select * from "sales-eu".orders join CRM.accounts using (id)', projects) == ["crm", "sales-eu"]
    assert named_in("select * from crm.main.accounts", projects) == ["crm"]  # DuckDB's own catalog, not the project
    assert named_in("select 1 as scrm", projects) == []


def test_a_key_reads_only_its_projects(hub):
    key = make_key(hub, ["sales"])
    assert hub.get("/api/data/projects/crm/nodes/accounts", headers=bearer(key)).status_code == 404
    r = hub.post("/api/data/query", json={"sql": "select * from crm.accounts"}, headers=bearer(key))
    assert r.status_code == 400 and "crm" in r.json()["detail"]
    r = hub.post("/api/data/query", json={"sql": "select 1", "projects": ["crm"]}, headers=bearer(key))
    assert r.status_code == 404
    # Nor is a data key anything outside the data API.
    assert hub.get("/p/sales/api/project", headers=bearer(key)).status_code == 401
    assert hub.get("/api/projects", headers=bearer(key)).status_code == 401


def test_keys_are_needed_and_checked(hub):
    assert hub.get("/api/data/projects").status_code == 401
    assert hub.get("/api/data/projects", headers=ALICE).status_code == 401  # signed in is not a key
    assert hub.get("/api/data/projects", headers={"Authorization": "Bearer exd_" + "x" * 40}).status_code == 401
    token = hub.post("/api/me/tokens", json={"name": "agent"}, headers=ALICE).json()["token"]
    assert hub.get("/api/data/projects", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    # An agent token can't make data keys, nor can anyone for a project they can't open.
    r = hub.post("/api/data-keys", json={"projects": ["crm"]}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert hub.post("/api/data-keys", json={"projects": ["secret"]}, headers=ALICE).status_code == 403
    assert hub.post("/api/data-keys", json={"projects": ["nope"]}, headers=ALICE).status_code == 404
    assert hub.post("/api/data-keys", json={"projects": []}, headers=ALICE).status_code == 400


def test_a_key_never_reads_more_than_its_maker(hub):
    key = make_key(hub, ["sales", "crm"], as_=BOB)
    assert len(hub.get("/api/data/projects", headers=bearer(key)).json()["projects"]) == 2
    hub.patch("/api/projects/crm/sharing", json={"general": "private"}, headers=ALICE)
    assert [p["id"] for p in hub.get("/api/data/projects", headers=bearer(key)).json()["projects"]] == ["sales"]
    bob = hub.get("/api/me", headers=BOB).json()["user"]["uid"]
    hub.patch(f"/api/users/{bob}", json={"disabled": True}, headers=ROOT)
    hub.app.state.hub.users._seen.clear()  # as if SEEN_EVERY had passed
    assert hub.get("/api/data/projects", headers=bearer(key)).status_code == 403


def test_keys_are_renamed_repointed_revoked_and_lose_deleted_projects(hub):
    key = make_key(hub, ["sales"])
    r = hub.patch(f"/api/data-keys/{key['id']}", json={"name": "bi", "projects": ["sales", "crm"]}, headers=ALICE)
    assert (r.json()["name"], r.json()["projects"]) == ("bi", ["crm", "sales"])
    assert len(hub.get("/api/data/projects", headers=bearer(key)).json()["projects"]) == 2
    # Only its maker (or an admin) sees and changes it.
    assert hub.delete(f"/api/data-keys/{key['id']}", headers=BOB).status_code == 404
    assert hub.get("/api/data-keys?all=true", headers=BOB).status_code == 403
    assert [k["id"] for k in hub.get("/api/data-keys?all=true", headers=ROOT).json()["keys"]] == [key["id"]]

    assert hub.delete("/api/projects/crm", headers=ALICE).status_code == 200
    assert hub.get("/api/data-keys", headers=ALICE).json()["keys"][0]["projects"] == ["sales"]
    project(hub, "crm", "accounts", ACCOUNTS, as_=BOB)  # a new project of the same name
    assert [p["id"] for p in hub.get("/api/data/projects", headers=bearer(key)).json()["projects"]] == ["sales"]

    assert hub.delete(f"/api/data-keys/{key['id']}", headers=ALICE).status_code == 200
    assert hub.get("/api/data/projects", headers=bearer(key)).status_code == 401


def test_an_admin_sees_how_much_each_key_is_used(hub):
    """Each request with a key counts by day (datakeys.py, usage): its
    queries and its failures too, and a request with an unknown key counts
    as turned away. Only an admin reads the counts."""
    used, idle = make_key(hub, ["sales", "crm"]), make_key(hub, ["crm"], as_=BOB, name="spare")
    assert hub.get("/api/data/projects", headers=bearer(used)).status_code == 200
    assert hub.post("/api/data/query", json={"sql": JOIN}, headers=bearer(used)).status_code == 200
    assert hub.post("/api/data/query", json={"sql": "select * from nope.t"}, headers=bearer(used)).status_code == 400
    assert hub.get("/api/data/projects/crm/nodes/nope", headers=bearer(used)).status_code == 404
    assert hub.get("/api/data/projects", headers={"Authorization": "Bearer exd_" + "x" * 40}).status_code == 401

    assert hub.get("/api/data-keys/usage", headers=ALICE).status_code == 403
    r = hub.get("/api/data-keys/usage", headers=ROOT).json()
    keys = {k["id"]: k for k in r["keys"]}
    assert keys[used["id"]]["usage"] == {"today": 4, "week": 4, "requests": 4, "queries": 2, "errors": 2}
    assert keys[used["id"]]["owner"]["name"] == "alice" and keys[used["id"]]["daily"][0]["requests"] == 4
    assert keys[idle["id"]]["usage"]["requests"] == 0 and keys[idle["id"]]["daily"] == []
    assert keys[idle["id"]]["owner"]["name"] == "bob"
    assert r["turned_away"]["requests"] == 1
    assert len(r["daily"]) == 30 and r["daily"][-1] == {"day": r["daily"][-1]["day"], "requests": 4, "queries": 2, "errors": 2}
    assert sum(d["requests"] for d in r["daily"][:-1]) == 0

    # Written to the database added to what is there, and kept when the hub closes.
    store = hub.app.state.hub.data_keys
    store.note(used["id"])
    store.flush()
    store.note(used["id"], query=True)
    assert store.usage()["keys"][used["id"]][-1] == {"day": r["daily"][-1]["day"], "requests": 6, "queries": 3, "errors": 2}


def test_usage_is_counted_by_day_and_old_days_are_dropped(tmp_path):
    from exuvia.datakeys import KEEP_DAYS, DataKeyStore
    from exuvia.db import SQLiteDatabase

    store = DataKeyStore(SQLiteDatabase(tmp_path / "k.db"))
    now = 1_790_000_000.0
    store.note("k1", now=now - (KEEP_DAYS + 5) * 86400)
    store.note("k1", now=now - 86400, ok=False)
    store.note("k1", now=now, query=True)
    store.flush(now)
    got = store.usage(days=7, now=now)
    assert got["days"][-1] == "2026-09-21" and len(got["days"]) == 7
    assert got["keys"]["k1"] == [
        {"day": "2026-09-20", "requests": 1, "queries": 0, "errors": 1},
        {"day": "2026-09-21", "requests": 1, "queries": 1, "errors": 0},
    ]
    assert len(store._q("select * from data_key_usage")) == 2  # the day past KEEP_DAYS is gone


def test_keys_travel_with_a_hub_export(hub, tmp_path):
    key = make_key(hub, ["sales"])
    blob = hub.get("/api/hub/export", headers=ROOT).content
    with TestClient(create_hub_app(tmp_path / "elsewhere")) as fresh:
        assert fresh.post("/api/hub/import", content=blob, headers=ROOT).status_code == 200
        got = fresh.get("/api/data/projects/sales/nodes/orders", headers=bearer(key))
        assert got.status_code == 200 and got.json()["total"] == 3


def test_a_hub_nobody_signs_in_to_still_needs_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_AUTH", "token")
    with TestClient(create_hub_app(tmp_path / "hub")) as client:
        project(client, "crm", "accounts", ACCOUNTS, as_={})
        assert client.get("/api/data/projects").status_code == 401
        key = make_key(client, ["crm"], as_={})
        assert key["owner_id"] is None
        r = client.post("/api/data/query", json={"sql": "select name from crm.accounts order by 1"}, headers=bearer(key))
        assert r.json()["rows"] == [["Acme"], ["Globex"]]


def test_the_data_api_publishes_its_own_spec(hub):
    spec = hub.get("/api/data/openapi.json").json()  # no key needed: an agent reads it first
    assert spec["info"]["title"] == "exuvia data API"
    assert sorted(spec["paths"]) == [
        "/api/data/projects", "/api/data/projects/{project}", "/api/data/projects/{project}/nodes/{node}", "/api/data/query",
    ]
    assert spec["components"]["securitySchemes"]["dataKey"] == {
        "type": "http", "scheme": "bearer", "bearerFormat": "exd_...",
        "description": "A data API key, made in the hub's account menu (Data API keys).",
    }
    assert spec["security"] == [{"dataKey": []}] and spec["servers"] == [{"url": "http://testserver"}]
    # `servers` is the address asked; nothing is kept for each one (anyone can send any Host).
    other = hub.get("/api/data/openapi.json", headers={"host": "hub.example"}).json()
    assert other["servers"] == [{"url": "http://hub.example"}] and other["paths"] == spec["paths"]
    assert hub.get("/api/data/openapi.json").json()["servers"] == [{"url": "http://testserver"}]
    query = spec["paths"]["/api/data/query"]["post"]
    ok = query["responses"]["200"]["content"]
    assert ok["application/json"]["schema"] == {"$ref": "#/components/schemas/QueryAnswer"}
    assert "application/vnd.apache.arrow.stream" in ok
    assert {"400", "401", "403", "404"} <= set(query["responses"])
    node = spec["components"]["schemas"]["DataNode"]["properties"]
    assert {"kind", "columns", "parts", "history", "artifact_id"} <= set(node)
    # The answers match the models they are documented with.
    from exuvia.dataapi import DataProjects, QueryAnswer

    key = make_key(hub, ["sales", "crm"])
    DataProjects.model_validate(hub.get("/api/data/projects", headers=bearer(key)).json())
    QueryAnswer.model_validate(hub.post("/api/data/query", json={"sql": JOIN}, headers=bearer(key)).json())


STATS = '''import exuvia


@exuvia.node()
def stats():
    return {"mean": float("nan"), "n": 0, "range": [float("-inf"), float("inf")]}
'''


def test_a_json_output_with_nan_is_served_as_null(hub):
    """JSON has no NaN or Infinity: they are written as null, so both routes
    that serve the output answer instead of failing to render it."""
    project(hub, "lab", "stats", STATS)
    want = {"mean": None, "n": 0, "range": [None, None]}
    r = hub.get("/p/lab/api/nodes/stats/data", headers=ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["value"] == want
    r = hub.get("/api/data/projects/lab/nodes/stats", headers=bearer(make_key(hub, ["lab"])))
    assert r.status_code == 200, r.text
    assert r.json()["value"] == want


class FakeWorkspace:
    """Just what QueryConnections reads of a workspace: its version and views."""

    def __init__(self):
        from exuvia.events import EventBus

        self.bus = EventBus()
        self.built = 0

    def add_output_views(self, con, params, only=None, catalog=None):
        import time

        self.built += 1
        time.sleep(0.2)  # long enough for every request below to miss together
        con.execute(f'attach \':memory:\' as "{catalog}"')
        con.execute(f'create view "{catalog}".main.t as select 1 as x')
        return []


def test_requests_that_miss_together_wait_for_one_build():
    """A run finishing invalidates the connection for every request at once:
    they wait for one build instead of each making their own."""
    from concurrent.futures import ThreadPoolExecutor

    conns, ws = dataapi.QueryConnections(), FakeWorkspace()

    def query(_):
        cur = conns.cursor({"sales": ws})
        try:
            return cur.execute("select x from sales.t").fetchall()
        finally:
            cur.close()

    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(query, range(8))) == [[(1,)]] * 8
    assert ws.built == 1 and conns.builds == 1

    ws.bus.touch()  # the project changed: the next requests build once more
    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(query, range(8))) == [[(1,)]] * 8
    assert ws.built == 2 and conns.builds == 2


def test_a_listing_is_kept_until_its_project_changes(hub, monkeypatch):
    key = make_key(hub, ["crm"])
    calls = []
    real = dataapi.describe
    monkeypatch.setattr(dataapi, "describe", lambda ws: calls.append(1) or real(ws))
    for _ in range(3):
        assert hub.get("/api/data/projects", headers=bearer(key)).status_code == 200
    assert len(calls) == 1
    hub.app.state.hub._ctxs["crm"].ws.bus.touch()
    got = hub.get("/api/data/projects/crm", headers=bearer(key)).json()
    assert len(calls) == 2 and [n["name"] for n in got["nodes"]] == ["accounts"]


def test_node_reads_share_one_database_and_keep_their_temp_tables_apart():
    """outputs.reader: a cursor each, on one database; a temp table one
    cursor makes (a partitioned read's filter) is not another's."""
    from exuvia import outputs

    a, b = outputs.reader(), outputs.reader()
    a.execute("create temp table __keep (v varchar)")
    a.execute("insert into __keep values ('a')")
    b.execute("create temp table __keep (v varchar)")
    assert a.execute("select count(*) from __keep").fetchone() == (1,)
    assert b.execute("select count(*) from __keep").fetchone() == (0,)
    assert a.execute("select current_setting('threads')").fetchone() == b.execute("select current_setting('threads')").fetchone()
