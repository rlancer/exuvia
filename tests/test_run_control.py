"""Following and stopping runs: run_node that returns while a run goes on,
read_run of a run in progress, cancel_run of a running or queued run, a
node's ``timeout``, and logs that keep what a failed node printed."""

import asyncio
import json
import time
from types import SimpleNamespace

from exuvia import mcp_server
from exuvia.agent import Agent, _validate
from exuvia.executor import exit_reason
from exuvia.project import Project
from exuvia.report import log_tail
from exuvia.workspace import Workspace

from test_engine import py, run, write


def in_loop(agent, steps):
    """Run ``steps(call)`` in one event loop, so runs still going between
    tool calls keep going; ``call(name, args)`` returns (ok, parsed answer)."""

    async def call(name, args):
        ok, text, _ = await agent._call(name, args)
        try:
            return ok, json.loads(text)
        except ValueError:
            return ok, text

    async def go():
        agent.ws.bus.bind(asyncio.get_running_loop())
        return await steps(call)

    return asyncio.run(go())


async def until(check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        await asyncio.sleep(0.1)
    raise AssertionError("timed out waiting")


def waits_for(tmp_path, name="slow", kw=""):
    """A node that prints, then waits for a file named ``go`` (up to 10 min)."""
    gate = tmp_path / "go"
    body = f"""import time
    from pathlib import Path
    print("step 1: started", flush=True)
    for _ in range(6000):
        if Path({str(gate)!r}).exists():
            break
        time.sleep(0.1)
    return pd.DataFrame({{'x': [1]}})"""
    py(tmp_path, name, body=body, kw=kw)
    return gate


def test_what_a_node_printed_survives_a_long_traceback(tmp_path):
    body = """print("step 1", flush=True)
    print("step 2: batch 7")
    # a driver's error, many lines long
    raise RuntimeError("the database hung up" + "".join(f"\\n  detail {i}" for i in range(50)))"""
    py(tmp_path, "src", body=body)
    ws = Workspace(tmp_path)
    res = run(ws, ["src"])
    log = ws.store.run(res["src"]["id"])["log"]
    # printed lines first, in order, then the traceback, which starts at the node's code
    assert log.index("step 1") < log.index("step 2: batch 7") < log.index("Traceback")
    assert "runner.py" not in log and "nodes" in log

    agent = Agent(ws)

    async def steps(call):
        return await call("run_node", {"name": "src"}), await call("read_run", {"run_id": res["src"]["id"], "log_lines": 20})

    (ok, report), (_, read) = in_loop(agent, steps)
    assert not ok and "the database hung up" in report["error"]
    assert "step 2" not in report["log_tail"]  # pushed out by a 50-line error...
    assert report["printed"].splitlines()[-2:] == ["step 1", "step 2: batch 7"]  # ...but kept
    assert "step 2: batch 7" in read["printed"]


def test_log_tail_keeps_printed_lines_only_when_the_traceback_hides_them():
    short = "a\nb\nTraceback (most recent call last):\n  x\nValueError: no\n"
    assert log_tail(short) == {"log_tail": short.rstrip("\n")}
    frames = [f'  File "f.py", line {i}' for i in range(50)]
    chained = "\n".join(
        ["setup", "fetching batch 3", "Traceback (most recent call last):", *frames, "KeyError: 'x'", "",
         "During handling of the above exception, another exception occurred:", "",
         "Traceback (most recent call last):", *frames, "RuntimeError: gave up"]
    )
    out = log_tail(chained)
    assert out["printed"] == "setup\nfetching batch 3"  # before the first traceback of the chain
    assert out["log_tail"].endswith("RuntimeError: gave up")
    assert "printed" not in log_tail("Traceback (most recent call last):\n" + "\n".join(frames))


def test_run_node_returns_while_the_run_goes_on_and_read_run_follows_it(tmp_path):
    gate = waits_for(tmp_path)
    agent = Agent(Workspace(tmp_path))

    async def steps(call):
        ok, started = await call("run_node", {"name": "slow", "wait_seconds": 0})
        assert ok and started["status"] == "running" and started["runs"] == {"slow": started["runs"]["slow"]}
        run_id = started["run_id"]
        # its log so far, while it runs
        async def printed():
            _, run = await call("read_run", {"run_id": run_id})
            return run if "step 1: started" in run.get("log_tail", "") else None
        seen = await until_async(printed)
        assert seen["status"] == "running"
        ok, again = await call("run_node", {"name": "slow", "wait_seconds": 1})
        assert ok and again["status"] == "running" and again["run_id"] != run_id  # queued behind it
        gate.write_text("go")
        ok, done = await call("read_run", {"run_id": run_id, "wait_seconds": 60})
        assert ok and done["status"] == "success"
        ok, second = await call("read_run", {"run_id": again["run_id"], "wait_seconds": 60})
        assert ok and second["status"] == "success"

    in_loop(agent, steps)


async def until_async(check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.1)
    raise AssertionError("timed out waiting")


def test_cancel_run_stops_a_running_run_and_one_queued_behind_it(tmp_path):
    waits_for(tmp_path)  # never released: only cancelling ends it
    ws = Workspace(tmp_path)
    agent = Agent(ws)
    sent = []
    ws._notify_failure = sent.append  # a cancelled run is not a failure to tell anyone about

    async def steps(call):
        _, first = await call("run_node", {"name": "slow", "wait_seconds": 0})
        await until(lambda: ws.store.run(first["run_id"])["status"] == "running")
        _, second = await call("run_node", {"name": "slow", "wait_seconds": 0})
        assert ws.store.run(second["run_id"])["status"] == "queued"  # waiting for the node's lock

        started = time.monotonic()
        ok, out = await call("cancel_run", {"run_id": second["run_id"]})
        assert ok and out["status"] == "failed" and out["error"] == "cancelled"
        assert ws.store.run(first["run_id"])["status"] == "running"  # the one ahead goes on

        ok, out = await call("cancel_run", {"run_id": first["run_id"]})
        assert ok and out["status"] == "failed" and out["error"] == "cancelled"
        assert time.monotonic() - started < 20

        ok, text = await call("cancel_run", {"run_id": first["run_id"]})
        assert not ok and "already ended" in text
        ok, text = await call("cancel_run", {"run_id": "nope"})
        assert not ok and "no run" in text

    in_loop(agent, steps)
    assert sent == [] and not ws._cancelled and not ws._run_tasks


def test_cancelling_a_queued_run_skips_what_waits_for_it_but_not_what_it_waits_for(tmp_path):
    gate = waits_for(tmp_path, "up")
    py(tmp_path, "down", params="up", body="return up")
    ws = Workspace(tmp_path)

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        ids = {}
        task = asyncio.create_task(ws.run(["down"], force={"up", "down"}, on_queued=ids.update))
        await until(lambda: ids and ws.store.run(ids["up"])["status"] == "running")
        assert await asyncio.to_thread(ws.cancel, ids["down"])
        await until(lambda: ws.store.run(ids["down"])["status"] == "failed")
        assert ws.store.run(ids["up"])["status"] == "running"
        gate.write_text("go")
        return await task

    res = asyncio.run(go())
    assert res["up"]["status"] == "success"
    assert (res["down"]["status"], res["down"]["error"]) == ("failed", "cancelled")


def test_a_node_timeout_stops_a_hung_attempt(tmp_path):
    waits_for(tmp_path, kw="timeout=8")
    ws = Workspace(tmp_path)
    started = time.monotonic()
    res = run(ws, ["slow"])["slow"]
    assert time.monotonic() - started < 60
    assert res["status"] == "failed"
    assert res["error"] == "timed out after 8s (the node's timeout)"
    assert "the attempt was stopped" in ws.store.run(res["id"])["log"]


def test_timeout_parses_from_python_and_sql(tmp_path):
    py(tmp_path, "a", kw="timeout=300")
    py(tmp_path, "b")
    py(tmp_path, "bad", kw="timeout=0")
    write(tmp_path, "c.sql", "-- timeout: 2.5\nselect 1 as x\n")
    g = Project(tmp_path).load()
    assert (g.nodes["a"].timeout, g.nodes["b"].timeout, g.nodes["c"].timeout) == (300.0, None, 2.5)
    assert "timeout" in g.nodes["bad"].parse_error


def test_a_killed_runner_says_why():
    assert "out of memory" in exit_reason(-9) and "SIGKILL" in exit_reason(-9)
    assert exit_reason(3) == "runner exited with code 3"


def test_wait_seconds_may_be_zero_but_not_negative():
    assert _validate("run_node", {"name": "x", "wait_seconds": 0}) is None
    assert "from 0" in _validate("run_node", {"name": "x", "wait_seconds": -1})
    assert _validate("read_run", {"run_id": "x", "wait_seconds": 30}) is None
    assert "positive" in _validate("list_runs", {"limit": 0})


def test_a_long_tool_call_sends_progress(monkeypatch):
    monkeypatch.setattr(mcp_server, "HEARTBEAT_S", 0.05)
    beats = []

    class Session:
        async def report_progress(self, progress, total, message):
            beats.append(progress)

    async def slow():
        await asyncio.sleep(0.3)
        return True, "done"

    out = asyncio.run(mcp_server._with_heartbeat(SimpleNamespace(session=Session()), slow()))
    assert out == (True, "done") and len(beats) >= 3 and beats[:2] == [1, 2]


def test_a_heartbeat_that_cannot_be_sent_does_not_fail_the_call(monkeypatch):
    monkeypatch.setattr(mcp_server, "HEARTBEAT_S", 0.05)

    class Session:
        async def report_progress(self, *a):
            raise RuntimeError("client gone")

    async def slow():
        await asyncio.sleep(0.2)
        return True, "done"

    assert asyncio.run(mcp_server._with_heartbeat(SimpleNamespace(session=Session()), slow())) == (True, "done")
