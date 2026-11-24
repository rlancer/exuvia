"""Push nodes: output uploaded from another machine (a Bloomberg Terminal's
desktop, say) rather than computed, recorded as a run that cascades."""

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import exuvia
from exuvia.project import Project
from exuvia.pushing import PushError, encode
from exuvia.server import create_app
from exuvia.workspace import Workspace

from test_engine import py, run

TOKEN = "push-test-token"
PARQUET = {"Content-Type": "application/vnd.apache.parquet"}


def graph(root, push_kw="push=True"):
    """``prices`` is pushed; ``doubled`` reads it."""
    py(root, "prices", kw=push_kw, body="raise RuntimeError('a push node never runs here')")
    py(root, "doubled", params="prices", body="return prices.assign(y=prices.x * 2)")


def parquet(df):
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def push(ws, name, df_or_json, fmt="parquet", tmp=None, **kw):
    path = tmp / f"upload.{fmt}"
    if fmt == "parquet":
        path.write_bytes(parquet(df_or_json))
    else:
        path.write_text(df_or_json, encoding="utf-8")

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        res = await ws.push(name, path, fmt, **kw)
        for _ in range(100):  # the cascade it spawned
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not pending:
                break
            await asyncio.gather(*pending)
        return res

    return asyncio.run(go())


def test_push_parses_and_takes_no_upstreams_or_schedule(tmp_path):
    graph(tmp_path)
    py(tmp_path, "with_dep", params="doubled", kw="push=True")
    py(tmp_path, "scheduled", kw='push=True, schedule="0 9 * * *"')
    py(tmp_path, "not_bool", kw='push="yes"')
    nodes = Project(tmp_path).load().nodes
    assert nodes["prices"].push and nodes["prices"].parse_error is None
    assert not nodes["doubled"].push
    assert "takes no upstream nodes" in nodes["with_dep"].parse_error
    assert "has no schedule" in nodes["scheduled"].parse_error
    assert "True or False" in nodes["not_bool"].parse_error


def test_a_push_is_a_run_that_cascades_and_nothing_here_runs_the_node(tmp_path):
    graph(tmp_path)
    ws = Workspace(tmp_path)
    st = ws.status()
    assert (st["prices"]["state"], st["prices"]["reason"]) == ("new", "waiting for a push")
    # Running everything leaves the push node alone; what reads it can't run yet.
    res = run(ws, ["prices", "doubled"])
    assert set(res) == {"doubled"}
    assert res["doubled"]["error"] == "upstream prices has not been pushed yet (exuvia push)"
    assert ws.store.runs_for("prices") == []

    pushed = push(ws, "prices", pd.DataFrame({"x": [1, 2, 3]}), tmp=tmp_path, note="from the terminal")
    assert (pushed["status"], pushed["trigger"], pushed["rows"]) == ("success", "push", 3)
    log = ws.store.run(pushed["id"])["log"]
    assert "output pushed: from the terminal" in log and "3 rows, 1 columns" in log
    st = ws.status()
    assert st["prices"]["state"] == "fresh" and st["doubled"]["state"] == "fresh"
    cascaded = ws.store.runs_for("doubled")[0]
    assert cascaded["trigger"] == "cascade" and cascaded["status"] == "success"
    assert list(ws.preview("doubled")["rows"]) == [[1, 2], [2, 4], [3, 6]]

    # The same data again: the same output, so nothing downstream re-runs.
    again = push(ws, "prices", pd.DataFrame({"x": [1, 2, 3]}), tmp=tmp_path)
    assert again["artifact_id"] == pushed["artifact_id"]
    assert len(ws.store.runs_for("doubled")) == 2  # the failed run and the cascade

    # Editing the push node's file doesn't make its pushed output stale.
    py(tmp_path, "prices", kw="push=True", body="'''Daily closes from the terminal.'''")
    ws.reload()
    assert ws.status()["prices"]["state"] == "fresh"


def test_unreadable_data_is_a_failed_run_and_only_push_nodes_take_pushes(tmp_path):
    graph(tmp_path)
    ws = Workspace(tmp_path)
    bad = tmp_path / "bad.parquet"
    bad.write_bytes(b"not parquet at all")

    async def go(name, path, fmt):
        return await ws.push(name, path, fmt)

    failed = asyncio.run(go("prices", bad, "parquet"))
    assert failed["status"] == "failed" and failed["trigger"] == "push"
    assert ws.status()["prices"]["state"] == "failed"
    with pytest.raises(ValueError, match="not a push node"):
        asyncio.run(go("doubled", bad, "parquet"))
    with pytest.raises(KeyError):
        asyncio.run(go("nope", bad, "parquet"))


def test_a_push_that_dies_midway_is_a_failed_run(tmp_path):
    graph(tmp_path)
    ws = Workspace(tmp_path)

    def down(rel):
        raise OSError("the object store is down")

    ws._upload = down
    with pytest.raises(OSError):
        push(ws, "prices", pd.DataFrame({"x": [1]}), tmp=tmp_path)
    assert [r["status"] for r in ws.store.runs_for("prices")] == ["failed"]
    assert ws.status()["prices"]["state"] != "running" and not ws._inflight


def test_json_and_run_parameters(tmp_path):
    (tmp_path / "exuvia.json").write_text(json.dumps({"params": {"as_of": {"type": "date", "default": "2026-11-20"}}}))
    py(tmp_path, "curve", kw='push=True, params=["as_of"]')
    ws = Workspace(tmp_path)
    one = push(ws, "curve", '{"tenor": [1, 2], "rate": [4.1, 4.3]}', fmt="json", tmp=tmp_path,
               params={"as_of": "2026-11-19"})
    assert (one["kind"], one["params"]) == ("json", {"as_of": "2026-11-19"})
    assert ws.status({"as_of": "2026-11-19"})["curve"]["state"] == "fresh"
    assert ws.status({"as_of": "2026-11-20"})["curve"]["state"] == "new"


def test_the_api_takes_pushes_and_refuses_to_run_or_schedule_a_push_node(tmp_path):
    graph(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        body = parquet(pd.DataFrame({"x": [5]}))
        r = client.post("/api/nodes/prices/push", content=body, headers=PARQUET, params={"note": "desk 4"})
        assert r.status_code == 200, r.text
        assert r.json()["run"]["rows"] == 1 and "log" not in r.json()["run"]
        r = client.post("/api/nodes/prices/run", json={})
        assert r.status_code == 400 and "push node" in r.json()["detail"]
        r = client.post("/api/nodes/prices/schedule", json={"cron": "0 9 * * *"})
        assert r.status_code == 400 and "push node" in r.json()["detail"]
        r = client.post("/api/nodes/prices/push", content=body, headers={"Content-Type": "image/png"})
        assert r.status_code == 400 and "parquet" in r.json()["detail"]
        # What a page on another site can send without asking first: a form or
        # text/plain, even naming the format (the server may have no auth).
        for ct in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", None):
            r = client.post("/api/nodes/prices/push", content=b'{"x": [1]}', params={"format": "json"},
                            headers={"Content-Type": ct} if ct else {})
            assert r.status_code == 415, (ct, r.text)
        octet = {"Content-Type": "application/octet-stream"}
        r = client.post("/api/nodes/prices/push", content=b"junk", headers=octet, params={"format": "parquet"})
        assert r.status_code == 422 and "could not be read" in r.json()["detail"]
        r = client.post("/api/nodes/doubled/push", content=body, headers=PARQUET)
        assert r.status_code == 400 and "not a push node" in r.json()["detail"]
        r = client.post("/api/nodes/prices/push", content=body, headers=PARQUET, params={"params": "[1]"})
        assert r.status_code == 400
        r = client.post("/api/nodes/prices/push", content=b"", headers=PARQUET)
        assert r.status_code == 400 and "nothing was sent" in r.json()["detail"]
    leftovers = [p.name for p in (tmp_path / ".exuvia").iterdir() if p.name.startswith("push-")]
    assert leftovers == []


def test_the_agent_is_told_a_push_node_is_pushed_not_run(tmp_path):
    graph(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        r = client.post("/api/tools/run_node", json={"name": "prices"})
        assert "push node" in json.dumps(r.json())


def test_encode():
    data, fmt = encode(pd.DataFrame({"x": [1]}))
    assert fmt == "parquet" and pd.read_parquet(io.BytesIO(data))["x"].tolist() == [1]
    data, fmt = encode({"a": 1})
    assert fmt == "json" and json.loads(data) == {"a": 1}
    with pytest.raises(PushError, match="its own push node"):
        encode({"a": pd.DataFrame({"x": [1]})})
    with pytest.raises(PushError, match="nothing to push"):
        encode(None)
    with pytest.raises(PushError, match=r"\.parquet, \.csv or \.json"):
        encode("prices.xlsx")


@pytest.fixture
def served(tmp_path, monkeypatch):
    import uvicorn

    monkeypatch.setenv("EXUVIA_API_TOKEN", TOKEN)
    graph(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(tmp_path), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    monkeypatch.delenv("EXUVIA_API_TOKEN")
    yield f"http://127.0.0.1:{port}", tmp_path
    server.should_exit = True
    thread.join(timeout=10)


def test_pushing_from_python_and_from_the_command_line(served, tmp_path):
    url, root = served
    with pytest.raises(PushError, match="needs a token"):
        exuvia.push(url, "prices", pd.DataFrame({"x": [1]}))
    pushed = exuvia.push(url, "prices", pd.DataFrame({"x": [1, 2]}), token=TOKEN, note="from python")
    assert pushed["status"] == "success" and pushed["rows"] == 2

    csv = tmp_path / "closes.csv"
    csv.write_text("x\n7\n8\n9\n", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-m", "exuvia.cli", "push", "prices", str(csv), "--url", url, "--note", "nightly"],
        capture_output=True, text=True, env={**os.environ, "EXUVIA_API_TOKEN": TOKEN},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("pushed prices: 3 rows")
    with pytest.raises(PushError, match="no node named"):
        exuvia.push(url, "missing", pd.DataFrame({"x": [1]}), token=TOKEN)
    with pytest.raises(PushError, match="plain http"):
        exuvia.push("http://203.0.113.9/p/x", "prices", pd.DataFrame({"x": [1]}), token=TOKEN)
