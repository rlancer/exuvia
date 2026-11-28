"""A run's log is capped (EXUVIA_MAX_LOG_KB): head as it streams, tail at the end."""

import asyncio

from test_engine import py, run

from exuvia import executor
from exuvia.executor import CappedLog


def capture(limit):
    out: list[str] = []
    return out, CappedLog(out.append, limit=limit)


def test_under_the_limit_passes_through_untouched():
    out, log = capture(1000)
    for i in range(10):
        log(f"line {i}\n")
    log.close()
    assert out == [f"line {i}\n" for i in range(10)]
    assert not log.truncated


def test_over_the_limit_keeps_head_and_tail_with_markers():
    out, log = capture(4000)  # head 3000 bytes, tail 1000 bytes
    lines = [f"line {i:05d}\n" for i in range(5000)]  # 11 bytes each, ~55 KB
    for line in lines:
        log(line)
    live = "".join(out)
    # while running: the head, then one marker, then nothing more
    assert live.startswith(lines[0])
    assert live.count("[exuvia] log truncated") == 1
    assert len(live.encode()) < 3000 + 200
    assert lines[-1] not in live

    log.close()
    text = "".join(out)
    assert text.count("[exuvia] log truncated") == 2
    _, rest = text.split("(EXUVIA_MAX_LOG_KB)\n", 1)
    marker, tail = rest.split("\n", 1)
    assert "dropped" in marker
    assert tail.endswith(lines[-1]) and tail.startswith("line ")  # cut on a line boundary
    assert len(tail.encode()) <= 1000
    # the total stays near the limit, and every byte is kept or counted as dropped
    assert len(text.encode()) < 4000 + 400
    assert log.size + log.dropped + len(tail.encode()) == sum(len(x) for x in lines)


def test_one_huge_chunk_is_cut_and_close_is_idempotent():
    out, log = capture(1000)
    log("x" * 100_000)
    log.close()
    log.close()
    text = "".join(out)
    assert text.count("[exuvia] log truncated") == 2
    assert len(text.encode()) < 1000 + 400
    assert log.dropped == 100_000 - 1000


def test_zero_disables_the_cap():
    out, log = capture(0)
    log("y" * 50_000)
    log.close()
    assert out == ["y" * 50_000]


def test_runaway_node_log_is_capped_and_error_kept_in_full(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "MAX_LOG_BYTES", 8 * 1024)
    long_msg = "z" * 20_000  # longer than the log cap: `error` is not capped
    py(
        tmp_path,
        "noisy",
        body=f"for i in range(20000):\n        print('spam', i)\n    raise ValueError('{long_msg}')",
    )
    from exuvia.workspace import Workspace

    ws = Workspace(tmp_path)
    events: list[dict] = []

    async def go():
        ws.bus.bind(asyncio.get_running_loop())
        q = ws.bus.subscribe()
        await ws.run(["noisy"])
        while not q.empty():
            events.append(q.get_nowait())

    asyncio.run(go())
    rid = ws.store.recent_runs(1)[0]["id"]
    r = ws.store.run(rid)
    assert r["status"] == "failed"
    assert long_msg in r["error"]
    log = r["log"]
    assert len(log.encode()) < 8 * 1024 + 500
    assert log.startswith("spam 0")
    assert "log truncated" in log and "dropped" in log
    # the tail survives (here only the end of the huge message fits in it)
    assert log.rstrip().endswith("z" * 100)
    streamed = "".join(e["text"] for e in events if e.get("type") == "log" and e["run_id"] == rid)
    assert streamed == log  # the UI got exactly what was stored
    ws.executor.close()


def test_small_logs_are_unchanged_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "MAX_LOG_BYTES", 64 * 1024)
    py(tmp_path, "quiet", body="print('hello')\n    return pd.DataFrame({'x': [1]})")
    from exuvia.workspace import Workspace

    ws = Workspace(tmp_path)
    finals = run(ws, ["quiet"])
    log = ws.store.run(finals["quiet"]["id"])["log"]
    assert "hello" in log and "truncated" not in log
    ws.executor.close()


def test_each_attempt_is_capped_and_exuvias_own_lines_are_kept(tmp_path, monkeypatch):
    """A retried run gets a cap per attempt, and what exuvia writes after an
    attempt's output is closed (the retry note) is never swallowed by it."""
    monkeypatch.setattr(executor, "MAX_LOG_BYTES", 8 * 1024)
    py(
        tmp_path,
        "noisy",
        body="for i in range(20000):\n        print('spam', i)\n    raise RuntimeError('noisy boom')",
        kw="retries=1, retry_delay=0",
    )
    from exuvia.workspace import Workspace

    ws = Workspace(tmp_path)
    finals = run(ws, ["noisy"])
    log = ws.store.run(finals["noisy"]["id"])["log"]
    assert finals["noisy"]["status"] == "failed"
    assert "attempt 1 of 2 failed" in log
    assert log.count("dropped; the last part follows") == 2  # one tail per attempt
    assert log.count("noisy boom") >= 2  # each attempt's traceback
    assert len(log.encode()) < 2 * (8 * 1024 + 500)
    ws.executor.close()


def test_writes_from_several_threads_are_counted_together():
    import threading

    out, log = capture(4000)
    threads = [threading.Thread(target=lambda: [log("x" * 10 + "\n") for _ in range(2000)]) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.close()
    tail = "".join(out).split("the last part follows\n", 1)[1]
    assert log.size + log.dropped + len(tail.encode()) == 4 * 2000 * 11
