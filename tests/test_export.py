"""Downloads: node outputs and SQL results as CSV, Excel, Parquet or JSON,
and the project's code as a zip (exuvia/exports.py)."""

import asyncio
import csv
import io
import json
import re
import textwrap
import zipfile
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from exuvia import exports
from exuvia.server import _needed_role, create_app
from exuvia.workspace import Workspace


def write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def run(ws, *a, **k):
    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        res = await ws.run(*a, **k)
        for _ in range(100):
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.gather(*pending)
        return res

    return asyncio.run(go())


PRICES = '''
import exuvia
from lib.fmt import label

@exuvia.node()
def prices():
    import datetime as dt
    import pandas as pd
    return pd.DataFrame({
        "ticker": ["AAA", "=SUM(A1)", label("c")],
        "close": [10.5, float("nan"), 3.0],
        "volume": [100, 200, 2**60],
        "day": [dt.date(2026, 1, 2), dt.date(2026, 1, 3), dt.date(2026, 1, 4)],
        "up": [True, False, True],
        "tags": [["a"], [], ["b", "c"]],
    })
'''

FIT = '''
import exuvia

@exuvia.node()
def fit():
    import pandas as pd
    return {"preds": pd.DataFrame({"x": [1, 2]}), "metrics": {"r2": 0.9}}
'''

META = '''
import exuvia

@exuvia.node()
def meta():
    return {"window": 20}
'''


@pytest.fixture()
def client(tmp_path):
    write(tmp_path, "exuvia.json", json.dumps({"name": "My Research", "params": {"as_of": {"type": "date", "default": "2026-03-02"}}}))
    write(tmp_path, "pyproject.toml", '[project]\nname = "my-research"\n')
    write(tmp_path, ".env", "SECRET=hunter2\n")
    write(tmp_path, "lib/fmt.py", "def label(x):\n    return x.upper()\n")
    write(tmp_path, "lib/unused.py", "X = 1\n")
    write(tmp_path, "nodes/prices.py", PRICES)
    write(tmp_path, "nodes/fit.py", FIT)
    write(tmp_path, "nodes/meta.py", META)
    write(tmp_path, "nodes/day.sql", "-- params: as_of\nselect $as_of as d, 1 as n\n")
    write(tmp_path, "nodes/never.sql", "select 1 as x\n")
    write(tmp_path, "views/board/view.json", json.dumps({"title": "Board", "reads": ["prices"]}))
    write(tmp_path, "views/board/App.tsx", "export default function App() { return null; }\n")
    ws = Workspace(tmp_path)
    run(ws, ["prices", "fit", "meta"])
    run(ws, ["day"], params={"as_of": "2026-03-02"})
    run(ws, ["day"], params={"as_of": "2026-03-03"})
    ws.store.close()
    with TestClient(create_app(tmp_path)) as c:
        yield c


def filename(res) -> str:
    return re.search(r'filename="([^"]+)"', res.headers["content-disposition"]).group(1)


def xlsx_rows(body: bytes) -> list[list[str | None]]:
    """The first sheet's cells as text (inline strings, numbers, booleans), row by row."""
    xml = zipfile.ZipFile(io.BytesIO(body)).read("xl/worksheets/sheet1.xml").decode()
    rows = []
    for row in re.findall(r"<row [^>]*>(.*?)</row>", xml):
        cells: list[str | None] = []
        for ref, inner in re.findall(r'<c r="([A-Z]+)\d+"[^>]*>(.*?)</c>', row):
            col = sum((ord(ch) - 64) * 26**i for i, ch in enumerate(reversed(ref))) - 1
            cells.extend([None] * (col - len(cells)))
            m = re.search(r"<t[^>]*>(.*?)</t>|<v>(.*?)</v>", inner)
            cells.append(m.group(1) if m and m.group(1) is not None else (m.group(2) if m else None))
        rows.append(cells)
    return rows


def test_node_as_csv(client):
    res = client.get("/api/nodes/prices/export?format=csv")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/csv")
    assert filename(res) == "prices.csv"
    rows = list(csv.reader(io.StringIO(res.text)))
    assert rows[0] == ["ticker", "close", "volume", "day", "up", "tags"]
    assert rows[1][:5] == ["AAA", "10.5", "100", "2026-01-02", "true"]
    assert rows[2][1] == ""  # NaN is an empty cell
    assert rows[3][2] == str(2**60)
    assert json.loads(rows[3][5]) == ["b", "c"]  # a list column is JSON text
    assert len(rows) == 4


def test_node_as_parquet_keeps_types(client):
    res = client.get("/api/nodes/prices/export?format=parquet")
    assert res.status_code == 200, res.text
    assert filename(res) == "prices.parquet"
    table = pq.read_table(io.BytesIO(res.content))
    assert table.num_rows == 3
    assert table.column("volume").to_pylist()[2] == 2**60
    assert table.column("tags").to_pylist() == [["a"], [], ["b", "c"]]
    assert b"exuvia" not in (table.schema.metadata or {})


def test_node_as_json(client):
    res = client.get("/api/nodes/prices/export?format=json")
    assert res.status_code == 200, res.text
    rows = res.json()
    assert rows[0] == {"ticker": "AAA", "close": 10.5, "volume": 100, "day": "2026-01-02", "up": True, "tags": ["a"]}
    assert rows[1]["close"] is None


def test_node_as_excel(client):
    res = client.get("/api/nodes/prices/export?format=xlsx")
    assert res.status_code == 200, res.text
    assert filename(res) == "prices.xlsx"
    rows = xlsx_rows(res.content)
    assert rows[0] == ["ticker", "close", "volume", "day", "up", "tags"]
    assert rows[1][:3] == ["AAA", "10.5", "100"]
    assert rows[1][4] == "1"  # a boolean cell
    # Text that looks like a formula stays text; NaN is a blank; a huge int keeps its digits as text.
    assert rows[2][0] == "=SUM(A1)" and rows[2][1] is None
    assert rows[3][2] == str(2**60)
    xml = zipfile.ZipFile(io.BytesIO(res.content)).read("xl/worksheets/sheet1.xml").decode()
    assert "<f>" not in xml
    assert 'ySplit="1"' in xml  # the header row is frozen


def test_excel_refuses_more_rows_than_a_sheet(client, monkeypatch):
    monkeypatch.setattr(exports, "EXCEL_MAX_ROWS", 2)
    res = client.get("/api/nodes/prices/export?format=xlsx")
    assert res.status_code == 413
    assert "CSV or Parquet" in res.json()["detail"]


def test_params_and_run_id_pick_the_output(client):
    res = client.get('/api/nodes/day/export?format=csv&params={"as_of":"2026-03-03"}')
    assert res.status_code == 200, res.text
    assert filename(res) == "day-2026-03-03.csv"
    assert "2026-03-03" in res.text
    runs = client.get("/api/nodes/day/history").json()
    old = next(r for r in runs if r["run_params"]["as_of"] == "2026-03-02")
    res = client.get(f"/api/nodes/day/export?format=csv&run_id={old['run_id']}")
    assert filename(res) == "day-2026-03-02.csv" and "2026-03-02" in res.text


def test_filename_override_is_made_safe(client):
    res = client.get("/api/nodes/prices/export?format=csv&filename=../../etc/my%20prices")
    assert filename(res) == "etc-my-prices.csv"


def test_several_outputs(client):
    res = client.get("/api/nodes/fit/export?format=csv&part=preds")
    assert res.status_code == 200, res.text
    assert filename(res) == "fit.preds.csv"
    assert res.text.splitlines() == ['"x"', "1", "2"]
    res = client.get("/api/nodes/fit/export?format=json&part=metrics")
    assert res.json() == {"r2": 0.9}
    # Without a part: the stored zip of every output, or a 400 that names them.
    res = client.get("/api/nodes/fit/export?format=zip")
    assert res.status_code == 200 and filename(res) == "fit.zip"
    assert sorted(zipfile.ZipFile(io.BytesIO(res.content)).namelist()) == ["metrics.json", "preds.parquet"]
    res = client.get("/api/nodes/fit/export?format=csv")
    assert res.status_code == 400 and "preds, metrics" in res.json()["detail"]
    assert client.get("/api/nodes/fit/export?format=csv&part=nope").status_code == 404


def test_json_output(client):
    res = client.get("/api/nodes/meta/export?format=json")
    assert res.status_code == 200 and res.json() == {"window": 20}
    assert filename(res) == "meta.json"
    res = client.get("/api/nodes/meta/export?format=csv")
    assert res.status_code == 400 and "JSON" in res.json()["detail"]


def test_bad_requests(client):
    assert client.get("/api/nodes/prices/export?format=docx").status_code == 400
    assert client.get("/api/nodes/prices/export?format=zip").status_code == 400
    assert client.get("/api/nodes/nope/export?format=csv").status_code == 404
    assert client.get("/api/nodes/never/export?format=csv").status_code == 404


def test_query_export(client):
    sql = "select ticker, volume from prices order by volume"
    res = client.post("/api/query/export", json={"sql": sql, "format": "csv"})
    assert res.status_code == 200, res.text
    assert filename(res) == "query.csv"
    assert res.text.splitlines()[0] == '"ticker","volume"'
    res = client.post("/api/query/export", json={"sql": sql, "format": "parquet", "filename": "by volume"})
    assert filename(res) == "by-volume.parquet"
    assert pq.read_table(io.BytesIO(res.content)).num_rows == 3
    res = client.post("/api/query/export", json={"sql": "select * from nope", "format": "csv"})
    assert res.status_code == 400
    # A statement with no rows still has its header.
    res = client.post("/api/query/export", json={"sql": "select 1 as a where false", "format": "csv"})
    assert res.text.strip() == '"a"'


def test_view_exports_only_reach_its_reads(client):
    res = client.get("/api/views/board/data/prices/export?format=csv")
    assert res.status_code == 200, res.text
    assert client.get("/api/views/board/data/fit/export?format=csv").status_code == 403
    assert client.get("/api/views/nope/data/prices/export?format=csv").status_code == 404
    res = client.post("/api/views/board/query/export", json={"sql": "select count(*) as n from prices", "format": "json"})
    assert res.status_code == 200 and res.json() == [{"n": 3}]
    assert filename(res) == "board-query.json"
    res = client.post("/api/views/board/query/export", json={"sql": "select * from fit.preds", "format": "csv"})
    assert res.status_code == 400


def test_exports_are_for_viewers():
    class Req:
        def __init__(self, method, path):
            self.method = method
            self.url = type("U", (), {"path": path})()

    assert _needed_role(Req("POST", "/p/x/api/query/export")) == "viewer"
    assert _needed_role(Req("POST", "/p/x/api/views/board/query/export")) == "viewer"
    assert _needed_role(Req("GET", "/p/x/api/nodes/prices/export")) == "viewer"
    assert _needed_role(Req("POST", "/p/x/api/query/exports")) == "editor"


def test_code_archive(client):
    res = client.get("/api/code/archive")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/zip"
    assert filename(res) == "My-Research.zip"
    names = sorted(zipfile.ZipFile(io.BytesIO(res.content)).namelist())
    assert names == sorted(
        f"My-Research/{p}"
        for p in [
            "exuvia.json",
            "pyproject.toml",
            "lib/fmt.py",
            "lib/unused.py",
            "nodes/day.sql",
            "nodes/fit.py",
            "nodes/meta.py",
            "nodes/never.sql",
            "nodes/prices.py",
            "views/board/App.tsx",
            "views/board/view.json",
        ]
    )
    assert not any(".env" in n or ".exuvia" in n for n in names)


def test_code_archive_of_one_node_or_view(client):
    res = client.get("/api/code/archive?node=prices")
    assert filename(res) == "My-Research-prices.zip"
    z = zipfile.ZipFile(io.BytesIO(res.content))
    assert sorted(z.namelist()) == ["My-Research-prices/lib/fmt.py", "My-Research-prices/nodes/prices.py"]
    assert "def prices" in z.read("My-Research-prices/nodes/prices.py").decode()
    res = client.get("/api/code/archive?view=board")
    assert sorted(zipfile.ZipFile(io.BytesIO(res.content)).namelist()) == [
        "My-Research-board/views/board/App.tsx",
        "My-Research-board/views/board/view.json",
    ]
    assert client.get("/api/code/archive?node=nope").status_code == 404
    assert client.get("/api/code/archive?view=nope").status_code == 404
    assert client.get("/api/code/archive?node=prices&view=board").status_code == 400


def test_code_archive_skips_symlinks_out_of_the_project(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("TOKEN = 1\n")
    root = tmp_path / "proj"
    write(root, "nodes/a.sql", "select 1 as x\n")
    (root / "lib").mkdir(parents=True, exist_ok=True)
    (root / "lib" / "leak.py").symlink_to(outside / "secret.py")
    ws = Workspace(root)
    body, _ = exports.code_archive(ws.project, ws.graph)
    names = zipfile.ZipFile(io.BytesIO(body)).namelist()
    assert not any("leak" in n for n in names)
    assert any(n.endswith("nodes/a.sql") for n in names)


def test_disposition_handles_unicode():
    d = exports.disposition("prix-é.csv")
    assert 'filename="prix-_.csv"' in d
    assert "filename*=UTF-8''prix-%C3%A9.csv" in d


@pytest.mark.parametrize("fmt", ["csv", "xlsx", "parquet", "json"])
def test_types_no_format_has_natively_still_export(client, fmt):
    sql = "select interval 90 minute as dur, interval '1 month 2 days' as i, {'a': 1} as s, 'x'::blob as b, now() as ts"
    res = client.post("/api/query/export", json={"sql": sql, "format": fmt})
    assert res.status_code == 200, res.text
    if fmt == "csv":
        row = list(csv.reader(io.StringIO(res.text)))[1]
        assert row[:3] == ["01:30:00", "1 month 2 days", '{"a": 1}']
    if fmt == "parquet":
        assert pq.read_table(io.BytesIO(res.content)).column("dur").to_pylist() == ["01:30:00"]
    if fmt == "json":
        assert res.json()[0]["i"] == "1 month 2 days"


@pytest.mark.parametrize("path", [
    "/api/nodes/prices/data",
    "/api/nodes/prices/data?limit=1&offset=1",
    "/api/nodes/fit/data",
    "/api/nodes/fit/data?part=preds",
    "/api/nodes/fit/data?part=metrics",
    "/api/nodes/meta/data",
    '/api/nodes/day/data?params={"as_of":"2026-03-03"}',
    "/api/nodes/never/data",
])
def test_data_answers_are_the_same_bytes_as_jsonable_encoder(client, path):
    """The data and query routes write their answers with FastJSON (fast,
    no jsonable_encoder walk): the bytes must be what FastAPI's own
    encoding would have sent."""
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse
    from urllib.parse import parse_qsl, urlsplit

    ws = client.app.state.ws
    url = urlsplit(path)
    q = dict(parse_qsl(url.query))
    name = url.path.split("/")[3]
    params = ws.resolve_params(json.loads(q["params"]) if "params" in q else None)
    want = ws.preview(name, limit=int(q.get("limit", 200)), offset=int(q.get("offset", 0)), params=params, part=q.get("part"))
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert r.content == JSONResponse(jsonable_encoder(want)).body


@pytest.mark.parametrize("sql", ["select * from prices", "select * from day__runs order by all", "select 1.5 as x, null as y, 'é' as z"])
def test_query_answers_are_the_same_bytes_as_jsonable_encoder(client, sql):
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse

    want = client.app.state.ws.query(sql, limit=500)
    r = client.post("/api/query", json={"sql": sql})
    assert r.status_code == 200, r.text
    assert r.content == JSONResponse(jsonable_encoder(want)).body
