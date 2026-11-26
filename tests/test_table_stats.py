"""A table output's overview, read from its Parquet footers alone
(tablestats.py, GET /api/nodes/{name}/stats)."""

import textwrap
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from exuvia.server import create_app
from exuvia.tablestats import parquet_stats


def write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def test_footers_sum_over_row_groups_and_files(tmp_path):
    a = pa.table({
        "n": [3, None, 1, 7],
        "s": ["b", "a", None, None],
        "d": pd.to_datetime(["2024-03-01", "2024-01-01", None, "2024-02-01"]),
        "l": [[1], [2, 3], None, []],
        "raw": [b"x", b"y", b"z", None],
    })
    pq.write_table(a, tmp_path / "a.parquet", row_group_size=2)
    # A second file without "s": its rows are nulls there, as SQL reads them.
    pq.write_table(pa.table({"n": [-5, 2]}), tmp_path / "b.parquet")
    out = parquet_stats([str(tmp_path / "a.parquet"), str(tmp_path / "b.parquet")])
    assert (out["rows"], out["files"], out["row_groups"]) == (6, 2, 3)
    assert out["bytes"] == (tmp_path / "a.parquet").stat().st_size + (tmp_path / "b.parquet").stat().st_size
    cols = out["columns"]
    assert (cols["n"]["nulls"], cols["n"]["min"], cols["n"]["max"]) == (1, -5, 7)
    # The second row group of "s" is all nulls: it has no bounds, and needs none.
    assert (cols["s"]["nulls"], cols["s"]["min"], cols["s"]["max"]) == (4, "a", "b")
    assert (cols["d"]["min"], cols["d"]["max"]) == ("2024-01-01T00:00:00", "2024-03-01T00:00:00")
    # A list's nulls and bounds are its elements', not its rows': not answered.
    assert (cols["l"]["nulls"], cols["l"]["min"], cols["l"]["max"]) == (None, None, None)
    assert cols["l"]["bytes"] > 0
    # Binary bounds are bytes, which mean nothing shown.
    assert (cols["raw"]["nulls"], cols["raw"]["min"]) == (3, None)


def test_route_answers_tables_and_parts(tmp_path):
    write(tmp_path, "nodes/px.py", '''
        import exuvia

        @exuvia.node()
        def px():
            import pandas as pd
            return pd.DataFrame({"t": ["A", "B", None], "v": [1.5, None, -2.0]})
    ''')
    write(tmp_path, "nodes/fit.py", '''
        import exuvia

        @exuvia.node()
        def fit():
            import pandas as pd
            return {"coefs": pd.DataFrame({"k": [1, 2]}), "metrics": {"r2": 0.9}}
    ''')
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/nodes/px/stats").status_code == 404  # not run yet
        for node in ("px", "fit"):
            assert client.post(f"/api/nodes/{node}/run", json={"wait": 60}).json()["ok"]
        got = client.get("/api/nodes/px/stats").json()
        assert got["kind"] == "table" and got["rows"] == 3 and got["row_groups"] >= 1 and got["bytes"] > 0
        cols = {c["name"]: c for c in got["columns"]}
        assert cols["t"]["type"] == "VARCHAR" and (cols["t"]["nulls"], cols["t"]["min"], cols["t"]["max"]) == (1, "A", "B")
        assert cols["v"]["type"] == "DOUBLE" and (cols["v"]["nulls"], cols["v"]["min"], cols["v"]["max"]) == (1, -2.0, 1.5)

        assert client.get("/api/nodes/fit/stats", params={"part": "coefs"}).json()["rows"] == 2
        assert client.get("/api/nodes/fit/stats").status_code == 404  # several outputs: pick one
        assert client.get("/api/nodes/fit/stats", params={"part": "metrics"}).status_code == 404  # JSON
