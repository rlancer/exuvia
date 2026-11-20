"""The binary data routes: Arrow streams from the query and data routes, and
stored output files served byte for byte."""

import hashlib
import json
import textwrap
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from exuvia.arrowstream import MEDIA_TYPE, wants_arrow
from exuvia.server import create_app

ARROW = {"Accept": MEDIA_TYPE}
ROWS = 12_000  # more than the JSON routes will ever answer with

BIG = f'''
    import exuvia

    @exuvia.node()
    def big():
        import pandas as pd
        n = {ROWS}
        return pd.DataFrame({{
            "i": range(n),
            "x": [k / 4 for k in range(n)],
            "label": [f"row{{k}}" for k in range(n)],
            "day": pd.Timestamp("2026-11-06"),
        }})
'''
NOTE = '''
    import exuvia

    @exuvia.node()
    def note():
        return {"summary": "hello", "n": 3}
'''
PRICES = '''
    import exuvia

    @exuvia.node(params=["as_of"])
    def prices():
        import pandas as pd
        day = exuvia.param("as_of")
        return pd.DataFrame({"as_of": [day], "px": [int(day[-2:])]})
'''


def write(root: Path, name: str, body: str) -> None:
    (root / "nodes").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / name).write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def table(res) -> pa.Table:
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == MEDIA_TYPE
    return pa.ipc.open_stream(res.content).read_all()


def meta(t: pa.Table) -> dict:
    return json.loads(t.schema.metadata[b"exuvia"])


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("arrow")
    (root / "exuvia.json").write_text(
        json.dumps({"params": {"as_of": {"type": "date", "default": "2026-11-06"}}}), encoding="utf-8"
    )
    write(root, "big.py", BIG)
    write(root, "note.py", NOTE)
    write(root, "prices.py", PRICES)
    write(root, "empty.sql", "select i from big where i < 0\n")
    with TestClient(create_app(root)) as c:
        c.root = root
        for params in ({}, {"as_of": "2026-11-05"}):
            done = c.post("/api/run", json={"wait": 120, "force": True, "params": params}).json()
            assert done["ok"] and not done["running"], done
        yield c


def node(client: TestClient, name: str) -> dict:
    return next(n for n in client.get("/api/project").json()["nodes"] if n["name"] == name)


def test_accept_header_selects_arrow():
    assert wants_arrow(MEDIA_TYPE)
    assert wants_arrow(f"application/json;q=0.5, {MEDIA_TYPE.upper()}")
    assert not wants_arrow("application/json")
    assert not wants_arrow(None)


def test_query_json_is_unchanged(client):
    res = client.post("/api/query", json={"sql": "select * from big"}).json()
    assert len(res["rows"]) == 500 and res["columns"][0] == {"name": "i", "type": "BIGINT"}
    capped = client.post("/api/query", json={"sql": "select * from big", "limit": 9000}).json()
    assert len(capped["rows"]) == 5000


def test_query_arrow_streams_every_row_with_types(client):
    t = table(client.post("/api/query", json={"sql": "select * from big order by i"}, headers=ARROW))
    assert t.num_rows == ROWS
    assert t.schema.field("i").type == pa.int64() and t.schema.field("x").type == pa.float64()
    assert pa.types.is_timestamp(t.schema.field("day").type)
    assert t.column("i").to_pylist()[-1] == ROWS - 1 and t.column("label").to_pylist()[1] == "row1"
    assert [c["name"] for c in meta(t)["columns"]] == ["i", "x", "label", "day"]
    assert meta(t)["columns"][0]["type"] == "BIGINT"


def test_query_arrow_limit_params_and_history_view(client):
    assert table(client.post("/api/query", json={"sql": "select * from big", "limit": 7}, headers=ARROW)).num_rows == 7
    past = {"sql": "select px from prices", "params": {"as_of": "2026-11-05"}}
    assert table(client.post("/api/query", json=past, headers=ARROW)).column("px").to_pylist() == [5]
    runs = table(client.post("/api/query", json={"sql": "select _as_of, px from prices__runs order by 1"}, headers=ARROW))
    assert runs.to_pydict() == {"_as_of": ["2026-11-05", "2026-11-06"], "px": [5, 6]}


def test_query_arrow_keeps_what_json_cannot(client):
    sql = "select 'nan'::double as bad, 9007199254740993::bigint as wide, 1.25::decimal(10,2) as exact"
    t = table(client.post("/api/query", json={"sql": sql}, headers=ARROW))
    assert t.schema.field("exact").type == pa.decimal128(10, 2)
    row = t.to_pylist()[0]
    assert row["bad"] != row["bad"] and row["wide"] == 9007199254740993 and str(row["exact"]) == "1.25"
    as_json = client.post("/api/query", json={"sql": sql}).json()["rows"][0]
    assert as_json[0] is None  # the JSON route has no way to say NaN


def test_query_arrow_empty_and_failing_statements(client):
    t = table(client.post("/api/query", json={"sql": "select i, label from big where i < 0"}, headers=ARROW))
    assert t.num_rows == 0 and t.schema.names == ["i", "label"]
    for sql in ("select * from nope", "select 'x'::int from big"):  # unknown table; fails while running
        bad = client.post("/api/query", json={"sql": sql}, headers=ARROW)
        assert bad.status_code == 400 and bad.headers["content-type"].startswith("application/json"), sql
        assert bad.json()["detail"]


def test_node_data_as_arrow(client):
    t = table(client.get("/api/nodes/big/data", headers=ARROW))
    assert t.num_rows == ROWS
    m = meta(t)
    assert m["kind"] == "table" and m["total"] == ROWS and m["run_params"] == {}
    assert m["run_id"] == node(client, "big")["last_run"]["id"]

    page = table(client.get("/api/nodes/big/data?limit=3&offset=10", headers=ARROW))
    assert page.column("i").to_pylist() == [10, 11, 12] and meta(page)["total"] == ROWS
    rest = table(client.get(f"/api/nodes/big/data?offset={ROWS - 2}", headers=ARROW))
    assert rest.column("i").to_pylist() == [ROWS - 2, ROWS - 1]

    assert table(client.get("/api/nodes/empty/data", headers=ARROW)).num_rows == 0

    as_json = client.get("/api/nodes/big/data").json()
    assert as_json["kind"] == "table" and len(as_json["rows"]) == 200 and as_json["total"] == ROWS
    assert len(client.get("/api/nodes/big/data?limit=9000").json()["rows"]) == 5000


def test_node_data_arrow_follows_params_and_run_id(client):
    past = table(client.get("/api/nodes/prices/data", params={"params": '{"as_of": "2026-11-05"}'}, headers=ARROW))
    assert past.column("px").to_pylist() == [5] and meta(past)["run_params"] == {"as_of": "2026-11-05"}
    now = table(client.get("/api/nodes/prices/data", headers=ARROW))
    assert now.column("px").to_pylist() == [6]
    again = table(client.get(f"/api/nodes/prices/data?run_id={meta(past)['run_id']}", headers=ARROW))
    assert again.column("px").to_pylist() == [5]


def test_node_data_that_is_not_a_table_stays_json(client):
    res = client.get("/api/nodes/note/data", headers=ARROW)
    assert res.headers["content-type"].startswith("application/json")
    assert res.json()["kind"] == "json" and res.json()["value"] == {"summary": "hello", "n": 3}
    assert client.get("/api/nodes/nope/data", headers=ARROW).status_code == 404


def test_artifact_route_serves_the_stored_file(client):
    big = node(client, "big")
    url = f"/api/artifacts/big/{big['artifact_id']}.parquet"
    res = client.get(url)
    assert res.status_code == 200 and res.headers["content-type"] == "application/vnd.apache.parquet"
    assert hashlib.sha256(res.content).hexdigest()[:16] == big["artifact_id"]
    assert res.content == (client.root / ".exuvia" / "artifacts" / "big" / f"{big['artifact_id']}.parquet").read_bytes()
    assert "immutable" in res.headers["cache-control"] and res.headers["etag"] == f'"{big["artifact_id"]}"'
    assert res.headers["accept-ranges"] == "bytes"

    head = client.head(url)
    assert head.status_code == 200 and int(head.headers["content-length"]) == len(res.content) and not head.content

    # A Parquet reader fetches the footer first, then the column chunks it needs.
    tail = client.get(url, headers={"Range": "bytes=-8"})
    assert tail.status_code == 206 and tail.content == res.content[-8:] and tail.content.endswith(b"PAR1")
    assert tail.headers["content-range"] == f"bytes {len(res.content) - 8}-{len(res.content) - 1}/{len(res.content)}"
    part = client.get(url, headers={"Range": "bytes=4-19"})
    assert part.status_code == 206 and part.content == res.content[4:20]

    note = node(client, "note")
    as_json = client.get(f"/api/artifacts/note/{note['artifact_id']}.json")
    assert as_json.headers["content-type"].startswith("application/json")
    assert as_json.json() == {"summary": "hello", "n": 3}


def test_artifact_route_serves_past_outputs(client):
    history = client.get("/api/nodes/prices/history").json()
    assert len(history) == 2 and len({h["artifact_id"] for h in history}) == 2
    for h in history:
        res = client.get(f"/api/artifacts/prices/{h['artifact_id']}.parquet")
        got = pq.read_table(pa.BufferReader(res.content)).to_pydict()
        assert got["as_of"] == [h["run_params"]["as_of"]]


def test_artifact_route_refuses_what_it_does_not_have(client):
    big, note = node(client, "big"), node(client, "note")
    for path in (
        "big/0000000000000000.parquet",  # no such output
        f"note/{big['artifact_id']}.parquet",  # another node's output
        f"big/{big['artifact_id']}.json",  # wrong kind
        f"note/{note['artifact_id']}.parquet",
        f"big/{big['artifact_id']}",  # no extension
        f"big/{big['artifact_id']}.tmp",
        "big/.parquet",
        "nope/none.parquet",
    ):
        assert client.get(f"/api/artifacts/{path}").status_code == 404, path
