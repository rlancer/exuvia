"""What a project keeps on disk (storage.py, GET /api/storage)."""

import json
import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from exuvia import partitioned as partitions
from exuvia.server import create_app


def write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def test_outputs_by_node_parquet_and_compaction(tmp_path, monkeypatch):
    monkeypatch.setattr(partitions, "COMPACT_AFTER", 3)
    (tmp_path / "exuvia.json").write_text(
        json.dumps({"params": {"as_of": {"type": "date", "default": "2026-03-02"}}}), encoding="utf-8"
    )
    write(tmp_path, "nodes/px.py", '''
        import os
        import exuvia

        @exuvia.node()
        def px():
            import pandas as pd
            n = int(open("n.txt").read()) if os.path.exists("n.txt") else 3
            return pd.DataFrame({"v": range(n)})
    ''')
    write(tmp_path, "nodes/meta.py", '''
        import exuvia

        @exuvia.node()
        def meta():
            return {"ok": True}
    ''')
    write(tmp_path, "nodes/daily.py", '''
        import exuvia

        @exuvia.node(params=["as_of"], partition_by="date")
        def daily():
            import pandas as pd
            return pd.DataFrame({"date": [exuvia.param("as_of")], "x": [1]})
    ''')
    with TestClient(create_app(tmp_path)) as client:
        empty = client.get("/api/storage").json()
        assert empty["outputs"]["files"] == 0 and empty["code"]["files"] >= 4
        assert {n["name"] for n in empty["nodes"]} == {"px", "meta", "daily"}

        for node in ("px", "meta"):
            assert client.post(f"/api/nodes/{node}/run", json={"wait": 60}).json()["ok"]
        # A second output of px: the first is kept, no longer current.
        (tmp_path / "n.txt").write_text("1000", encoding="utf-8")
        assert client.post("/api/nodes/px/run", json={"wait": 60}).json()["ok"]
        for d in range(2, 8):
            got = client.post(
                "/api/nodes/daily/run", json={"wait": 60, "params": {"as_of": f"2026-03-{d:02d}"}, "force": True}
            ).json()
            assert got["ok"], got

        s = client.get("/api/storage").json()
        nodes = {n["name"]: n for n in s["nodes"]}
        px = nodes["px"]
        assert px["files"] == 2 and px["current_files"] == 1 and px["runs"] == 2
        assert px["rows"] == 1000 and 0 < px["current_bytes"] < px["bytes"]
        assert nodes["meta"]["kinds"] == {"json": 1} and nodes["meta"]["rows"] == 0

        assert s["parquet"]["files"] == s["outputs"]["by_kind"]["parquet"]["files"] >= 3
        assert s["parquet"]["uncompressed_bytes"] > 0 and 0 < s["parquet"]["compressed_bytes"] < s["parquet"]["bytes"]
        assert px["compressed_bytes"] > 0
        assert [c["codec"] for c in s["parquet"]["codecs"]] == ["ZSTD"]

        [daily] = s["partitioned"]
        assert daily["node"] == "daily" and daily["partitions"] == 6 and daily["periods"] == 1
        assert daily["limit"] == 1 + 3 == s["compact_after"] + 1
        # Past the limit its files were merged; the chunks they replaced are kept.
        assert daily["files"] <= daily["limit"] and daily["merged_files"] >= 1
        assert daily["superseded_files"] >= 1
        assert nodes["daily"]["partitioned"] and nodes["daily"]["current_files"] == daily["files"]

        keys = {p["key"]: p for p in s["state"]}
        assert keys["artifacts"]["bytes"] == s["outputs"]["bytes"]
        assert s["database"]["kind"] == "sqlite" and s["database"]["in_project"]
        assert s["database"]["bytes"] == keys["exuvia.db"]["bytes"] > 0
        assert s["database"]["rows"]["runs"] == 9
        assert s["remote"] == {"configured": False, "uploaded": 0, "missing": 0}
