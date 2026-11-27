"""Load test for the data API (exuvia/dataapi.py).

Seeds a hub of projects with data of a realistic size, starts the hub as its
own process (``exuvia hub``) and drives ``/api/data/`` with concurrent clients,
one scenario after another. Each scenario reports requests per second,
latency percentiles, errors and the hub process's memory (RSS) and threads.

    uv run python scripts/loadtest_data_api.py                 # seed and run
    uv run python scripts/loadtest_data_api.py --hub /tmp/lt   # keep the hub: later runs skip seeding
    uv run python scripts/loadtest_data_api.py --only hot_query,churn_query

The seeded hub has ``--projects`` projects, each with

* ``orders``: ``--rows`` rows (account, region, product, amount, day);
* ``accounts``: 10,000 rows; ``products``: 500 rows;
* ``by_region.sql``: a join and aggregate over them;
* ``daily``: a node with an ``as_of`` parameter, run for ``--history`` dates,
  so ``daily__runs`` is a view over that many files;
* ``--small`` small nodes, as real projects have.

Nobody signs in to the hub, so data keys are the operator's. Linux only (RSS
is read from /proc). ``--json FILE`` writes the results for comparing runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

ARROW = {"Accept": "application/vnd.apache.arrow.stream"}

ORDERS = '''import exuvia


@exuvia.node()
def orders():
    """One row per order line."""
    import numpy as np
    import pandas as pd

    n, rng = {rows}, np.random.default_rng({seed})
    return pd.DataFrame({{
        "order_id": np.arange(n, dtype="int64"),
        "account": rng.integers(0, 10_000, n),
        "product": rng.integers(0, 500, n),
        "region": rng.choice(["na", "emea", "apac", "latam"], n),
        "amount": rng.gamma(2.0, 40.0, n).round(2),
        "day": pd.Timestamp("2025-01-01") + pd.to_timedelta(rng.integers(0, 365, n), unit="D"),
    }})
'''

ACCOUNTS = '''import exuvia


@exuvia.node()
def accounts():
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng({seed})
    return pd.DataFrame({{
        "account": np.arange(10_000),
        "name": [f"account {{i}}" for i in range(10_000)],
        "segment": rng.choice(["smb", "mid", "enterprise"], 10_000),
    }})
'''

PRODUCTS = '''import exuvia


@exuvia.node()
def products():
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng({seed})
    return pd.DataFrame({{
        "product": np.arange(500),
        "category": rng.choice(["hardware", "software", "services"], 500),
        "list_price": rng.uniform(5, 500, 500).round(2),
    }})
'''

BY_REGION = """select o.region, a.segment, p.category, count(*) as orders, sum(o.amount) as revenue
from orders o join accounts a using (account) join products p using (product)
group by all
"""

DAILY = '''import exuvia


@exuvia.node(params=["as_of"])
def daily():
    """A snapshot per run date."""
    import datetime

    import numpy as np
    import pandas as pd

    as_of = str(exuvia.param("as_of"))
    rng = np.random.default_rng(datetime.date.fromisoformat(as_of[:10]).toordinal())
    return pd.DataFrame({
        "as_of": [as_of] * 2_000,
        "account": np.arange(2_000),
        "balance": rng.normal(1_000, 250, 2_000).round(2),
    })
'''


SMALL = '''import exuvia


@exuvia.node()
def {name}():
    import pandas as pd

    return pd.DataFrame({{"k": list(range(50)), "v": [i * {i} for i in range(50)]}})
'''

TICK = '''import exuvia


@exuvia.node()
def tick():
    """Re-run during churn_query: each run changes the project."""
    import time

    import pandas as pd

    return pd.DataFrame({"t": [time.time()]})
'''


def pid_of(i: int) -> str:
    return f"p{i:03d}"


def history_dates(n: int) -> list[str]:
    from datetime import date, timedelta

    start = date(2026, 1, 1)
    return [(start + timedelta(days=d)).isoformat() for d in range(n)]


def write_project(root: Path, i: int, rows: int, small: int, history: int) -> None:
    proj = root / pid_of(i)
    nodes = proj / "nodes"
    nodes.mkdir(parents=True, exist_ok=True)
    (nodes / "orders.py").write_text(ORDERS.format(rows=rows, seed=i))
    (nodes / "accounts.py").write_text(ACCOUNTS.format(seed=i))
    (nodes / "products.py").write_text(PRODUCTS.format(seed=i))
    (nodes / "by_region.sql").write_text(BY_REGION)
    (nodes / "daily.py").write_text(DAILY)
    (nodes / "tick.py").write_text(TICK)
    for s in range(small):
        (nodes / f"small_{s}.py").write_text(SMALL.format(name=f"small_{s}", i=s))
    default = history_dates(history)[-1]
    cfg = {"name": f"Project {i}", "params": {"as_of": {"type": "date", "default": default}}}
    (proj / "exuvia.json").write_text(json.dumps(cfg, indent=2))


# -- the hub process ------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HubProcess:
    def __init__(self, root: Path, env: dict[str, str]):
        self.root = root
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        base = dict(os.environ)
        for name in ("EXUVIA_AUTH", "EXUVIA_API_TOKEN", "EXUVIA_BROWSER_AUTH", "EXUVIA_DB", "EXUVIA_ARTIFACT_STORE"):
            base.pop(name, None)  # nobody signs in, outputs on local disk: unless `env` says otherwise
        env = {**base, **env}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "exuvia.cli", "hub", str(root), "--port", str(self.port)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(f"the hub exited: {self.proc.stderr.read() if self.proc.stderr else ''}")
            try:
                if httpx.get(self.url + "/api/projects", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise SystemExit("the hub did not start in 60s")

    def stat(self) -> tuple[float, int]:
        """(RSS in MiB, threads) of the hub process."""
        rss, threads = 0.0, 0
        try:
            for line in Path(f"/proc/{self.proc.pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) / 1024
                elif line.startswith("Threads:"):
                    threads = int(line.split()[1])
        except OSError:
            pass
        return rss, threads

    def stop(self) -> None:
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Sampler:
    """Peak RSS and threads of the hub while a scenario runs."""

    def __init__(self, hub: HubProcess):
        self.hub, self.peak_rss, self.peak_threads = hub, 0.0, 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            rss, threads = self.hub.stat()
            self.peak_rss, self.peak_threads = max(self.peak_rss, rss), max(self.peak_threads, threads)
            self._stop.wait(0.05)

    def __enter__(self) -> "Sampler":
        self._t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._t.join()


# -- seeding ----------------------------------------------------------------------


async def seed(hub: HubProcess, projects: int, history: int) -> None:
    dates = history_dates(history)
    sem = asyncio.Semaphore(4)
    done = 0
    t0 = time.time()

    async with httpx.AsyncClient(base_url=hub.url, timeout=600) as c:

        async def one(i: int) -> None:
            nonlocal done
            pid = pid_of(i)
            async with sem:
                r = await c.post(f"/p/{pid}/api/run", json={"wait": 600})
                r.raise_for_status()
                for d in dates:
                    r = await c.post(f"/p/{pid}/api/nodes/daily/run", json={"params": {"as_of": d}, "wait": 120})
                    r.raise_for_status()
                    if not r.json().get("ok"):
                        raise SystemExit(f"{pid} daily {d}: {r.text[:500]}")
            done += 1
            print(f"  seeded {pid} ({done}/{projects}, {time.time() - t0:.0f}s)", flush=True)

        await asyncio.gather(*(one(i) for i in range(projects)))


async def make_key(hub: HubProcess, projects: list[str], name: str) -> str:
    async with httpx.AsyncClient(base_url=hub.url, timeout=60) as c:
        r = await c.post("/api/data-keys", json={"name": name, "projects": projects})
        r.raise_for_status()
        return r.json()["key"]


# -- measuring --------------------------------------------------------------------


@dataclass
class Result:
    name: str
    concurrency: int
    seconds: float
    latencies: list[float] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)
    throttled: int = 0  # 429s: the key had as many requests running as it may
    peak_rss: float = 0.0
    peak_threads: int = 0
    rss_after: float = 0.0
    note: str = ""

    def row(self) -> dict[str, Any]:
        lat = sorted(self.latencies)

        def pct(p: float) -> float | None:
            return round(lat[min(len(lat) - 1, int(p * len(lat)))] * 1000, 1) if lat else None

        return {
            "scenario": self.name, "conc": self.concurrency, "ok": len(lat),
            "err": sum(self.errors.values()), "429": self.throttled, "rps": round(len(lat) / self.seconds, 1) if self.seconds else 0,
            "p50_ms": pct(0.5), "p95_ms": pct(0.95), "p99_ms": pct(0.99),
            "max_ms": round(lat[-1] * 1000, 1) if lat else None,
            "mean_ms": round(statistics.mean(lat) * 1000, 1) if lat else None,
            "peak_rss_mb": round(self.peak_rss), "rss_after_mb": round(self.rss_after),
            "peak_threads": self.peak_threads, "errors": self.errors, "note": self.note,
        }


Request = Callable[[httpx.AsyncClient, int], Awaitable[httpx.Response]]


async def drained(c: httpx.AsyncClient, method: str, url: str, **kw: Any) -> httpx.Response:
    """A request whose body is read and thrown away as it arrives: a client
    holding every large answer at once would run out of memory before the hub."""
    r = await c.send(c.build_request(method, url, **kw), stream=True)
    try:
        if r.status_code != 200:
            await r.aread()
        else:
            async for _ in r.aiter_raw():
                pass
    finally:
        await r.aclose()
    return r


async def drive(
    hub: HubProcess, name: str, request: Request, concurrency: int, duration: float,
    max_requests: int | None = None, background: Callable[[httpx.AsyncClient, asyncio.Event], Awaitable[None]] | None = None,
    timeout: float = 120,
) -> Result:
    """``concurrency`` clients each sending ``request`` back to back for
    ``duration`` seconds (or until ``max_requests`` in all)."""
    res = Result(name, concurrency, 0.0)
    stop = asyncio.Event()
    sent = 0
    limits = httpx.Limits(max_connections=concurrency + 4, max_keepalive_connections=concurrency + 4)
    async with httpx.AsyncClient(base_url=hub.url, timeout=timeout, limits=limits) as c:
        bg = asyncio.create_task(background(c, stop)) if background else None

        async def client(n: int) -> None:
            nonlocal sent
            i = n
            while not stop.is_set():
                if max_requests is not None:
                    if sent >= max_requests:
                        return
                    sent += 1
                t = time.perf_counter()
                try:
                    r = await request(c, i)
                    if r.status_code == 429:
                        res.throttled += 1
                        await asyncio.sleep(0.05)
                    elif r.status_code != 200:
                        key = f"HTTP {r.status_code}: {r.text[:120]}"
                        res.errors[key] = res.errors.get(key, 0) + 1
                    else:
                        res.latencies.append(time.perf_counter() - t)
                except httpx.HTTPError as e:
                    key = type(e).__name__
                    res.errors[key] = res.errors.get(key, 0) + 1
                i += concurrency

        with Sampler(hub) as s:
            t0 = time.perf_counter()
            clients = [asyncio.create_task(client(n)) for n in range(concurrency)]
            if max_requests is None:
                await asyncio.sleep(duration)
                stop.set()
            await asyncio.gather(*clients)
            res.seconds = time.perf_counter() - t0
            stop.set()
            if bg:
                await bg
        res.peak_rss, res.peak_threads = s.peak_rss, s.peak_threads
    res.rss_after = hub.stat()[0]
    return res


def print_row(r: dict[str, Any]) -> None:
    errs = f"  errors: {r['errors']}" if r["errors"] else ""
    note = f"  ({r['note']})" if r["note"] else ""
    print(
        f"{r['scenario']:<16} c={r['conc']:<3} ok={r['ok']:<6} err={r['err']:<4} 429={r['429']:<5} rps={r['rps']:<8} "
        f"p50={r['p50_ms']}ms p95={r['p95_ms']}ms p99={r['p99_ms']}ms max={r['max_ms']}ms "
        f"rss peak/after={r['peak_rss_mb']}/{r['rss_after_mb']}MiB threads={r['peak_threads']}{note}{errs}",
        flush=True,
    )


# -- scenarios ----------------------------------------------------------------------


async def run_scenarios(args: argparse.Namespace, root: Path, env: dict[str, str]) -> list[dict[str, Any]]:
    pids = [pid_of(i) for i in range(args.projects)]
    out: list[dict[str, Any]] = []
    only = set(args.only.split(",")) if args.only else None
    want = lambda name: only is None or name in only  # noqa: E731
    conc = [int(c) for c in args.concurrency.split(",")]

    def record(res: Result) -> None:
        row = res.row()
        print_row(row)
        out.append(row)

    # A fresh hub per group of scenarios that needs nothing loaded yet.
    hub = HubProcess(root, env)
    try:
        all_key = await make_key(hub, pids, "all")
        two_key = await make_key(hub, pids[:2], "two")
        other_key = await make_key(hub, pids[:2], "another app")
        auth_all = {"Authorization": f"Bearer {all_key}"}
        auth_two = {"Authorization": f"Bearer {two_key}"}
        auth_other = {"Authorization": f"Bearer {other_key}"}
        rss0 = hub.stat()[0]
        print(f"hub started: rss={rss0:.0f}MiB, {len(pids)} projects", flush=True)

        if want("cold_list"):
            # One request for every project's nodes, nothing loaded yet.
            res = await drive(hub, "cold_list", lambda c, i: c.get("/api/data/projects", headers=auth_all), 1, 0, max_requests=1, timeout=600)
            res.note = f"{len(pids)} projects; rss before {rss0:.0f}MiB"
            record(res)

        if want("warm_list"):
            for n in conc:
                record(await drive(hub, "warm_list", lambda c, i: c.get("/api/data/projects", headers=auth_all), n, args.duration))

        two = f'"{pids[0]}"', f'"{pids[1]}"'
        hot_sql = (
            f"select o.region, sum(o.amount) as revenue, count(*) as n from {two[0]}.orders o "
            f"join {two[1]}.accounts a using (account) where a.segment = 'enterprise' group by all"
        )
        if want("hot_query"):
            for n in conc:
                record(await drive(
                    hub, "hot_query", lambda c, i: c.post("/api/data/query", json={"sql": hot_sql}, headers=auth_two),
                    n, args.duration,
                ))

        if want("point_query"):
            # A cheap query: the cost is the request path, not DuckDB.
            sql = f"select * from {two[0]}.accounts where account = 42"
            for n in conc:
                record(await drive(hub, "point_query", lambda c, i: c.post("/api/data/query", json={"sql": sql}, headers=auth_two), n, args.duration))

        if want("node_read"):
            for n in conc:
                record(await drive(
                    hub, "node_read",
                    lambda c, i: c.get(f"/api/data/projects/{pids[i % 2]}/nodes/accounts?limit=100", headers=auth_two),
                    n, args.duration,
                ))

        if want("history"):
            sql = f"select as_of, avg(balance) from {two[0]}.daily__runs group by all order by 1"
            for n in conc:
                record(await drive(hub, "history", lambda c, i: c.post("/api/data/query", json={"sql": sql}, headers=auth_two), n, args.duration))

        if want("churn_query"):
            # The same query while one of its projects runs a node again and
            # again: every run invalidates the shared connection.
            runs = 0

            async def churn(c: httpx.AsyncClient, stop: asyncio.Event) -> None:
                nonlocal runs
                while not stop.is_set():
                    r = await c.post(f"/p/{pids[0]}/api/nodes/tick/run", json={"wait": 60})
                    runs += r.status_code == 200
                    await asyncio.sleep(args.churn_every)

            for n in conc:
                runs = 0
                res = await drive(
                    hub, "churn_query", lambda c, i: c.post("/api/data/query", json={"sql": hot_sql}, headers=auth_two),
                    n, args.duration, background=churn,
                )
                res.note = f"{runs} runs during it"
                record(res)

        if want("distinct_sets"):
            # Each request a different pair of projects: more sets than the
            # connection cache keeps.
            pairs = [(a, b) for a in pids for b in pids if a < b]
            random.Random(1).shuffle(pairs)

            def req(c: httpx.AsyncClient, i: int) -> Awaitable[httpx.Response]:
                a, b = pairs[i % len(pairs)]
                sql = f'select count(*) from "{a}".orders o join "{b}".products p using (product) where p.category = \'software\''
                return c.post("/api/data/query", json={"sql": sql}, headers=auth_all)

            for n in conc:
                res = await drive(hub, "distinct_sets", req, n, args.duration)
                res.note = f"{len(pairs)} pairs"
                record(res)

        if want("arrow_scan"):
            # Every row of a large table, as Arrow, no limit.
            sql = f"select * from {two[0]}.orders"
            for n in conc:
                record(await drive(
                    hub, "arrow_scan",
                    lambda c, i: drained(c, "POST", "/api/data/query", json={"sql": sql}, headers={**auth_two, **ARROW}),
                    n, args.duration, timeout=300,
                ))

        if want("heavy_query"):
            # Queries heavy enough to keep DuckDB busy, from one key: do they
            # starve a cheap one another app sends at the same time?
            heavy = (
                f"select a.account, b.account, count(*) from {two[0]}.orders a join {two[1]}.orders b "
                f"on a.product = b.product and a.day = b.day group by all order by 3 desc limit 10"
            )
            cheap = f"select * from {two[0]}.accounts where account = 42"
            cheap_res = Result("heavy_query:cheap", 1, 0.0)

            async def cheap_loop(c: httpx.AsyncClient, stop: asyncio.Event) -> None:
                while not stop.is_set():
                    t = time.perf_counter()
                    try:
                        r = await c.post("/api/data/query", json={"sql": cheap}, headers=auth_other, timeout=600)
                        if r.status_code == 200:
                            cheap_res.latencies.append(time.perf_counter() - t)
                        else:
                            cheap_res.errors[str(r.status_code)] = cheap_res.errors.get(str(r.status_code), 0) + 1
                    except httpx.HTTPError as e:
                        cheap_res.errors[type(e).__name__] = cheap_res.errors.get(type(e).__name__, 0) + 1
                    await asyncio.sleep(0.1)

            n = max(conc)
            t0 = time.perf_counter()
            res = await drive(
                hub, "heavy_query", lambda c, i: c.post("/api/data/query", json={"sql": heavy}, headers=auth_two),
                n, args.duration, background=cheap_loop, timeout=600,
            )
            cheap_res.seconds = time.perf_counter() - t0
            record(res)
            record(cheap_res)
    finally:
        hub.stop()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hub", help="the hub folder: seeded once and reused (default: a temporary folder)")
    ap.add_argument("--projects", type=int, default=40)
    ap.add_argument("--rows", type=int, default=1_000_000, help="rows of each project's orders")
    ap.add_argument("--small", type=int, default=6, help="small nodes per project")
    ap.add_argument("--history", type=int, default=30, help="runs of each project's parameterized node")
    ap.add_argument("--duration", type=float, default=10.0, help="seconds per scenario and concurrency")
    ap.add_argument("--concurrency", default="1,8,32", help="clients at once, per scenario")
    ap.add_argument("--churn-every", type=float, default=0.25, help="seconds between runs in churn_query")
    ap.add_argument("--only", help="comma-separated scenarios: cold_list, warm_list, hot_query, point_query, "
                    "node_read, history, churn_query, distinct_sets, arrow_scan, heavy_query")
    ap.add_argument("--json", help="write the results to this file")
    args = ap.parse_args()

    root = Path(args.hub).resolve() if args.hub else Path(tempfile.mkdtemp(prefix="exuvia-loadtest-"))
    marker = root / ".loadtest.json"
    shape = {"projects": args.projects, "rows": args.rows, "small": args.small, "history": args.history}
    env = {"EXUVIA_SCHEDULER": "external"}
    if not (marker.is_file() and json.loads(marker.read_text()) == shape):
        print(f"seeding {root}: {shape}", flush=True)
        for i in range(args.projects):
            write_project(root, i, args.rows, args.small, args.history)
        hub = HubProcess(root, env)
        try:
            asyncio.run(seed(hub, args.projects, args.history))
        finally:
            hub.stop()
        marker.write_text(json.dumps(shape))
    else:
        print(f"reusing the seeded hub at {root}", flush=True)

    rows = asyncio.run(run_scenarios(args, root, env))
    if args.json:
        Path(args.json).write_text(json.dumps({"shape": shape, "cpus": os.cpu_count(), "results": rows}, indent=2))


if __name__ == "__main__":
    main()
