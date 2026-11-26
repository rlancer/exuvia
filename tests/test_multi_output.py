"""Nodes that return several named outputs (a dict, dataclass or pydantic
model with DataFrame values), and typed JSON in and out."""

import asyncio
import json
import textwrap
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exuvia import runner
from exuvia.server import create_app
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


FIT = '''
import exuvia

@exuvia.node()
def fit():
    import pandas as pd
    preds = pd.DataFrame({"x": [1, 2, 3], "yhat": [1.5, 2.5, 3.5]})
    coefs = pd.DataFrame({"term": ["a", "b"], "value": [0.5, 1.0]})
    return {"preds": preds, "coefs": coefs, "metrics": {"r2": 0.9}}
'''


def fit_project(root: Path) -> Path:
    write(root, "nodes/fit.py", FIT)
    return root


def test_save_artifact_writes_a_deterministic_zip(tmp_path):
    import pandas as pd

    def save():
        return runner.save_artifact({"t": pd.DataFrame({"a": [1, 2]}), "meta": {"n": 2}}, tmp_path)

    a, b = save(), save()
    assert a["kind"] == "multi" and a["artifact_path"].endswith(".zip")
    # same outputs -> same bytes -> same id, so an unchanged run stops a cascade
    assert a["artifact_id"] == b["artifact_id"]
    assert [p["name"] for p in a["schema"]] == ["t", "meta"]
    assert a["schema"][0] == {"name": "t", "type": "table", "rows": 2, "columns": [{"name": "a", "type": "int64"}]}
    assert runner.part_members(a["artifact_path"]) == [("t", "table"), ("meta", "json")]
    parts = runner.load_artifact(a["artifact_path"], "multi")
    assert parts["t"]["a"].tolist() == [1, 2] and parts["meta"] == {"n": 2}


def test_a_dict_without_tables_is_still_json(tmp_path):
    out = runner.save_artifact({"window": 20, "tickers": ["a"]}, tmp_path)
    assert out["kind"] == "json"


@pytest.mark.parametrize("name", ["Preds", "1st", "a-b", ""])
def test_part_names_must_be_identifiers(tmp_path, name):
    import pandas as pd

    with pytest.raises(TypeError, match="lowercase identifier"):
        runner.save_artifact({name: pd.DataFrame({"a": [1]})}, tmp_path)


def test_dataclasses_and_pydantic_models_are_json(tmp_path):
    import dataclasses
    import datetime as dt

    import pydantic

    @dataclasses.dataclass
    class Point:
        x: int
        when: dt.date

    class Summary(pydantic.BaseModel):
        name: str
        points: list[int]

    out = runner.save_artifact({"p": Point(1, dt.date(2026, 1, 2)), "s": Summary(name="a", points=[1])}, tmp_path)
    assert out["kind"] == "json"
    assert json.loads(Path(out["artifact_path"]).read_text()) == {
        "p": {"x": 1, "when": "2026-01-02"},
        "s": {"name": "a", "points": [1]},
    }


def test_multi_output_end_to_end(tmp_path):
    fit_project(tmp_path)
    write(tmp_path, "nodes/top_coef.sql", "select term from fit.coefs order by value desc limit 1\n")
    write(tmp_path, "nodes/report.py", '''
        import exuvia

        @exuvia.node()
        def report(fit):
            import pandas as pd
            return pd.DataFrame({"n": [len(fit["preds"])], "r2": [fit["metrics"]["r2"]]})
    ''')
    ws = Workspace(tmp_path)
    assert sorted(ws.graph.nodes["top_coef"].deps) == ["fit"]
    finals = run(ws, ["top_coef", "report"])
    assert {n: r["status"] for n, r in finals.items()} == {"fit": "success", "top_coef": "success", "report": "success"}
    assert ws.preview("top_coef")["rows"] == [["b"]]
    assert ws.preview("report")["rows"] == [[3, 0.9]]

    st = ws.status()["fit"]
    assert st["output_kind"] == "multi" and st["state"] == "fresh"
    assert [p["name"] for p in st["schema"]] == ["preds", "coefs", "metrics"]

    whole = ws.preview("fit")
    assert whole["kind"] == "multi"
    assert whole["parts"] == [
        {"name": "preds", "kind": "table", "rows": 3},
        {"name": "coefs", "kind": "table", "rows": 2},
        {"name": "metrics", "kind": "json", "rows": None},
    ]
    coefs = ws.preview("fit", part="coefs")
    assert coefs["kind"] == "table" and coefs["total"] == 2 and coefs["part"] == "coefs"
    assert [c["name"] for c in coefs["columns"]] == ["term", "value"]
    assert ws.preview("fit", part="metrics")["value"] == {"r2": 0.9}
    with pytest.raises(KeyError, match="no output 'nope'"):
        ws.preview("fit", part="nope")
    with pytest.raises(KeyError, match="single output"):
        ws.preview("report", part="coefs")

    assert json.loads(ws.preview_arrow("fit", part="preds").schema.metadata[b"exuvia"])["total"] == 3
    assert ws.preview_arrow("fit") is None and ws.preview_arrow("fit", part="metrics") is None

    # ad-hoc SQL: each table part is <node>.<part>
    assert ws.query("select count(*) from fit.preds join fit.coefs on true")["rows"] == [[6]]
    # ... or "<node>.<part>" as one quoted name, as views and DuckDB's hint write it
    assert ws.query('select count(*) from "fit.preds"')["rows"] == [[3]]
    assert ws.query('select count(*) from "fit"."preds"')["rows"] == [[3]]
    # the node alone, or a part without its node, names the tables to query
    with pytest.raises(Exception, match=r"fit has several outputs, each a table of its own: query fit.coefs, fit.preds"):
        ws.query("select * from fit")
    with pytest.raises(Exception, match=r"query it as fit.coefs"):
        ws.query("select * from coefs")
    with pytest.raises(Exception, match="nope"):
        ws.query("select * from nope")


def test_a_sql_node_reads_a_part_as_one_quoted_name(tmp_path):
    fit_project(tmp_path)
    write(tmp_path, "nodes/n.sql", 'select count(*) as n from "fit.preds"\n')
    ws = Workspace(tmp_path)
    assert run(ws, ["n"])["n"]["status"] == "success"
    assert ws.preview("n")["rows"] == [[3]]


def test_unchanged_parts_stop_the_cascade_and_changed_parts_run_it(tmp_path):
    fit_project(tmp_path)
    write(tmp_path, "nodes/n.sql", "select count(*) as n from fit.preds\n")
    ws = Workspace(tmp_path)
    run(ws, ["n"])
    first = ws.store.last_success("fit")["artifact_id"]
    finals = run(ws, ["fit"], force={"fit"})
    assert ws.store.last_success("fit")["artifact_id"] == first
    assert "n" not in finals
    write(tmp_path, "nodes/fit.py", FIT.replace('"r2": 0.9', '"r2": 0.8'))
    ws.reload()
    run(ws, ["fit"])
    assert ws.store.last_success("fit")["artifact_id"] != first
    # the cascade re-ran n on the new output
    assert ws.status()["n"]["state"] == "fresh"
    assert ws.store.last_success("n")["inputs"]["fit"] == ws.store.last_success("fit")["artifact_id"]


def test_runs_history_per_part(tmp_path):
    write(tmp_path, "exuvia.json", json.dumps({"params": {"day": {"type": "string", "default": "a"}}}))
    write(tmp_path, "nodes/fit.py", '''
        import exuvia

        @exuvia.node(params=["day"])
        def fit():
            import pandas as pd
            day = exuvia.param("day")
            return {"preds": pd.DataFrame({"v": [len(day)]}), "note": day}
    ''')
    ws = Workspace(tmp_path)
    run(ws, ["fit"], params={"day": "a"})
    run(ws, ["fit"], params={"day": "bbb"})
    rows = ws.query("select _day, v from fit.preds__runs order by _day")["rows"]
    assert rows == [["a", 1], ["bbb", 3]]


def test_typed_inputs_from_dataclasses_and_pydantic(tmp_path):
    write(tmp_path, "lib/models.py", '''
        import dataclasses

        import pandas as pd
        import pydantic


        @dataclasses.dataclass
        class Fit:
            preds: pd.DataFrame
            coefs: pd.DataFrame
            metrics: dict


        class Settings(pydantic.BaseModel):
            window: int
            tickers: list[str]
    ''')
    write(tmp_path, "nodes/fit.py", '''
        import exuvia
        from lib.models import Fit

        @exuvia.node()
        def fit() -> Fit:
            import pandas as pd
            return Fit(preds=pd.DataFrame({"y": [1, 2]}), coefs=pd.DataFrame({"c": [3]}), metrics={"r2": 0.5})
    ''')
    write(tmp_path, "nodes/settings.py", '''
        import exuvia
        from lib.models import Settings

        @exuvia.node()
        def settings():
            return Settings(window=20, tickers=["a", "b"])
    ''')
    write(tmp_path, "nodes/summary.py", '''
        from __future__ import annotations

        import exuvia
        from lib.models import Fit, Settings

        @exuvia.node()
        def summary(fit: Fit, settings: Settings):
            import pandas as pd
            assert isinstance(fit, Fit) and isinstance(settings, Settings)
            return pd.DataFrame({"rows": [len(fit.preds)], "r2": [fit.metrics["r2"]], "window": [settings.window]})
    ''')
    ws = Workspace(tmp_path)
    finals = run(ws, ["summary"])
    assert finals["summary"]["status"] == "success", ws.store.run(finals["summary"]["id"])["error"]
    assert ws.preview("summary")["rows"] == [[2, 0.5, 20]]
    assert ws.preview("settings")["value"] == {"window": 20, "tickers": ["a", "b"]}
    assert ws.status()["fit"]["output_kind"] == "multi"


def test_typed_input_that_does_not_fit_fails_clearly(tmp_path):
    write(tmp_path, "lib/models.py", '''
        import pydantic

        class Settings(pydantic.BaseModel):
            window: int
    ''')
    write(tmp_path, "nodes/settings.py", '''
        import exuvia

        @exuvia.node()
        def settings():
            return {"window": "not a number"}
    ''')
    write(tmp_path, "nodes/use.py", '''
        import exuvia
        from lib.models import Settings

        @exuvia.node()
        def use(settings: Settings):
            return settings.window
    ''')
    ws = Workspace(tmp_path)
    finals = run(ws, ["use"])
    assert finals["use"]["status"] == "failed"
    assert "could not build Settings for parameter 'settings'" in ws.store.run(finals["use"]["id"])["error"]


def test_routes_serve_parts(tmp_path):
    fit_project(fit_project(tmp_path))
    write(tmp_path, "views/fitview/view.json", json.dumps({"title": "Fit", "reads": ["fit"]}))
    write(tmp_path, "views/fitview/App.tsx", "export default function App() { return null; }\n")
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/nodes/fit/run", json={"wait": 60}).json()["ok"]

        whole = client.get("/api/nodes/fit/data").json()
        assert whole["kind"] == "multi" and [p["name"] for p in whole["parts"]] == ["preds", "coefs", "metrics"]
        assert client.get("/api/nodes/fit/data", params={"part": "coefs"}).json()["total"] == 2
        assert client.get("/api/nodes/fit/data", params={"part": "nope"}).status_code == 404
        arrow = client.get(
            "/api/nodes/fit/data", params={"part": "preds"}, headers={"accept": "application/vnd.apache.arrow.stream"}
        )
        assert arrow.headers["content-type"].startswith("application/vnd.apache.arrow.stream")

        view = next(v for v in client.get("/api/views").json()["views"] if v["name"] == "fitview")
        out = view["outputs"][0]
        assert out["kind"] == "multi" and [p["name"] for p in out["parts"]] == ["preds", "coefs", "metrics"]
        got = client.get("/api/views/fitview/data/fit", params={"part": "metrics"}).json()
        assert got["value"] == {"r2": 0.9}

        aid = out["artifact_id"]
        z = client.get(f"/api/artifacts/fit/{aid}.zip")
        assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
        assert client.get(f"/api/artifacts/fit/{aid}/coefs.parquet").status_code == 200
        assert client.get(f"/api/artifacts/fit/{aid}/metrics.json").json() == {"r2": 0.9}
        assert client.get(f"/api/artifacts/fit/{aid}/metrics.parquet").status_code == 404
        assert client.get(f"/api/artifacts/fit/{aid}/nope.parquet").status_code == 404

        q = client.post("/api/views/fitview/query", json={"sql": "select count(*) as n from fit.coefs"}).json()
        assert q["rows"] == [[2]]

        report = client.post("/api/nodes/fit/run", json={"wait": 60, "force": True}).json()
        assert report["parts"]["coefs"]["head"][0] == {"term": "a", "value": 0.5}
        assert report["parts"]["metrics"]["value"] == {"r2": 0.9}


def test_parts_cache_is_rebuilt_and_left_out_of_exports(tmp_path):
    fit_project(tmp_path)
    ws = Workspace(tmp_path)
    run(ws, ["fit"])
    ls = ws.store.last_success("fit")
    path, kind = ws.part_file(ls, "coefs")
    assert kind == "table" and ".exuvia/parts/fit/" in path.as_posix()
    path.unlink()
    assert ws.part_file(ls, "coefs")[0].is_file()
    with zipfile.ZipFile(ws.artifact_path(ls)) as z:
        assert z.namelist() == ["preds.parquet", "coefs.parquet", "metrics.json"]
