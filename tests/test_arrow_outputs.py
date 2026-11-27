"""A node that returns a pyarrow table (or a polars frame) is stored as Arrow,
not through pandas, so its column types are what every reader sees: pandas
has no plain nullable integer, and would have stored an int16 column with a
null as float64."""

import datetime as dt
import io
from decimal import Decimal

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from exuvia import runner
from exuvia.pushing import encode, save_pushed
from exuvia.workspace import Workspace

from test_engine import py, run

TYPED = """return pa.table({
        "small": pa.array([1, None], pa.int16()),
        "amount": pa.array([Decimal("12.34"), None], pa.decimal128(10, 2)),
        "ts": pa.array([dt.datetime(2026, 11, 27, 9, 30, tzinfo=dt.timezone.utc), None], pa.timestamp("us", "UTC")),
        "tags": pa.array([["a", "b"], []], pa.list_(pa.string())),
    })"""


def typed_node(root, name="typed"):
    py(root, name, body="import datetime as dt\n    from decimal import Decimal\n    import pyarrow as pa\n    " + TYPED)


def test_a_returned_pyarrow_table_keeps_its_column_types(tmp_path):
    typed_node(tmp_path)
    py(tmp_path, "reads", params="typed", body="return typed")  # a pandas node downstream still gets a DataFrame
    (tmp_path / "nodes" / "summed.sql").write_text("select small, amount from typed where small is null", encoding="utf-8")
    ws = Workspace(tmp_path)
    finals = run(ws, ["typed", "reads", "summed"])
    assert {n: r["status"] for n, r in finals.items()} == {"typed": "success", "reads": "success", "summed": "success"}

    [path] = (tmp_path / ".exuvia" / "artifacts" / "typed").glob("*.parquet")
    schema = pq.read_schema(path)
    assert schema.field("small").type == pa.int16()
    assert schema.field("amount").type == pa.decimal128(10, 2)
    assert schema.field("ts").type == pa.timestamp("us", "UTC")
    assert schema.field("tags").type == pa.list_(pa.string())
    t = pq.read_table(path)
    assert t.column("small").to_pylist() == [1, None]
    assert t.column("amount").to_pylist() == [Decimal("12.34"), None]

    # The run records the Arrow types, and DuckDB (previews, SQL, the data API) reads them back.
    assert finals["typed"]["rows"] == 2
    assert ws.status()["typed"]["schema"][:2] == [
        {"name": "small", "type": "int16"}, {"name": "amount", "type": "decimal128(10, 2)"},
    ]
    cols = {c["name"]: c["type"] for c in ws.preview("typed")["columns"]}
    assert cols["small"] == "SMALLINT" and cols["amount"] == "DECIMAL(10,2)"
    assert cols["ts"] == "TIMESTAMP WITH TIME ZONE" and cols["tags"] == "VARCHAR[]"
    # and so does a SQL node downstream
    assert ws.status()["summed"]["schema"] == [{"name": "small", "type": "SMALLINT"}, {"name": "amount", "type": "DECIMAL(10,2)"}]


def test_the_same_table_through_save_artifact_and_as_a_part(tmp_path):
    table = pa.table({"small": pa.array([1, None], pa.int16()), "amount": pa.array([Decimal("1.50"), None], pa.decimal128(10, 2))})
    out = runner.save_artifact(table, tmp_path / "one")
    assert out["kind"] == "table" and out["rows"] == 2
    assert pq.read_schema(out["artifact_path"]).field("small").type == pa.int16()
    assert runner.save_artifact(table, tmp_path / "again")["artifact_id"] == out["artifact_id"]  # same bytes

    multi = runner.save_artifact({"t": table, "meta": {"n": 2}}, tmp_path / "multi")
    assert multi["schema"][0] == {
        "name": "t", "type": "table", "rows": 2,
        "columns": [{"name": "small", "type": "int16"}, {"name": "amount", "type": "decimal128(10, 2)"}],
    }
    import zipfile

    with zipfile.ZipFile(multi["artifact_path"]) as z:
        part = pq.read_table(io.BytesIO(z.read("t.parquet")))
    assert part.schema.field("amount").type == pa.decimal128(10, 2) and part.column("small").to_pylist() == [1, None]

    # A record batch and a lone array are tables too; the array is column "0", as pandas names it.
    batch = runner.save_artifact(table.to_batches()[0], tmp_path / "batch")
    assert pq.read_schema(batch["artifact_path"]).field("small").type == pa.int16()
    arr = runner.save_artifact(pa.array([1, None], pa.int16()), tmp_path / "arr")
    assert arr["schema"] == [{"name": "0", "type": "int16"}]


def test_pandas_frames_are_stored_as_before(tmp_path):
    # pandas still has no nullable int in a plain frame: what it holds is what is stored.
    out = runner.save_artifact(pd.DataFrame({"x": [1, None]}), tmp_path / "pd")
    assert out["schema"] == [{"name": "x", "type": "float64"}]
    # An index becomes a column; the frame returned is left as it was.
    df = pd.DataFrame({0: [1, 2]}, index=pd.Index(["a", "b"], name="k"))
    out = runner.save_artifact(df, tmp_path / "idx")
    assert pq.read_table(out["artifact_path"]).column_names == ["k", "0"]
    assert list(df.columns) == [0] and df.index.name == "k"
    # A pyarrow table made from such a frame goes through pandas, so its index is a column the same way.
    from_pd = runner.save_artifact(pa.Table.from_pandas(df), tmp_path / "from_pd")
    assert from_pd["artifact_id"] == out["artifact_id"]


def test_duplicate_column_names_are_refused(tmp_path):
    t = pa.table([pa.array([1]), pa.array([2])], names=["a", "a"])
    with pytest.raises(ValueError, match="Duplicate column names"):
        runner.save_artifact(t, tmp_path)


def test_a_pushed_pyarrow_table_keeps_its_types(tmp_path):
    table = pa.table({"small": pa.array([1, None], pa.int16()), "amount": pa.array([Decimal("2.25"), None], pa.decimal128(10, 2))})
    data, fmt = encode(table)
    assert fmt == "parquet"
    upload = tmp_path / "upload.parquet"
    upload.write_bytes(data)
    out = save_pushed(upload, "parquet", tmp_path / "out")
    schema = pq.read_schema(out["artifact_path"])
    assert schema.field("small").type == pa.int16() and schema.field("amount").type == pa.decimal128(10, 2)
    # A parquet file pandas wrote is stored as it was before, index and all.
    upload.write_bytes(encode(pd.DataFrame({"x": [1, 2]}, index=pd.Index([5, 6], name="k")))[0])
    pushed = save_pushed(upload, "parquet", tmp_path / "pd")
    assert pq.read_table(pushed["artifact_path"]).column_names == ["k", "x"]


def test_a_polars_frame_keeps_its_types(tmp_path):
    pl = pytest.importorskip("polars")
    frame = pl.DataFrame({
        "small": pl.Series([1, None], dtype=pl.Int16),
        "amount": pl.Series([Decimal("1.20"), None], dtype=pl.Decimal(10, 2)),
        "when": [dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), None],
    })
    out = runner.save_artifact(frame, tmp_path / "frame")
    schema = pq.read_schema(out["artifact_path"])
    assert schema.field("small").type == pa.int16()
    assert schema.field("amount").type == pa.decimal128(10, 2)
    assert schema.field("when").type == pa.timestamp("us", "UTC")
    series = runner.save_artifact(pl.Series("n", [1, None], dtype=pl.Int16), tmp_path / "series")
    assert series["schema"] == [{"name": "n", "type": "int16"}]
