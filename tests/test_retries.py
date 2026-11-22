"""Node run options: ``retries`` / ``retry_delay`` (tried again after a failure,
in the same run) and ``overlap`` (parsed here; scheduling is in test_schedule)."""

import asyncio
import time

import pytest

from exuvia.project import Project
from exuvia.workspace import Workspace

from test_engine import py, run, write


def flaky(tmp_path, name="src", fails=1, kw="retries=2, retry_delay=0"):
    """A node that fails its first ``fails`` attempts, counted in a file."""
    counter = tmp_path / f"{name}.count"
    body = f"""from pathlib import Path
    p = Path({str(counter)!r})
    n = int(p.read_text()) if p.exists() else 0
    p.write_text(str(n + 1))
    if n < {fails}:
        raise RuntimeError("feed is down")
    return pd.DataFrame({{'x': [n]}})"""
    py(tmp_path, name, body=body, kw=kw)
    return counter


def test_run_options_parse_from_python_and_sql(tmp_path):
    py(tmp_path, "a", kw='retries=3, retry_delay=5, overlap="skip"')
    py(tmp_path, "b")
    write(tmp_path, "c.sql", "-- totals\n-- retries: 2\n-- retry_delay: 0.5\n-- overlap: queue\nselect 1 as x\n")
    g = Project(tmp_path).load()
    assert (g.nodes["a"].retries, g.nodes["a"].retry_delay, g.nodes["a"].overlap) == (3, 5.0, "skip")
    assert (g.nodes["b"].retries, g.nodes["b"].retry_delay, g.nodes["b"].overlap) == (0, 30.0, "queue")
    assert (g.nodes["c"].retries, g.nodes["c"].retry_delay, g.nodes["c"].overlap) == (2, 0.5, "queue")
    assert g.nodes["c"].description == "totals"


@pytest.mark.parametrize(
    "kw",
    ["retries=-1", "retries=11", "retries=1.5", "retries=True", "retry_delay=-1", "retry_delay=4000", 'overlap="later"'],
)
def test_bad_run_options_are_parse_errors(tmp_path, kw):
    py(tmp_path, "a", kw=kw)
    assert Project(tmp_path).load().nodes["a"].parse_error


def test_a_failed_attempt_is_retried_in_the_same_run(tmp_path):
    counter = flaky(tmp_path, fails=1)
    ws = Workspace(tmp_path)
    res = run(ws, ["src"])
    assert res["src"]["status"] == "success"
    assert counter.read_text() == "2"
    runs = ws.store.runs_for("src")
    assert len(runs) == 1  # one run, two attempts
    log = ws.store.run(runs[0]["id"])["log"]
    assert "feed is down" in log
    assert "attempt 1 of 3 failed" in log
    assert "attempt 2 of 3" not in log  # the second attempt worked
    assert ws.status()["src"]["state"] == "fresh"


def test_retries_run_out_and_the_run_fails_once(tmp_path):
    counter = flaky(tmp_path, fails=5, kw="retries=1, retry_delay=0")
    py(tmp_path, "down", params="src")
    ws = Workspace(tmp_path)
    reported: list[str] = []
    ws._notify_failure = reported.append  # one notification, for the last attempt
    res = run(ws, ["down"])
    assert res["src"]["status"] == "failed"
    assert "feed is down" in res["src"]["error"]
    assert res["down"]["status"] == "skipped"
    assert counter.read_text() == "2"
    assert reported == [res["src"]["id"]]
    assert ws.store.run(res["src"]["id"])["log"].count("Retrying") == 1


def test_no_retries_by_default(tmp_path):
    counter = flaky(tmp_path, fails=1, kw="")
    ws = Workspace(tmp_path)
    assert run(ws, ["src"])["src"]["status"] == "failed"
    assert counter.read_text() == "1"


def test_cancel_ends_the_wait_to_retry(tmp_path):
    flaky(tmp_path, fails=5, kw="retries=3, retry_delay=600")
    ws = Workspace(tmp_path)

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        task = asyncio.create_task(ws.run(["src"]))
        for _ in range(600):  # until the first attempt has failed
            await asyncio.sleep(0.05)
            runs = ws.store.runs_for("src")
            if runs and runs[0]["id"] in ws._retry_waits:
                break
        run_id = ws.store.runs_for("src")[0]["id"]
        assert ws.store.run(run_id)["status"] == "queued"
        started = time.monotonic()
        # The API calls cancel from a worker thread.
        assert await asyncio.to_thread(ws.cancel, run_id)
        res = await task
        assert time.monotonic() - started < 10  # not the 600 s delay
        return res

    res = asyncio.run(go())
    assert res["src"]["status"] == "failed"
    assert res["src"]["error"] == "cancelled"
    assert (tmp_path / "src.count").read_text() == "1"  # never tried again
    assert not ws._cancelled
