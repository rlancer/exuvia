"""SQL nodes run entirely in DuckDB: inputs are read from their Parquet files
and the result is written with COPY, so no column goes through pandas and
every type is kept exactly."""

import asyncio
import json
import textwrap
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from exuvia import runner
from exuvia.workspace import Workspace

TYPES_SQL = """
select
    date '2026-03-02' as d,
    123.4567::decimal(18, 4) as amount,
    i::integer as n,
    case when i = 2 then null else i::integer end as maybe,
    timestamptz '2026-03-02 10:00:00+00' as at,
    [i::integer, i::integer + 1] as xs,
    {'k': 'v' || i, 'w': i::integer} as s
from range(3) t(i)
order by i
"""

TYPES_SCHEMA = [
    {"name": "d", "type": "DATE"},
    {"name": "amount", "type": "DECIMAL(18,4)"},
    {"name": "n", "type": "INTEGER"},
    {"name": "maybe", "type": "INTEGER"},
    {"name": "at", "type": "TIMESTAMP WITH TIME ZONE"},
    {"name": "xs", "type": "INTEGER[]"},
    {"name": "s", "type": "STRUCT(k VARCHAR, w INTEGER)"},
]


def run_sql(tmp_path: Path, sql: str, inputs=None, params=None, partition_by=None, partitions=None, monkeypatch=None):
    """Run one SQL node through the runner, in this process."""
    (tmp_path / "nodes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "nodes" / "q.sql").write_text(textwrap.dedent(sql), encoding="utf-8")
    spec = {
        "root": str(tmp_path), "node": "q", "kind": "sql", "path": "nodes/q.sql",
        "inputs": inputs or {}, "params": params or {}, "out_dir": str(tmp_path / "out"),
        "partition_by": partition_by, "partitions": partitions or [],
    }
    return runner.execute(spec)


@pytest.fixture(autouse=True)
def _keep_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # runner.execute changes directory; put it back after


def parquet(path: Path, sql: str) -> str:
    duckdb.execute(f"copy ({sql}) to '{path.as_posix()}' (format parquet)")
    return str(path)


def test_types_are_kept_exactly(tmp_path):
    res = run_sql(tmp_path, TYPES_SQL)
    assert res["kind"] == "table" and res["rows"] == 3
    assert res["schema"] == TYPES_SCHEMA
    t = pq.read_table(res["artifact_path"])
    assert t.schema.field("d").type == pa.date32()
    assert t.schema.field("amount").type == pa.decimal128(18, 4)
    assert t.schema.field("n").type == pa.int32()
    assert t.schema.field("maybe").type == pa.int32()  # not float64 for the null
    assert t.schema.field("at").type == pa.timestamp("us", tz="UTC")
    assert pa.types.is_list(t.schema.field("xs").type) and pa.types.is_struct(t.schema.field("s").type)
    assert t.column("maybe").to_pylist() == [0, 1, None]
    assert str(t.column("amount")[0]) == "123.4567"
    # Named by its content, the way every output is.
    assert Path(res["artifact_path"]).name == f"{res['artifact_id']}.parquet"
    assert [p.name for p in (tmp_path / "out").iterdir()] == [Path(res["artifact_path"]).name]


def test_same_rows_make_the_same_artifact(tmp_path):
    assert run_sql(tmp_path, TYPES_SQL)["artifact_id"] == run_sql(tmp_path, TYPES_SQL)["artifact_id"]


def test_output_uses_the_parquet_options(tmp_path):
    res = run_sql(tmp_path, "select i from range(10) t(i)")
    meta = pq.ParquetFile(res["artifact_path"]).metadata
    assert meta.row_group(0).column(0).compression == "ZSTD"
    assert meta.format_version == "2.6"


def test_no_rows_keeps_the_columns(tmp_path):
    res = run_sql(tmp_path, "select date '2026-03-02' as d, 1.5::decimal(9, 2) as x where false")
    assert res["rows"] == 0
    assert res["schema"] == [{"name": "d", "type": "DATE"}, {"name": "x", "type": "DECIMAL(9,2)"}]


def test_reads_a_table_input_with_its_types(tmp_path):
    up = parquet(tmp_path / "up.parquet", "select date '2026-03-02' + i::int as d, i::decimal(10, 2) as v from range(3) t(i)")
    res = run_sql(tmp_path, "select d, v from up where d > date '2026-03-02'", inputs={"up": {"path": up, "kind": "table"}})
    assert res["rows"] == 2
    assert res["schema"] == [{"name": "d", "type": "DATE"}, {"name": "v", "type": "DECIMAL(10,2)"}]


def test_reads_a_partitioned_input_from_its_chunks(tmp_path):
    a = parquet(tmp_path / "a.parquet", "select date '2026-03-01' as d, 1 as x")
    b = parquet(tmp_path / "b.parquet", "select date '2026-03-02' as d, 2 as x, 'new' as extra")
    res = run_sql(
        tmp_path, "select d, x, extra from up order by d", inputs={"up": {"path": None, "paths": [a, b], "kind": "table"}}
    )
    assert res["rows"] == 2
    assert [c["type"] for c in res["schema"]] == ["DATE", "INTEGER", "VARCHAR"]


def test_an_empty_partitioned_input_fails_clearly(tmp_path):
    with pytest.raises(ValueError, match="no partitions yet"):
        run_sql(tmp_path, "select * from up", inputs={"up": {"path": None, "paths": [], "kind": "table"}})


def test_reads_the_parts_of_a_multi_output_input(tmp_path):
    saved = runner.save_artifact(
        {"coefs": pd.DataFrame({"term": ["a", "b"], "value": [1.0, 3.0]}), "meta": {"r2": 0.5}}, tmp_path / "fit"
    )
    res = run_sql(
        tmp_path, "select term from fit.coefs order by value desc limit 1",
        inputs={"fit": {"path": saved["artifact_path"], "kind": "multi"}},
    )
    assert res["rows"] == 1
    assert pq.read_table(res["artifact_path"]).column("term").to_pylist() == ["b"]


def test_json_inputs_and_missing_outputs_are_not_tables(tmp_path):
    j = tmp_path / "j.json"
    j.write_text(json.dumps({"a": 1}), encoding="utf-8")
    res = run_sql(
        tmp_path, "select 1 as one",
        inputs={"j": {"path": str(j), "kind": "json"}, "gone": {"path": None, "kind": "none"}},
    )
    assert res["rows"] == 1


def test_parameters_bind(tmp_path):
    res = run_sql(tmp_path, "select $as_of::date as d, $n::integer as n", params={"as_of": "2026-03-02", "n": 4, "unused": 1})
    assert res["schema"] == [{"name": "d", "type": "DATE"}, {"name": "n", "type": "INTEGER"}]
    assert pq.read_table(res["artifact_path"]).to_pylist() == [{"d": pd.Timestamp("2026-03-02").date(), "n": 4}]


def test_last_partition_binds_null_then_the_newest(tmp_path):
    sql = "select coalesce($last_partition, 'none') as last, date '2026-03-02' as d"
    first = run_sql(tmp_path, sql, partition_by="d")
    assert pq.read_table(first["artifact_path"]).column("last").to_pylist() == ["none"]
    later = run_sql(tmp_path, sql, partition_by="d", partitions=["2026-03-01", "2026-03-02"])
    assert pq.read_table(later["artifact_path"]).column("last").to_pylist() == ["2026-03-02"]


def test_statements_before_the_query_run_first(tmp_path):
    res = run_sql(
        tmp_path,
        "create temp table t as select i from range(5) t(i);\n-- the output\nselect i from t where i >= $lo order by i; -- done\n",
        params={"lo": 3},
    )
    assert pq.read_table(res["artifact_path"]).column("i").to_pylist() == [3, 4]


# -- end to end, through the workspace ---------------------------------------


def go(ws, coro_fn):
    async def main():
        ws.bus.bind(asyncio.get_running_loop())
        res = await coro_fn()
        for _ in range(200):
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
        return res

    return asyncio.run(main())


def test_types_survive_a_chain_of_sql_nodes(tmp_path):
    (tmp_path / "nodes").mkdir()
    (tmp_path / "nodes" / "base.sql").write_text(TYPES_SQL, encoding="utf-8")
    (tmp_path / "nodes" / "next.sql").write_text("select * from base where maybe is null or maybe >= 0\n", encoding="utf-8")
    ws = Workspace(tmp_path)
    res = go(ws, lambda: ws.run(["next"]))
    assert {n: r["status"] for n, r in res.items()} == {"base": "success", "next": "success"}
    run = ws.store.last_success("next", "")
    assert run["schema"] == TYPES_SCHEMA and run["rows"] == 3
