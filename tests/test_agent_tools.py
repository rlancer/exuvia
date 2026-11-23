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


def test_report_issue_drafts_but_sends_nothing(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    agent = Agent(Workspace(tmp_path))
    ok, text, _ = call(agent, "report_issue", {"title": "write_node  fails\non  valid input", "details": "Steps: ...\n\nError: KeyError 'x'"})
    out = json.loads(text)
    assert ok and out["title"] == "write_node fails on valid input"
    assert out["labels"] == ["bug"] and "Error: KeyError 'x'" in out["body"]
    assert "exuvia:" in out["body"] and "Python:" in out["body"]  # environment is added
    assert "NOTHING HAS BEEN SENT" in out["next"] and "gh issue create --repo rlancer/exuvia" in out["next"]

    url = urlsplit(out["url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://github.com/rlancer/exuvia/issues/new"
    query = parse_qs(url.query)
    assert query["title"] == [out["title"]] and query["body"] == [out["body"]] and query["labels"] == ["bug"]

    ok, text, _ = call(agent, "report_issue", {"title": "a chart helper", "details": "d", "kind": "idea"})
    assert json.loads(text)["labels"] == ["enhancement"]
    ok, text, _ = call(agent, "report_issue", {"title": "t", "details": "d", "kind": "feedback"})
    assert json.loads(text)["labels"] == [] and "labels=" not in json.loads(text)["url"]


def test_report_issue_link_stays_within_what_github_accepts(tmp_path):
    agent = Agent(Workspace(tmp_path))
    ok, text, _ = call(agent, "report_issue", {"title": "huge traceback", "details": "line é\n" * 5000})
    out = json.loads(text)
    assert ok and len(out["url"]) <= 7000 and "Shortened" in out["url"].replace("%20", " ")
    assert out["body"].count("line é") == 5000  # the draft itself is whole
    assert "prefer `gh`" in out["next"]


def test_report_issue_validates_its_input(tmp_path):
    agent = Agent(Workspace(tmp_path))
    assert not asyncio.run(agent.call_tool("report_issue", {"title": "x"}))[0]
    ok, text, _ = asyncio.run(agent.call_tool("report_issue", {"title": "x", "details": "y", "kind": "rant"}))
    assert not ok and "must be one of" in text


def test_the_guide_tells_the_agent_where_to_report(tmp_path):
    text = Agent(Workspace(tmp_path)).instructions()
    assert "report_issue" in text and "github.com/rlancer/exuvia" in text


def test_list_packages_can_list_the_whole_environment(tmp_path):
    agent = Agent(Workspace(tmp_path))
    ok, text, _ = call(agent, "list_packages", {})
    assert ok and "environment" not in json.loads(text)

    ok, text, _ = call(agent, "list_packages", {"environment": True})
    env = json.loads(text)["environment"]
    assert ok and env["pandas"] and env["exuvia"] and "duckdb" in env
    assert "INVALID_INPUT" in asyncio.run(agent.call_tool("list_packages", {"environment": "yes"}))[1]


def test_runs_can_be_listed_and_read(tmp_path):
    agent = Agent(Workspace(tmp_path))
    bad = SRC.replace('return pd.DataFrame({"n": [1, 2, 3]})', 'print("about to fail")\n    return undefined_name')
    call(agent, "write_node", {"name": "nums", "kind": "python", "source": bad})
    ok, text, _ = call(agent, "run_node", {"name": "nums"})
    failed_id = json.loads(text)["run_id"]
    call(agent, "write_node", {"name": "nums", "kind": "python", "source": SRC})
    call(agent, "run_node", {"name": "nums"})

    ok, text, extra = call(agent, "list_runs", {"name": "nums"})
    runs = json.loads(text)
    assert ok and extra == {"node": "nums"}
    assert [r["status"] for r in runs] == ["success", "failed"] and runs[1]["id"] == failed_id
    assert runs[0]["finished"].endswith("Z") and "log" not in runs[0]
    ok, text, _ = call(agent, "list_runs", {"limit": 1})
    assert ok and len(json.loads(text)) == 1
    ok, text, _ = call(agent, "list_runs", {"name": "nope"})
    assert not ok and "no node" in text

    ok, text, extra = call(agent, "read_run", {"run_id": failed_id})
    run = json.loads(text)
    assert ok and extra == {"node": "nums"} and run["status"] == "failed" and "NameError" in run["error"]
    assert "about to fail" in run["log_tail"]
    # The code has changed since: the run's own code comes back with it.
    assert run["code_current"] is False and "undefined_name" in run["source"]

    ok, text, _ = call(agent, "read_run", {"run_id": runs[0]["id"]})
    assert ok and json.loads(text)["code_current"] is True and "source" not in json.loads(text)
    ok, text, _ = call(agent, "read_run", {"run_id": "nope"})
    assert not ok and "no run" in text
    assert "INVALID_INPUT" in asyncio.run(agent.call_tool("list_runs", {"limit": 0}))[1]


def test_delete_param(tmp_path):
    agent = Agent(Workspace(tmp_path))
    ok, _, _ = call(agent, "define_param", {"name": "as_of", "type": "date", "default": "today"})
    assert ok and "as_of" in agent.ws.project.params
    reader = SRC.replace("@exuvia.node()", '@exuvia.node(params=["as_of"])')
    call(agent, "write_node", {"name": "nums", "kind": "python", "source": reader})

    ok, text, _ = call(agent, "delete_param", {"name": "as_of"})
    assert ok and json.loads(text) == {"deleted": "as_of", "still_declared_by": ["nums"]}
    assert "as_of" not in agent.ws.project.params
    ok, text, _ = call(agent, "delete_param", {"name": "as_of"})
    assert not ok and "no parameter" in text


def test_viewers_get_the_new_read_tools():
    from exuvia.agent import READ_TOOLS

    assert {"list_runs", "read_run", "list_packages"} <= READ_TOOLS
    assert "delete_param" not in READ_TOOLS
