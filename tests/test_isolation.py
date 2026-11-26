"""What node code and ad-hoc SQL can reach: node outputs and the project's
own files, never exuvia's own settings or files outside the outputs."""

import asyncio
import json
import sys
import textwrap

import duckdb
import pytest

from exuvia.agent import Agent
from exuvia.pool import node_environ
from exuvia.workspace import Workspace

ENV_NODE = '''
import os

import exuvia


@exuvia.node()
def env():
    """Which variables this node sees."""
    names = (
        "EXUVIA_API_TOKEN", "EXUVIA_FAILURE_WEBHOOK", "PORTKEY_API_KEY", "UV_INDEX_CORP_PASSWORD",
        "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "PLAIN_SETTING",
    )
    return {n: os.environ.get(n) for n in names}
'''

NUMS = '''
import exuvia


@exuvia.node()
def nums():
    import pandas as pd

    return pd.DataFrame({"n": [1, 2, 3]})
'''


def write(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def run(ws, targets):
    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        return await ws.run(targets, cascade=False)

    return asyncio.run(go())


def test_a_node_never_sees_exuvias_own_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_API_TOKEN", "hub-token-123")
    monkeypatch.setenv("EXUVIA_FAILURE_WEBHOOK", "https://hooks.example.com/T000/secret")
    monkeypatch.setenv("PORTKEY_API_KEY", "pk-gateway")
    monkeypatch.setenv("UV_INDEX_CORP_PASSWORD", "index-pass")
    # Variables a node may use itself stay, as before.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("PLAIN_SETTING", "shared")
    write(tmp_path, "nodes/env.py", ENV_NODE)
    ws = Workspace(tmp_path)
    run(ws, ["env"])
    last = ws.store.last_success("env", "")
    assert last is not None, ws.store.last_run("env")
    seen = json.loads((tmp_path / last["artifact_path"]).read_text(encoding="utf-8"))
    assert seen == {
        "EXUVIA_API_TOKEN": None,
        "EXUVIA_FAILURE_WEBHOOK": None,
        "PORTKEY_API_KEY": None,
        "UV_INDEX_CORP_PASSWORD": None,
        "ANTHROPIC_API_KEY": "sk-ant-key",
        "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
        "PLAIN_SETTING": "shared",
    }


def test_node_environ():
    env = {
        "PATH": "/bin", "HTTPS_PROXY": "http://proxy", "EXUVIA_DB": "postgresql://x",
        "EXUVIA_API_TOKEN_FILE": "/run/secrets/t", "PORTKEY_API_KEY": "k", "UV_INDEX_URL": "https://idx",
        "UV_INDEX_CORP_USERNAME": "u", "UV_INDEX_CORP_PASSWORD_FILE": "/run/secrets/p", "OPENAI_API_KEY": "k",
    }
    assert node_environ(env) == {
        "PATH": "/bin", "HTTPS_PROXY": "http://proxy", "UV_INDEX_URL": "https://idx", "OPENAI_API_KEY": "k",
    }
    # The installer keeps the index credentials uv needs.
    assert {"UV_INDEX_CORP_USERNAME", "UV_INDEX_CORP_PASSWORD_FILE"} <= node_environ(env, index=False).keys()


def test_ad_hoc_sql_reads_outputs_and_nothing_else(tmp_path):
    write(tmp_path, "nodes/nums.py", NUMS)
    ws = Workspace(tmp_path)
    run(ws, ["nums"])
    assert ws.query("select sum(n) as s from nums")["rows"] == [[6]]
    out = ws.store.last_success("nums", "")["artifact_path"]
    # Its own output file stays readable by path.
    assert ws.query(f"select count(*) from read_parquet('{(tmp_path / out).as_posix()}')")["rows"] == [[3]]
    for sql in (
        "select content from read_text('/proc/self/environ')",
        f"select * from read_text('{(tmp_path / 'nodes' / 'nums.py').as_posix()}')",
        f"select * from read_blob('{(tmp_path / '.exuvia' / 'exuvia.db').as_posix()}')",
        "select * from glob('**')",
        f"copy (select 1) to '{(tmp_path / 'leak.csv').as_posix()}'",
        f"attach '{(tmp_path / 'x.duckdb').as_posix()}' as x",
        "set enable_external_access = true",
        "set lock_configuration = false",
        "install httpfs",
    ):
        with pytest.raises(duckdb.Error):  # refused by the sandbox: permission, catalog or parser error
            ws.query(sql)
    assert not (tmp_path / "leak.csv").exists() and not (tmp_path / "x.duckdb").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self/environ is Linux's")
def test_the_agent_cannot_read_the_servers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EXUVIA_API_TOKEN", "hub-token-123")
    ws = Workspace(tmp_path)
    agent = Agent(ws)

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        return await agent.call_tool("query_data", {"sql": "select content from read_text('/proc/self/environ')"})

    ok, text, _ = asyncio.run(go())
    assert not ok and "hub-token-123" not in text
