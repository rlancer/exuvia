"""Run nodes through a real `exuvia executor` HTTP service on a local port."""

import asyncio
import socket
import textwrap
import threading
import time

import httpx
import pytest
import uvicorn

from exuvia.workspace import Workspace

TOKEN = "test-token"


@pytest.fixture(scope="module")
def executor_url(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("EXUVIA_EXECUTOR_TOKEN", TOKEN)
    mp.setenv("EXUVIA_EXECUTOR_DIR", str(tmp_path_factory.mktemp("executor")))
    from exuvia.executor_service import create_executor_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_executor_app(), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=10)
    mp.undo()


def write(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def run(ws, targets, **kw):
    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        return await ws.run(targets, cascade=False, **kw)

    return asyncio.run(go())


@pytest.fixture
def remote_ws(tmp_path, executor_url, monkeypatch):
    monkeypatch.setenv("EXUVIA_EXECUTOR_URL", executor_url)
    monkeypatch.setenv("EXUVIA_EXECUTOR_TOKEN", TOKEN)
    # a shared helper module + data file: proves the project bundle ships
    write(tmp_path, "lib/scale.py", "FACTOR = 10\n")
    write(tmp_path, "data/base.csv", "x\n1\n2\n3\n")
    write(tmp_path, "nodes/base.py", '''
        import exuvia

        @exuvia.node()
        def base():
            import pandas as pd
            from lib.scale import FACTOR
            print("scaling by", FACTOR)
            return pd.read_csv("data/base.csv").assign(y=lambda d: d.x * FACTOR)
    ''')
    write(tmp_path, "nodes/total.sql", "select sum(y) as s from base\n")
    ws = Workspace(tmp_path)
    yield ws
    ws.executor.close()


def test_remote_chain_with_helpers_and_sql(remote_ws):
    from exuvia.executor import RemoteExecutor

    assert isinstance(remote_ws.executor, RemoteExecutor)
    finals = run(remote_ws, ["total"])
    assert {n: r["status"] for n, r in finals.items()} == {"base": "success", "total": "success"}
    assert remote_ws.preview("total")["rows"] == [[60]]
    assert "scaling by 10" in remote_ws.store.run(finals["base"]["id"])["log"]
    # artifacts land in the control plane's store, content-addressed
    art = remote_ws.store.last_success("base")
    assert art["artifact_path"].endswith(f"{art['artifact_id']}.parquet")


def test_remote_failure_reports_traceback(remote_ws, tmp_path):
    write(tmp_path, "nodes/base.py", '''
        import exuvia

        @exuvia.node()
        def base():
            raise RuntimeError("remote boom")
    ''')
    remote_ws.reload()
    finals = run(remote_ws, ["base"])
    assert finals["base"]["status"] == "failed"
    assert "remote boom" in finals["base"]["error"]
    assert "Traceback" in remote_ws.store.run(finals["base"]["id"])["log"]


def test_remote_nodes_never_see_the_executors_token(remote_ws, tmp_path):
    write(tmp_path, "nodes/base.py", '''
        import os

        import exuvia

        @exuvia.node()
        def base():
            return {"token": os.environ.get("EXUVIA_EXECUTOR_TOKEN")}
    ''')
    remote_ws.reload()
    finals = run(remote_ws, ["base"])
    assert finals["base"]["status"] == "success", finals["base"].get("error")
    art = remote_ws.store.last_success("base")
    assert (tmp_path / art["artifact_path"]).read_text(encoding="utf-8") == '{"token": null}'


def test_remote_log_is_capped_and_keeps_the_traceback(remote_ws, tmp_path, monkeypatch):
    from exuvia import executor

    monkeypatch.setattr(executor, "MAX_LOG_BYTES", 8 * 1024)  # executor service runs in-process
    write(tmp_path, "nodes/base.py", '''
        import exuvia

        @exuvia.node()
        def base():
            for i in range(20000):
                print("spam", i)
            raise RuntimeError("remote noisy boom")
    ''')
    remote_ws.reload()
    finals = run(remote_ws, ["base"])
    assert "remote noisy boom" in finals["base"]["error"]
    log = remote_ws.store.run(finals["base"]["id"])["log"]
    assert len(log.encode()) < 8 * 1024 + 500
    assert log.startswith("spam 0") and "dropped" in log
    assert "Traceback" in log and "remote noisy boom" in log


def test_executor_requires_token(executor_url):
    assert httpx.get(f"{executor_url}/healthz").json()["auth"] is True
    assert httpx.head(f"{executor_url}/v1/bundles/{'0' * 24}").status_code == 401
    bad = httpx.put(
        f"{executor_url}/v1/bundles/{'0' * 24}", content=b"not a zip of that hash",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert bad.status_code in (400, 422, 500) and bad.status_code != 200


def test_remote_installs_the_projects_declared_packages(remote_ws, tmp_path, monkeypatch):
    """The executor builds the packages in the bundle's pyproject.toml itself."""
    from test_packages import make_wheel, pyproject

    monkeypatch.setenv("UV_OFFLINE", "1")
    index = tmp_path.parent / f"{tmp_path.name}-wheels"
    make_wheel(index, "acme-internal", "1.0", "ANSWER = 42\n")
    pyproject(tmp_path, index, ["acme-internal"])
    write(tmp_path, "nodes/base.py", '''
        import exuvia

        @exuvia.node()
        def base():
            import acme_internal
            import pandas as pd
            return pd.DataFrame({"answer": [acme_internal.ANSWER]})
    ''')
    remote_ws.reload()
    finals = run(remote_ws, ["base"])
    assert finals["base"]["status"] == "success", finals["base"].get("error")
    assert remote_ws.preview("base")["rows"] == [[42]]
    assert not (tmp_path / ".exuvia" / "packages").exists()  # installed on the executor's side


def test_remote_multi_output(remote_ws, tmp_path):
    write(tmp_path, "nodes/fit.py", '''
        import exuvia
        @exuvia.node()
        def fit(base):
            return {"scaled": base, "stats": {"n": len(base)}}
    ''')
    write(tmp_path, "nodes/n.sql", "select count(*) as n from fit.scaled\n")
    remote_ws.reload()
    finals = run(remote_ws, ["n"])
    assert {n: r["status"] for n, r in finals.items()} == {"base": "success", "fit": "success", "n": "success"}
    assert remote_ws.preview("n")["rows"] == [[3]]
    art = remote_ws.store.last_success("fit")
    assert art["kind"] == "multi" and art["artifact_path"].endswith(f"{art['artifact_id']}.zip")
    assert remote_ws.preview("fit", part="stats")["value"] == {"n": 3}


def test_a_remote_run_gets_the_projects_variables(remote_ws):
    """A .env never reaches the executor; the project's variables travel with each run."""
    import json

    write(remote_ws.project.root, "nodes/env.py", '''
        import os

        import exuvia

        @exuvia.node()
        def env():
            print("key is", os.environ.get("REMOTE_KEY"))
            return {"key": os.environ.get("REMOTE_KEY")}
    ''')
    remote_ws.reload()
    remote_ws.set_env_var("REMOTE_KEY", "remote-secret-42\r\nsecond-line-77", secret=True)  # multi-line, CRLF
    run(remote_ws, ["env"])
    last = remote_ws.store.last_success("env", "")
    assert last is not None, remote_ws.store.last_run("env", "")
    out = json.loads((remote_ws.project.root / last["artifact_path"]).read_text(encoding="utf-8"))
    assert out == {"key": "remote-secret-42\nsecond-line-77\n"}
    log = remote_ws.store.run(last["id"])["log"]
    assert "key is ***" in log and "remote-secret-42" not in log and "second-line-77" not in log



def test_remote_run_reads_a_partitioned_input(tmp_path, executor_url, monkeypatch):
    monkeypatch.setenv("EXUVIA_EXECUTOR_URL", executor_url)
    monkeypatch.setenv("EXUVIA_EXECUTOR_TOKEN", TOKEN)
    write(tmp_path, "nodes/daily.py", '''
        import exuvia

        @exuvia.node(partition_by="d")
        def daily():
            import pandas as pd
            have = exuvia.partitions()
            return pd.DataFrame({"d": [f"2026-01-0{len(have) + 1}"], "v": [len(have) + 1]})
    ''')
    write(tmp_path, "nodes/total.sql", "select sum(v) as s, count(*) as n from daily\n")
    ws = Workspace(tmp_path)
    try:
        for _ in range(3):  # exuvia.partitions() reaches the remote node too
            assert run(ws, ["daily"], force={"daily"})["daily"]["status"] == "success"
        assert [p["value"] for p in ws.partitions_of("daily")["partitions"]] == ["2026-01-01", "2026-01-02", "2026-01-03"]
        sent = []
        put = ws.executor.http.put

        def spy(url, **kw):
            if url.startswith("/v1/chunks/"):
                sent.append(url.rsplit("/", 1)[1])
            return put(url, **kw)

        ws.executor.http.put = spy
        finals = run(ws, ["total"])
        assert finals["total"]["status"] == "success", finals["total"]["error"]
        assert ws.preview("total")["rows"] == [[6, 3]]
        files = [f["artifact_id"] for f in ws.partitions_of("daily")["files"]]
        assert sorted(sent) == sorted(files) and len(files) == 3  # each chunk, by its hash

        # The executor keeps them: the next read sends only the new day's chunk.
        sent.clear()
        assert run(ws, ["daily"], force={"daily"})["daily"]["status"] == "success"
        assert run(ws, ["total"], force={"total"})["total"]["status"] == "success"
        assert ws.preview("total")["rows"] == [[10, 4]]
        assert len(sent) == 1

        # An executor without a chunk cache gets the input as one file.
        ws.executor._chunks_ok = False
        sent.clear()
        assert run(ws, ["total"], force={"total"})["total"]["status"] == "success"
        assert ws.preview("total")["rows"] == [[10, 4]] and sent == []
    finally:
        ws.executor.close()


def test_executor_verifies_chunks(executor_url):
    import hashlib

    h = {"Authorization": f"Bearer {TOKEN}"}
    blob = b"not really parquet"
    digest = hashlib.sha256(blob).hexdigest()[:16]
    with httpx.Client(base_url=executor_url, headers=h) as c:
        assert c.put("/v1/chunks/0123456789abcdef", content=blob).status_code == 400
        assert c.put("/v1/chunks/../../x", content=blob).status_code in (400, 404)
        assert c.post("/v1/chunks/missing", json={"chunks": [digest]}).json() == {"missing": [digest]}
        assert c.put(f"/v1/chunks/{digest}", content=blob).status_code == 200
        assert c.post("/v1/chunks/missing", json={"chunks": [digest]}).json() == {"missing": []}
        assert c.post("/v1/chunks/missing", json={"chunks": ["../etc"]}).status_code == 400
        # A run naming a chunk the executor doesn't have is refused before it starts.
        spec = {"bundle": "0" * 24, "node": "x", "kind": "sql", "path": "x.sql",
                "inputs": {"d": {"kind": "table", "chunks": ["fedcba9876543210"]}}}
        assert c.post("/v1/runs/r1", json=spec).status_code == 409
        assert httpx.post(f"{executor_url}/v1/chunks/missing", json={"chunks": []}).status_code == 401
