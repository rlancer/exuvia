"""The agent's tools run against the real workspace; no API key needed."""

import asyncio
import json

from exuvia.agent import Agent, _validate
from exuvia.workspace import Workspace

SRC = '''import exuvia


@exuvia.node()
def nums():
    """Three numbers."""
    import pandas as pd

    return pd.DataFrame({"n": [1, 2, 3]})
'''


def call(agent, name, args):
    async def go():
        agent.ws.bus.bind(asyncio.get_running_loop())
        return await agent._call(name, args)

    return asyncio.run(go())


def test_write_run_query_roundtrip(tmp_path):
    agent = Agent(Workspace(tmp_path))

    ok, text, extra = call(agent, "write_node", {"name": "nums", "kind": "python", "source": SRC})
    assert ok and extra["node"] == "nums"

    ok, text, _ = call(agent, "write_node", {"name": "total", "kind": "sql", "source": "select sum(n) as s from nums\n"})
    assert ok and json.loads(text)["deps"] == ["nums"]

    ok, text, _ = call(agent, "run_node", {"name": "total"})
    out = json.loads(text)
    assert ok and out["runs"] == {"nums": "success", "total": "success"}
    assert out["head"] == [{"s": 6}]

    ok, text, _ = call(agent, "query_data", {"sql": "select max(n) as m from nums"})
    assert ok and json.loads(text) == [{"m": 3}]

    ok, text, _ = call(agent, "list_nodes", {})
    names = {n["name"]: n for n in json.loads(text)}
    assert names["total"]["state"] == "fresh" and names["nums"]["kind"] == "python"


def test_failures_come_back_to_the_model(tmp_path):
    agent = Agent(Workspace(tmp_path))
    bad = SRC.replace('return pd.DataFrame({"n": [1, 2, 3]})', "return undefined_name")
    call(agent, "write_node", {"name": "nums", "kind": "python", "source": bad})
    ok, text, _ = call(agent, "run_node", {"name": "nums"})
    out = json.loads(text)
    assert not ok and "NameError" in out["error"] and "undefined_name" in out["log_tail"]

    ok, text, _ = call(agent, "write_node", {"name": "broken", "kind": "python", "source": "def x(:\n"})
    assert not ok and "SyntaxError" in text

    ok, text, _ = call(agent, "query_data", {"sql": "select * from nope"})
    assert not ok and "nope" in text


def test_input_validation():
    assert _validate("run_node", {"name": "x"}) is None
    assert "missing" in _validate("run_node", {})
    assert "one of" in _validate("write_node", {"name": "a", "kind": "rust", "source": "x"})
    assert "unexpected" in _validate("read_node", {"name": "a", "extra": 1})
    assert "unknown tool" in _validate("rm_rf", {})
