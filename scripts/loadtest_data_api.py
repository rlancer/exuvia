"""Load test for the data API (exuvia/dataapi.py).

Seeds a hub of projects with data of a realistic size, starts the hub as its
own process (``exuvia hub``) and drives ``/api/data/`` with concurrent clients,
one scenario after another. Each scenario reports requests per second,
latency percentiles, errors and the hub process's memory (RSS) and threads.

    uv run python scripts/loadtest_data_api.py                 # seed and run
    uv run python scripts/loadtest_data_api.py --hub /tmp/lt   # keep the hub: later runs skip seeding
    uv run python scripts/loadtest_data_api.py --only hot_query,churn_query
    uv run python scripts/loadtest_data_api.py --only dashboard,staleness --cache-seconds 2 --proxy nginx

The seeded hub has ``--projects`` projects, each with

* ``orders``: ``--rows`` rows (account, region, product, amount, day);
* ``accounts``: 10,000 rows; ``products``: 500 rows;
* ``by_region.sql``: a join and aggregate over them;
* ``daily``: a node with an ``as_of`` parameter, run for ``--history`` dates,
  so ``daily__runs`` is a view over that many files;
* ``--small`` small nodes, as real projects have.

Each row also has the hub's CPU use over the scenario (``cpu``, % of one core) and per
answered request.

Caching (exuvia/dataapi.py, HTTP caching):

* ``dashboard``: many clients poll a few expensive queries and node reads
  while one project re-runs a node every ``--dashboard-churn`` seconds; once
  as plain clients (``dashboard:plain``), once as clients that keep answers
  like a browser does and send ``If-None-Match`` (``dashboard:reval``: how
  many got 304s, and what those cost).
* ``staleness``: clients like a browser's read the ``tick`` node (it writes
  ``time.time()``) and a query over it while it re-runs, and compare each
  answer with the latest run that had finished when they asked: the worst
  and p99 staleness, checked against the bound the hub's caching allows
  (0 with ``no-cache``; 2 x ``--cache-seconds`` with ``max-age``).
* ``artifact``: a whole table as Arrow from its node read, as the stored
  Parquet file by its hash (``url`` in the listing), and that file by
  clients that keep it (it never changes). Keep ``--concurrency`` low: those
  clients hold the file in memory.
* ``isolation``: through the proxy, a key that can't read a project never
  gets a cached answer for it, and one key's cached answer is never served
  to another.

``--cache-seconds N`` starts the hub with ``EXUVIA_DATA_CACHE_SECONDS=N``.
``--proxy nginx`` puts nginx in front of the hub with the example config
(``deploy/nginx/exuvia.conf``), and every scenario goes through it.

Nobody signs in to the hub, so data keys are the operator's. Linux only (RSS
is read from /proc). ``--json FILE`` writes the results for comparing runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
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
from urllib.parse import urlencode

import httpx

ARROW = {"Accept": "application/vnd.apache.arrow.stream"}
REPO = Path(__file__).resolve().parent.parent
TICKS = os.sysconf("SC_CLK_TCK")

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
        self.base = self.url  # where scenarios send their requests: the proxy's address when there is one
        self.proxy: NginxProxy | None = None
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

    def cpu(self) -> float:
        """CPU seconds (user + system) the hub process has used."""
        return proc_cpu(self.proc.pid)

    def stop(self) -> None:
        if self.proxy is not None:
            self.proxy.stop()
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def proc_cpu(pid: int) -> float:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return 0.0
    return (int(fields[11]) + int(fields[12])) / TICKS  # utime, stime


class NginxProxy:
    """nginx in front of the hub with the example config (deploy/nginx/
    exuvia.conf), its cache in a folder of its own."""

    def __init__(self, upstream_port: int, folder: Path):
        exe = shutil.which("nginx") or "/usr/sbin/nginx"
        if not Path(exe).exists():
            raise SystemExit("--proxy nginx needs nginx (apt-get install nginx)")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "cache").mkdir(parents=True)
        (folder / "logs").mkdir()
        conf = (REPO / "deploy" / "nginx" / "exuvia.conf").read_text()
        conf = conf.replace("server exuvia:7420;", f"server 127.0.0.1:{upstream_port};")
        conf = conf.replace("listen 80;", f"listen 127.0.0.1:{self.port};")
        conf = conf.replace("/var/cache/nginx/exuvia", str(folder / "cache"))
        (folder / "exuvia.conf").write_text(conf)
        (folder / "nginx.conf").write_text(
            f"worker_processes 2;\npid {folder}/nginx.pid;\nerror_log {folder}/logs/error.log warn;\n"
            "events { worker_connections 4096; }\n"
            f"http {{\n  access_log off;\n  client_body_temp_path {folder}/body;\n  proxy_temp_path {folder}/proxy;\n"
            f"  fastcgi_temp_path {folder}/fastcgi;\n  uwsgi_temp_path {folder}/uwsgi;\n  scgi_temp_path {folder}/scgi;\n"
            f"  include {folder}/exuvia.conf;\n}}\n"
        )
        self.folder = folder
        self.proc = subprocess.Popen(
            [exe, "-p", str(folder), "-c", str(folder / "nginx.conf"), "-g", "daemon off;"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(f"nginx exited: {self.proc.stderr.read() if self.proc.stderr else ''}")
            try:
                httpx.get(self.url + "/api/data/openapi.json", timeout=2)
                return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise SystemExit("nginx did not start in 10s")

    def cpu(self) -> float:
        """CPU seconds nginx's master and workers have used."""
        total = proc_cpu(self.proc.pid)
        for stat in Path("/proc").glob("[0-9]*/stat"):
            try:
                if int(stat.read_text().rsplit(")", 1)[1].split()[1]) == self.proc.pid:
                    total += proc_cpu(int(stat.parent.name))
            except (OSError, ValueError, IndexError):
                pass
        return total

    def cache_bytes(self) -> int:
        return sum(f.stat().st_size for f in (self.folder / "cache").rglob("*") if f.is_file())

    def stop(self) -> None:
        self.proc.send_signal(signal.SIGQUIT)
        try:
            self.proc.wait(10)
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
    cpu_seconds: float = 0.0  # the hub process's CPU time (user + system) while the scenario ran
    proxy_cpu: float | None = None  # nginx's, with --proxy nginx
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

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
            "peak_threads": self.peak_threads,
            "hub_cpu_pct": round(100 * self.cpu_seconds / self.seconds) if self.seconds else 0,
            "cpu_ms_per_req": round(1000 * self.cpu_seconds / len(lat), 2) if lat else None,
            "errors": self.errors, "note": self.note,
            **({"proxy_cpu_s": round(self.proxy_cpu, 2)} if self.proxy_cpu is not None else {}),
            **self.extra,
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
    async with httpx.AsyncClient(base_url=hub.base, timeout=timeout, limits=limits) as c:
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

        pcpu0 = hub.proxy.cpu() if hub.proxy else None
        with Sampler(hub) as s:
            cpu0 = hub.cpu()
            t0 = time.perf_counter()
            clients = [asyncio.create_task(client(n)) for n in range(concurrency)]
            if max_requests is None:
                await asyncio.sleep(duration)
                stop.set()
            await asyncio.gather(*clients)
            res.seconds = time.perf_counter() - t0
            res.cpu_seconds = hub.cpu() - cpu0
            if hub.proxy is not None and pcpu0 is not None:
                res.proxy_cpu = hub.proxy.cpu() - pcpu0
            stop.set()
            if bg:
                await bg
        res.peak_rss, res.peak_threads = s.peak_rss, s.peak_threads
    res.rss_after = hub.stat()[0]
    return res


def print_row(r: dict[str, Any]) -> None:
    errs = f"  errors: {r['errors']}" if r["errors"] else ""
    note = f"  ({r['note']})" if r["note"] else ""
    proxy = f" nginx cpu={r['proxy_cpu_s']}s" if "proxy_cpu_s" in r else ""
    print(
        f"{r['scenario']:<16} c={r['conc']:<3} ok={r['ok']:<6} err={r['err']:<4} 429={r['429']:<5} rps={r['rps']:<8} "
        f"p50={r['p50_ms']}ms p95={r['p95_ms']}ms p99={r['p99_ms']}ms max={r['max_ms']}ms "
        f"cpu={r.get('hub_cpu_pct')}% ({r.get('cpu_ms_per_req')}ms/req) "
        f"rss peak/after={r['peak_rss_mb']}/{r['rss_after_mb']}MiB threads={r['peak_threads']}{proxy}{note}{errs}",
        flush=True,
    )


# -- a client that caches like a browser ------------------------------------------------


def max_age(headers: httpx.Headers) -> float:
    """How long an answer stays fresh in a client's cache: max-age less the
    Age a shared cache reports. 0 for no-cache, no-store or none."""
    cc = headers.get("cache-control", "")
    if "no-cache" in cc or "no-store" in cc:
        return 0.0
    m = re.search(r"max-age=(\d+(?:\.\d+)?)", cc)
    return max(0.0, float(m.group(1)) - float(headers.get("age") or 0)) if m else 0.0


@dataclass
class CacheStats:
    local: int = 0  # answered from the client's cache, nothing sent
    sent_inm: int = 0  # sent with If-None-Match
    got_304: int = 0
    got_200: int = 0
    lat_304: list[float] = field(default_factory=list)
    lat_200: list[float] = field(default_factory=list)

    def note(self) -> dict[str, Any]:
        def p50(xs: list[float]) -> float | None:
            return round(statistics.median(xs) * 1000, 2) if xs else None

        sent = self.got_304 + self.got_200
        return {
            "client_cache": {
                "local_hits": self.local, "sent": sent, "sent_if_none_match": self.sent_inm, "got_304": self.got_304,
                "rate_304": round(self.got_304 / sent, 3) if sent else None,
                "p50_304_ms": p50(self.lat_304), "p50_200_ms": p50(self.lat_200),
            }
        }


class BrowserCache:
    """One client's HTTP cache, as a browser keeps one: each answer with its
    ETag, used without asking while it is fresh (max-age), then sent back
    in If-None-Match. Holds bodies in memory: for small answers only."""

    def __init__(self, stats: CacheStats):
        self.stats = stats
        self.entries: dict[tuple[str, str], tuple[str | None, bytes, float]] = {}  # (url, accept) -> (etag, body, fresh until)

    async def get(self, c: httpx.AsyncClient, url: str, headers: dict[str, str]) -> httpx.Response:
        k = (url, headers.get("Accept", ""))
        hit = self.entries.get(k)
        now = time.monotonic()
        if hit and now < hit[2]:
            self.stats.local += 1
            # Let the other clients run: a loop of hits that never waits
            # would hold the event loop and slow every request on the wire.
            await asyncio.sleep(0)
            return httpx.Response(200, content=hit[1])
        send = dict(headers)
        if hit and hit[0]:
            send["If-None-Match"] = hit[0]
            self.stats.sent_inm += 1
        t = time.perf_counter()
        r = await c.get(url, headers=send)
        took = time.perf_counter() - t
        if r.status_code == 304 and hit:
            self.stats.got_304 += 1
            self.stats.lat_304.append(took)
            self.entries[k] = (r.headers.get("etag") or hit[0], hit[1], time.monotonic() + max_age(r.headers))
            return httpx.Response(200, content=hit[1], headers=r.headers)
        if r.status_code == 200:
            self.stats.got_200 += 1
            self.stats.lat_200.append(took)
            if "no-store" not in r.headers.get("cache-control", ""):
                self.entries[k] = (r.headers.get("etag"), r.content, time.monotonic() + max_age(r.headers))
        return r


# -- scenarios ----------------------------------------------------------------------


def result_cache(hub: HubProcess) -> dict[str, Any] | None:
    """The hub's in-process result cache counters (None where it has none)."""
    try:
        return httpx.get(hub.url + "/api/data-keys/usage", params={"days": 1}, timeout=30).json().get("result_cache")
    except (httpx.HTTPError, ValueError):
        return None


def cache_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
    if not before or not after:
        return None
    d = {k: after[k] - before[k] for k in ("hits", "misses", "coalesced", "stored", "evictions", "not_kept")}
    asked = d["hits"] + d["misses"] + d["coalesced"]
    return {**d, "hit_rate": round(d["hits"] / asked, 3) if asked else None, "bytes": after["bytes"], "entries": after["entries"]}


def staleness_of(samples: list[tuple[float, float]], runs: list[tuple[float, float]]) -> list[float]:
    """For each answer (asked at, value it held), how long it had been out
    of date: since the first run newer than its value finished, if that run
    had finished before the answer was asked for; else 0. ``runs`` are
    (value the run wrote, when it was seen finished)."""
    out = []
    for asked, value in samples:
        newer = [done for t, done in runs if t > value + 1e-9 and done <= asked]
        out.append(asked - min(newer) if newer else 0.0)
    return out


async def staleness(
    hub: HubProcess, args: argparse.Namespace, pid: str, auth: dict[str, str],
    query: Callable[..., Awaitable[httpx.Response]], read: Callable[..., Awaitable[httpx.Response]],
) -> Result:
    """Clients that cache like a browser read ``tick`` (a node read and a
    query) while it re-runs every ``--tick-every`` seconds; how stale is
    what they see, against the latest run that had finished when they asked?"""
    runs: list[tuple[float, float]] = []
    samples: list[tuple[float, float]] = []
    stats = CacheStats()
    n = args.staleness_clients
    caches = [BrowserCache(stats) for _ in range(n)]
    sql = f'select t from "{pid}".tick'

    async def ticker(c: httpx.AsyncClient, stop: asyncio.Event) -> None:
        while not stop.is_set():
            r = await c.post(f"{hub.url}/p/{pid}/api/nodes/tick/run", json={"wait": 60})
            done = time.monotonic()
            if r.status_code == 200 and r.json().get("ok"):
                # What it wrote, read past every cache (the workbench API, straight from the hub).
                got = await c.get(f"{hub.url}/p/{pid}/api/nodes/tick/data")
                runs.append((float(got.json()["rows"][0][0]), done))
            try:
                await asyncio.wait_for(stop.wait(), args.tick_every)
            except asyncio.TimeoutError:
                pass

    async def ask(c: httpx.AsyncClient, i: int) -> httpx.Response:
        cache = caches[i % n]
        asked = time.monotonic()
        if (i // n) % 2:
            r = await query(c, sql, auth, cache)
        else:
            r = await read(c, f"/api/data/projects/{pid}/nodes/tick", auth, cache)
        if r.status_code == 200:
            samples.append((asked, float(json.loads(r.content)["rows"][0][0])))
        await asyncio.sleep(0.02)
        return r

    res = await drive(hub, "staleness", ask, n, args.duration, background=ticker)
    # Only answers asked once the first run had finished say anything.
    first = runs[0][1] if runs else float("inf")
    stale = sorted(staleness_of([s for s in samples if s[0] >= first], runs))
    mode = f"max-age={args.cache_seconds:g}" if args.cache_seconds else "no-cache"
    rc = result_cache(hub)
    if rc and rc["budget"] > 0:
        mode += f", result cache {rc['budget'] / 2**20:g} MiB ({rc['hits']} hits so far)"
    bound = 2 * args.cache_seconds + 0.5 if args.cache_seconds else 0.0  # max-age + stale-while-revalidate, and the update
    worst = stale[-1] if stale else 0.0
    p99 = stale[min(len(stale) - 1, int(0.99 * len(stale)))] if stale else 0.0
    ok = worst <= bound
    res.extra.update(stats.note())
    res.extra["staleness"] = {
        "mode": mode, "through": "nginx" if hub.proxy else "hub", "runs": len(runs), "answers": len(stale),
        "stale_answers": sum(1 for x in stale if x > 0), "worst_s": round(worst, 3), "p99_s": round(p99, 3),
        "bound_s": bound, "pass": ok,
    }
    res.note = (f"{mode}: {len(runs)} runs, {len(stale)} answers, {sum(1 for x in stale if x > 0)} stale, "
                f"worst {worst:.3f}s p99 {p99:.3f}s, bound {bound:g}s: {'PASS' if ok else 'FAIL'}")
    return res


async def isolation(hub: HubProcess, args: argparse.Namespace, pids: list[str], get_query: bool) -> Result:
    """Through the proxy (or straight to the hub): a key that can't read a
    project never gets a cached answer for it, and one key's cached answer
    is never another key's."""
    a = {"Authorization": f"Bearer {await make_key(hub, pids[:2], 'isolation a')}"}
    b = {"Authorization": f"Bearer {await make_key(hub, pids[2:3], 'isolation b')}"}
    a2 = {"Authorization": f"Bearer {await make_key(hub, pids[:2], 'isolation a2')}"}
    checks: dict[str, bool] = {}
    res = Result("isolation", 1, 0.0)
    cpu0 = hub.cpu()
    t0 = time.perf_counter()
    sql = f'select count(*) from "{pids[0]}".accounts'
    q = "/api/data/query?" + urlencode({"sql": sql}) if get_query else None
    node = f"/api/data/projects/{pids[0]}/nodes/accounts?limit=5"
    async with httpx.AsyncClient(base_url=hub.base, timeout=60) as c:

        async def get(url: str, headers: dict[str, str]) -> httpx.Response:
            t = time.perf_counter()
            r = await c.get(url, headers=headers)
            if r.status_code in (200, 304):
                res.latencies.append(time.perf_counter() - t)
            return r

        # The output by its hash, where the hub has it: `immutable`, so a proxy keeps it even without --cache-seconds.
        listed = (await c.get(f"/api/data/projects/{pids[0]}", headers=a)).json()
        artifact = next((n.get("url") for n in listed.get("nodes", []) if n["name"] == "accounts"), None)
        urls = [u for u in (q, node, "/api/data/projects", artifact) if u]
        for url in urls:
            first = await get(url, a)
            second = await get(url, a)  # a cache, if there is one, now has it
            checks[f"{url[:64]}: key A reads it"] = first.status_code == second.status_code == 200
            cached = second.headers.get("x-cache-status") in ("HIT", "REVALIDATED", "STALE", "UPDATING")
            res.extra.setdefault("cached_for_a", {})[url[:64]] = second.headers.get("x-cache-status")
            other = await get(url, b)
            if url == "/api/data/projects":
                checks[f"{url}: key B sees its own projects"] = (
                    other.status_code == 200 and [p["id"] for p in other.json()["projects"]] == pids[2:3]
                )
            else:
                checks[f"{url[:64]}: key B is refused"] = other.status_code in (400, 404)
            checks[f"{url[:64]}: key B gets no cached answer"] = other.headers.get("x-cache-status") not in ("HIT", "STALE", "UPDATING")
            twin = await get(url, a2)
            checks[f"{url[:64]}: key A2 is answered for itself, not from A's entry"] = (
                twin.status_code == 200 and twin.headers.get("x-cache-status") not in ("HIT", "STALE", "UPDATING")
                and (url != "/api/data/projects" or twin.json()["key"]["name"] == "isolation a2")
            )
            nobody = await get(url, {})
            checks[f"{url[:64]}: no key is refused"] = nobody.status_code == 401
            tag = second.headers.get("etag")
            if tag:
                stolen = await get(url, {**b, "If-None-Match": tag})
                checks[f"{url[:64]}: key B with A's ETag gets no 304"] = stolen.status_code != 304
            res.extra.setdefault("a_cached", {})[url[:64]] = cached
    res.seconds = time.perf_counter() - t0
    res.cpu_seconds = hub.cpu() - cpu0
    failed = [k for k, v in checks.items() if not v]
    res.extra["isolation"] = {"checks": checks, "pass": not failed}
    caching_on = any(res.extra.get("a_cached", {}).values())
    res.note = (f"{len(checks) - len(failed)}/{len(checks)} checks pass"
                f"{'' if hub.proxy else ', no proxy'}{'' if caching_on or not hub.proxy else ', nothing was cached (use --cache-seconds)'}"
                f"{': FAIL ' + '; '.join(failed) if failed else ''}")
    return res


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
    if args.proxy == "nginx":
        hub.proxy = NginxProxy(hub.port, Path(tempfile.mkdtemp(prefix="exuvia-loadtest-nginx-")))
        hub.base = hub.proxy.url
    try:
        all_key = await make_key(hub, pids, "all")
        two_key = await make_key(hub, pids[:2], "two")
        other_key = await make_key(hub, pids[:2], "another app")
        auth_all = {"Authorization": f"Bearer {all_key}"}
        auth_two = {"Authorization": f"Bearer {two_key}"}
        auth_other = {"Authorization": f"Bearer {other_key}"}
        rss0 = hub.stat()[0]
        print(f"hub started: rss={rss0:.0f}MiB, {len(pids)} projects"
              f"{f', behind nginx at {hub.base}' if hub.proxy else ''}"
              f"{f', EXUVIA_DATA_CACHE_SECONDS={args.cache_seconds:g}' if args.cache_seconds else ''}", flush=True)
        # Hubs before the GET form of the query answer it 405: send those a POST.
        probe = httpx.get(hub.url + "/api/data/query", params={"sql": "select 1", "projects": pids[0]}, headers=auth_two)
        get_query = probe.status_code != 405

        def query(c: httpx.AsyncClient, sql: str, headers: dict[str, str], cache: BrowserCache | None = None) -> Awaitable[httpx.Response]:
            """The query as a GET (which caches keep) where the hub has one."""
            if not get_query:
                return c.post("/api/data/query", json={"sql": sql}, headers=headers)
            url = "/api/data/query?" + urlencode({"sql": sql})
            return cache.get(c, url, headers) if cache else c.get(url, headers=headers)

        def read(c: httpx.AsyncClient, url: str, headers: dict[str, str], cache: BrowserCache | None = None) -> Awaitable[httpx.Response]:
            return cache.get(c, url, headers) if cache else c.get(url, headers=headers)

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

        await large_json(hub, args, pids, auth_two, want, conc, record)

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

        if want("artifact"):
            # A whole table, three ways: as Arrow from the node read (DuckDB
            # reads and streams it), as the stored Parquet file by its hash
            # (sent byte for byte), and that file by a client that keeps it
            # (it is immutable: asked for once).
            listing = httpx.get(hub.url + f"/api/data/projects/{pids[0]}", headers=auth_two, timeout=60).json()
            url = next((n.get("url") for n in listing["nodes"] if n["name"] == "orders"), None)
            for n in conc:
                record(await drive(
                    hub, "artifact:arrow",
                    lambda c, i: drained(c, "GET", f"/api/data/projects/{pids[0]}/nodes/orders", headers={**auth_two, **ARROW}),
                    n, args.duration, timeout=300,
                ))
                if url is None:
                    continue
                res = await drive(hub, "artifact:file", lambda c, i: drained(c, "GET", url, headers=auth_two), n, args.duration, timeout=300)
                res.note = url.rsplit("/", 1)[-1]
                record(res)
                stats = CacheStats()
                caches = [BrowserCache(stats) for _ in range(n)]

                def kept(c: httpx.AsyncClient, i: int, n: int = n, caches: list[BrowserCache] = caches) -> Awaitable[httpx.Response]:
                    return caches[i % n].get(c, url, auth_two)

                res = await drive(hub, "artifact:kept", kept, n, args.duration, timeout=300)
                res.extra.update(stats.note())
                res.note = f"{stats.got_200} fetched, {stats.local} from the client's cache"
                record(res)
            if url is None:
                print("  (no artifact urls on this hub: artifact:file and artifact:kept skipped)", flush=True)

        if want("dashboard"):
            # Many clients polling a dashboard's panels (three expensive
            # queries, two node reads) while one of its projects re-runs a
            # node every few seconds, which changes what two panels read.
            dash_key = await make_key(hub, pids[:3], "dashboard")
            auth_dash = {"Authorization": f"Bearer {dash_key}"}
            panels: list[tuple[str, str]] = [
                ("query", hot_sql),
                ("query", f'select region, date_trunc(\'month\', day) as m, sum(amount), count(*) from "{pids[2]}".orders group by all order by all'),
                ("query", f'select as_of, avg(balance), max(balance) from "{pids[1]}".daily__runs group by all order by 1'),
                ("node", f"/api/data/projects/{pids[0]}/nodes/by_region"),
                ("node", f"/api/data/projects/{pids[1]}/nodes/accounts?limit=100"),
            ]
            runs = 0

            async def dash_churn(c: httpx.AsyncClient, stop: asyncio.Event) -> None:
                nonlocal runs
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), args.dashboard_churn)
                    except asyncio.TimeoutError:
                        pass
                    if stop.is_set():
                        return
                    r = await c.post(f"/p/{pids[0]}/api/nodes/tick/run", json={"wait": 60})
                    runs += r.status_code == 200

            for mode in ("plain", "reval"):
                for n in conc:
                    stats = CacheStats()
                    caches = [BrowserCache(stats) for _ in range(n)] if mode == "reval" else None

                    def panel(c: httpx.AsyncClient, i: int, n: int = n, caches: list[BrowserCache] | None = caches) -> Awaitable[httpx.Response]:
                        kind, what = panels[(i // n) % len(panels)]
                        cache = caches[i % n] if caches else None
                        return query(c, what, auth_dash, cache) if kind == "query" else read(c, what, auth_dash, cache)

                    runs = 0
                    rc0 = result_cache(hub)
                    res = await drive(hub, f"dashboard:{mode}", panel, n, args.duration, background=dash_churn)
                    rc = cache_delta(rc0, result_cache(hub))
                    res.note = f"{runs} runs during it" + ("" if get_query else "; POST queries (no GET on this hub)")
                    if caches:
                        res.extra.update(stats.note())
                        cc = res.extra["client_cache"]
                        res.note += f"; 304s {cc['got_304']}/{cc['sent']} sent, {cc['local_hits']} from the client's cache, p50 304 {cc['p50_304_ms']}ms vs 200 {cc['p50_200_ms']}ms"
                    if rc:
                        res.extra["result_cache"] = rc
                        res.note += (f"; result cache: {rc['hits']} hits, {rc['misses']} misses, {rc['coalesced']} coalesced, "
                                     f"{rc['evictions']} evicted, {rc['bytes'] / 2**20:.1f} MiB")
                    if hub.proxy:
                        res.extra["nginx_cache_mb"] = round(hub.proxy.cache_bytes() / 2**20, 1)
                    record(res)

        if want("staleness"):
            record(await staleness(hub, args, pids[0], auth_two, query, read))

        if want("isolation"):
            record(await isolation(hub, args, pids, get_query))
    finally:
        hub.stop()
    return out


async def large_json(
    hub: HubProcess, args: argparse.Namespace, pids: list[str], auth: dict[str, str],
    want: Callable[[str], bool], conc: list[int], record: Callable[[Result], None],
) -> None:
    """Large JSON answers, 5,000 rows each, where writing the JSON is much of
    the work: ``wide_json`` a query over a join (10 columns: ints, floats,
    strings, a timestamp), ``wide_node`` a node's rows. Each request reads
    other rows (``offset``), so no answer repeats."""
    two = f'"{pids[0]}"', f'"{pids[1]}"'
    pages = max(1, args.rows // 5000)
    if want("wide_json"):
        def query(c: httpx.AsyncClient, i: int) -> Awaitable[httpx.Response]:
            sql = (
                f"select o.*, a.name, a.segment, a.account * 1.5 as score from {two[0]}.orders o "
                f"join {two[1]}.accounts a using (account) order by o.order_id limit 5000 offset {(i % pages) * 5000}"
            )
            return drained(c, "POST", "/api/data/query", json={"sql": sql, "limit": 5000}, headers=auth)

        for n in conc:
            record(await drive(hub, "wide_json", query, n, args.duration))
    if want("wide_node"):
        def node(c: httpx.AsyncClient, i: int) -> Awaitable[httpx.Response]:
            url = f"/api/data/projects/{pids[i % 2]}/nodes/orders?limit=5000&offset={(i % pages) * 5000}"
            return drained(c, "GET", url, headers=auth)

        for n in conc:
            record(await drive(hub, "wide_node", node, n, args.duration))


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
                    "node_read, history, churn_query, distinct_sets, arrow_scan, wide_json, wide_node, heavy_query, dashboard, "
                    "staleness, isolation, artifact")
    ap.add_argument("--cache-seconds", type=float, default=0.0, help="EXUVIA_DATA_CACHE_SECONDS for the hub (0: no-cache)")
    ap.add_argument("--proxy", choices=["nginx"], help="put a proxy in front of the hub (deploy/nginx/exuvia.conf)")
    ap.add_argument("--dashboard-churn", type=float, default=3.0, help="seconds between runs in dashboard")
    ap.add_argument("--tick-every", type=float, default=0.5, help="seconds between runs of tick in staleness")
    ap.add_argument("--staleness-clients", type=int, default=8, help="clients in staleness")
    ap.add_argument("--env", action="append", default=[], help="NAME=VALUE for the hub's environment (repeatable)")
    ap.add_argument("--json", help="write the results to this file")
    ap.add_argument("--db", help="EXUVIA_DB for the hub (a postgresql:// URL): run records in a shared database, not "
                    "each project's SQLite file. Seed a separate --hub for it")
    args = ap.parse_args()

    root = Path(args.hub).resolve() if args.hub else Path(tempfile.mkdtemp(prefix="exuvia-loadtest-"))
    marker = root / ".loadtest.json"
    shape = {"projects": args.projects, "rows": args.rows, "small": args.small, "history": args.history}
    env = {"EXUVIA_SCHEDULER": "external"}
    if args.cache_seconds:
        env["EXUVIA_DATA_CACHE_SECONDS"] = f"{args.cache_seconds:g}"
    for pair in args.env:
        name, _, value = pair.partition("=")
        env[name] = value
    if args.db:
        env["EXUVIA_DB"] = args.db
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
