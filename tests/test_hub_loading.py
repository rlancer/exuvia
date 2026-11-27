"""How a hub loads its projects: one at a time per project, never behind
another project's load, with one pool of warm runners for all of them, and
with file watchers that hold none of the threads the server's sync routes
and streamed answers share."""

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio.to_thread

from exuvia import pool as pool_mod
from exuvia.hub import Hub
from exuvia.workspace import Workspace

NODE = '''import exuvia


@exuvia.node()
def nums():
    import pandas as pd

    return pd.DataFrame({"n": [1, 2, 3]})
'''


def make_hub(tmp_path: Path, *ids: str) -> Hub:
    root = tmp_path / "hub"
    for pid in ids:
        (root / pid / "nodes").mkdir(parents=True)
        (root / pid / "nodes" / "nums.py").write_text(NODE, encoding="utf-8")
    return Hub(root)


def test_a_slow_load_holds_up_neither_loaded_projects_nor_other_loads(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, "a", "b", "c")
    release = asyncio.Event()
    start = Workspace.start

    async def slow_start(self):
        if self.project.root.name == "b":
            await release.wait()
        await start(self)

    monkeypatch.setattr(Workspace, "start", slow_start)

    async def go():
        a = await hub.get("a")
        loading_b = asyncio.create_task(hub.get("b"))
        await asyncio.sleep(0.05)
        assert not loading_b.done()
        assert await asyncio.wait_for(hub.get("a"), 1) is a  # loaded: answered at once
        c = await asyncio.wait_for(hub.get("c"), 5)  # another project loads meanwhile
        again = asyncio.create_task(hub.get("b"))  # a second request for b waits for the same load
        release.set()
        b = await loading_b
        assert await again is b and c is not b
        await hub.close()

    asyncio.run(go())


def test_delete_waits_for_the_projects_load(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, "a")
    release = asyncio.Event()
    start = Workspace.start

    async def slow_start(self):
        await release.wait()
        await start(self)

    monkeypatch.setattr(Workspace, "start", slow_start)

    async def go():
        loading = asyncio.create_task(hub.get("a"))
        await asyncio.sleep(0.05)
        deleting = asyncio.create_task(hub.delete("a"))
        await asyncio.sleep(0.05)
        assert not deleting.done()  # it waits for the load, then unloads what it loaded
        release.set()
        ctx = await loading
        await deleting
        assert "a" not in hub._ctxs and not hub.exists("a")
        assert ctx.ws._tasks == []  # the loaded workspace was stopped
        await hub.close()

    asyncio.run(go())


def test_hub_projects_share_the_hubs_warm_runners(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(pool_mod.WarmPool, "start", lambda self: started.append(self))
    hub = make_hub(tmp_path, "a", "b")

    async def go():
        a, b = await hub.get("a"), await hub.get("b")
        assert a.ws.executor.pool is hub.pool and b.ws.executor.pool is hub.pool
        assert started == [hub.pool]  # warmed once, for both
        # Its runners start in a folder of their own, not the hub's (where
        # a project named like a library would shadow it).
        assert hub.pool.cwd == hub.root / ".exuvia" / "runners"
        await hub.close()

    asyncio.run(go())

    # A project served on its own still warms a pool of its own as it loads.
    async def alone():
        ws = Workspace(tmp_path / "alone")
        await ws.start()
        assert started[1:] == [ws.executor.pool] and ws.executor.pool is not hub.pool
        await ws.stop()
        ws.store.close()

    asyncio.run(alone())


def wait_for(cond, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, "timed out"
        time.sleep(0.05)


def sleeper(self) -> subprocess.Popen:
    """A stand-in runner: a process that waits, with no imports to pay for."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)


def test_a_project_stopping_leaves_the_shared_runners_and_the_hub_closing_ends_them(tmp_path, monkeypatch):
    monkeypatch.setattr(pool_mod.WarmPool, "_spawn", sleeper)
    hub = make_hub(tmp_path, "a", "b")
    hub.pool.size = 2  # tests run with empty pools (conftest.py)

    async def go():
        await hub.get("a")
        await hub.get("b")
        wait_for(lambda: hub.pool.idle() == 2)
        warm = list(hub.pool._idle)
        await hub.delete("a")  # stops a's workspace and closes its executor
        await hub._unload("b")
        assert hub.pool.idle() == 2 and all(p.poll() is None for p in warm)
        assert not hub.pool._closed
        await hub.close()
        assert hub.pool._closed
        for p in warm:
            p.wait(10)  # killed

    asyncio.run(go())


def test_a_burst_of_takes_refills_the_pool_to_its_size_not_beyond(tmp_path, monkeypatch):
    monkeypatch.setattr(pool_mod.WarmPool, "_spawn", sleeper)
    pool = pool_mod.WarmPool(tmp_path, 0)
    pool.size = 2
    pool.start()
    wait_for(lambda: pool.idle() == 2)
    taken = []
    threads = [threading.Thread(target=lambda: taken.append(pool.take())) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wait_for(lambda: not pool._refilling)
    try:
        assert len(taken) == 12 and pool.idle() == 2
    finally:
        pool.close()
        for p in taken:
            pool_mod.kill(p)
            p.wait(10)


def test_file_watchers_hold_none_of_anyios_threads(tmp_path):
    """anyio's default limiter (40 threads) runs every sync route and every
    streamed answer's iterator. A watcher on it held a token for up to 5
    seconds at a time, so with 40 projects loaded each of those waited for
    one to run out."""
    hub = make_hub(tmp_path, *(f"p{i}" for i in range(3)))

    async def go():
        for pid in ("p0", "p1", "p2"):
            await hub.get(pid)
        await asyncio.sleep(0.3)  # the watchers are watching
        assert anyio.to_thread.current_default_thread_limiter().borrowed_tokens == 0
        # ... and still see hand edits.
        ws = (await hub.get("p0")).ws
        (ws.project.nodes_dir / "more.py").write_text(NODE.replace("nums", "more"), encoding="utf-8")
        deadline = time.time() + 20
        while "more" not in ws.graph.nodes and time.time() < deadline:
            await asyncio.sleep(0.1)
        assert "more" in ws.graph.nodes
        assert ws.code_revisions(node="more")[0]["origin"] == "file"
        await hub.close()
        assert ws._tasks == []

    asyncio.run(go())
