"""Drive the Flight SQL prototype (server.py) with the ADBC Flight SQL driver,
the client a BI tool or a script in another language would use, and compare
it with the HTTP data API.

    uv run --with adbc-driver-flightsql --with adbc-driver-manager --with httpx \\
        python docs/design/flight-sql/check.py

It starts server.py on a fresh hub, makes three projects over HTTP (two
small ones and one with BIG_ROWS rows), makes data keys, then checks:

1. the same cross-project join answers the same over Flight SQL and HTTP;
2. discovery: adbc_get_objects and get_table_schema see the key's catalogs,
   and only those;
3. the key rules: no key, a bad key, a project the key does not read, a
   revoked key, and statements that would write, all fail;
4. timings: a small join and BIG_ROWS rows, Flight SQL against HTTP Arrow
   (read as it arrives, and buffered whole first) and HTTP JSON.
"""

from __future__ import annotations

import io
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import adbc_driver_flightsql.dbapi as flight_sql
import httpx
import pyarrow as pa
from adbc_driver_flightsql import DatabaseOptions

HERE = Path(__file__).parent
HTTP, FLIGHT = 7450, 7451
BIG_ROWS = 2_000_000

ORDERS = '''import exuvia


@exuvia.node()
def orders():
    import pandas as pd

    return pd.DataFrame({"account": [1, 1, 2], "amount": [10.0, 5.0, 7.5]})
'''
ACCOUNTS = '''import exuvia


@exuvia.node()
def accounts():
    import pandas as pd

    return pd.DataFrame({"account": [1, 2], "name": ["Acme", "Globex"]})
'''
TICKS = f'''import exuvia


@exuvia.node()
def ticks():
    import numpy as np
    import pandas as pd

    n = {BIG_ROWS}
    rng = np.random.default_rng(0)
    return pd.DataFrame({{"i": np.arange(n), "px": rng.random(n), "sym": np.array(["AAA", "BBB", "CCC", "DDD"])[np.arange(n) % 4]}})
'''
JOIN = (
    'select a.name, sum(o.amount) as total from "sales".orders o join crm.accounts a using (account) '
    "group by a.name order by a.name"
)
ARROW = "application/vnd.apache.arrow.stream"
failures: list[str] = []


def check(what: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}{(': ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(what)


def connect(key: str | None):
    kwargs = {DatabaseOptions.AUTHORIZATION_HEADER.value: f"Bearer {key}"} if key else {}
    return flight_sql.connect(f"grpc://127.0.0.1:{FLIGHT}", db_kwargs=kwargs)


def fails(key: str | None, sql: str) -> str | None:
    """The error a query fails with over Flight SQL, or None if it ran."""
    try:
        with connect(key) as conn, conn.cursor() as cur:
            cur.execute(sql)
            cur.fetch_arrow_table()
        return None
    except Exception as e:
        return f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"


class Body(io.RawIOBase):
    """A streamed response body as a file, so pyarrow reads record batches as
    they arrive instead of after the whole body (as ADBC does over Flight)."""

    def __init__(self, response: httpx.Response):
        self._chunks = response.iter_raw()
        self._left = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:  # type: ignore[no-untyped-def]
        while not self._left:
            self._left = next(self._chunks, b"")
            if not self._left:
                return 0
        n = min(len(b), len(self._left))
        b[:n] = self._left[:n]
        self._left = self._left[n:]
        return n


def median_ms(fn, n: int) -> float:
    fn()
    xs = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t) * 1000)
    return statistics.median(xs)


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="exuvia-flight-"))
    env = {**os.environ, "EXUVIA_AUTH": "token"}
    env.pop("EXUVIA_API_TOKEN", None)
    srv = subprocess.Popen(
        [sys.executable, str(HERE / "server.py"), str(root), "--http", str(HTTP), "--flight", str(FLIGHT)], env=env,
    )
    try:
        http = httpx.Client(base_url=f"http://127.0.0.1:{HTTP}", timeout=300)
        for _ in range(200):
            try:
                http.get("/healthz")
                break
            except httpx.TransportError:
                time.sleep(0.1)
        time.sleep(0.5)  # Flight starts once HTTP answers
        for pid, node, src in (("sales", "orders", ORDERS), ("crm", "accounts", ACCOUNTS), ("market", "ticks", TICKS)):
            http.post("/api/projects", json={"id": pid}).raise_for_status()
            http.put(f"/p/{pid}/api/nodes/{node}", json={"source": src}).raise_for_status()
            run = http.post(f"/p/{pid}/api/nodes/{node}/run", json={"wait": 300}).json()
            assert run["ok"], run
        key = http.post("/api/data-keys", json={"name": "flight", "projects": ["sales", "crm", "market"]}).json()["key"]
        narrow = http.post("/api/data-keys", json={"name": "sales only", "projects": ["sales"]}).json()
        h = {"Authorization": f"Bearer {key}"}

        print("1. the same answer over Flight SQL and HTTP")
        with connect(key) as conn, conn.cursor() as cur:
            cur.execute(JOIN)
            flight_rows = cur.fetchall()
            info = conn.adbc_get_info()
        http_rows = [tuple(r) for r in http.post("/api/data/query", json={"sql": JOIN}, headers=h).json()["rows"]]
        check("cross-project join", flight_rows == http_rows == [("Acme", 15.0), ("Globex", 7.5)], f"{flight_rows} vs {http_rows}")
        check("server info", info.get("vendor_name") == "exuvia", str(info))

        print("2. discovery")
        with connect(key) as conn:
            objects = conn.adbc_get_objects(depth="tables").read_all().to_pylist()
            tables = {
                (c["catalog_name"], s["db_schema_name"], t["table_name"])
                for c in objects for s in c["catalog_db_schemas"] for t in s["db_schema_tables"]
            }
            schema = conn.adbc_get_table_schema("ticks", catalog_filter="market", db_schema_filter="main")
        check("adbc_get_objects lists the key's tables",
              {("sales", "main", "orders"), ("crm", "main", "accounts"), ("market", "main", "ticks")} <= tables, str(tables))
        check("get_table_schema", schema.names == ["i", "px", "sym"], str(schema))
        with connect(narrow["key"]) as conn:
            cats = {c["catalog_name"] for c in conn.adbc_get_objects(depth="catalogs").read_all().to_pylist()}
        check("a narrower key sees only its catalogs", cats == {"sales"}, str(cats))

        print("3. the key rules")
        check("no key", fails(None, "select 1") is not None)
        check("a bad key", fails("exd_" + "x" * 40, "select 1") is not None)
        check("a project the key does not read", fails(narrow["key"], "select * from crm.accounts") is not None)
        for sql in ("drop view crm.accounts", "attach ':memory:' as m", "set threads = 1", "copy (select 1) to 'x.csv'",
                    "select * from read_csv('/etc/passwd')", "select 1; select 2"):
            err = fails(key, sql)
            check(f"rejects {sql!r}", err is not None)
        check("the shared connection still answers after those", fails(key, "select count(*) from crm.accounts") is None)
        http.delete(f"/api/data-keys/{narrow['id']}").raise_for_status()
        time.sleep(0.1)
        check("a revoked key", fails(narrow["key"], "select * from sales.orders") is not None)

        print(f"4. timings (median; {BIG_ROWS:,} rows is market.ticks: int64, double, text)")
        conn = connect(key)

        def flight(sql: str) -> pa.Table:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetch_arrow_table()

        def http_arrow(sql: str) -> pa.Table:
            with http.stream("POST", "/api/data/query", json={"sql": sql}, headers={**h, "Accept": ARROW}) as r:
                return pa.ipc.open_stream(io.BufferedReader(Body(r), 1 << 20)).read_all()

        def http_arrow_buffered(sql: str) -> pa.Table:  # the whole body first, then parse it
            r = http.post("/api/data/query", json={"sql": sql}, headers={**h, "Accept": ARROW})
            return pa.ipc.open_stream(io.BytesIO(r.content)).read_all()

        def http_json(sql: str, limit: int) -> dict:
            return http.post("/api/data/query", json={"sql": sql, "limit": limit}, headers=h).json()

        big = "select * from market.ticks"
        check("all rows over Flight SQL", flight(big).num_rows == BIG_ROWS)
        check("all rows over HTTP Arrow", http_arrow(big).equals(http_arrow_buffered(big)) and http_arrow(big).num_rows == BIG_ROWS)
        agg = "select sym, avg(px) from market.ticks group by 1"
        rows = [
            ("small join", lambda: flight(JOIN), lambda: http_arrow(JOIN), lambda: http_arrow_buffered(JOIN),
             lambda: http_json(JOIN, 500), 200),
            ("5,000 rows", lambda: flight(big + " limit 5000"), lambda: http_arrow(big + " limit 5000"),
             lambda: http_arrow_buffered(big + " limit 5000"), lambda: http_json(big, 5000), 50),
            (f"{BIG_ROWS:,} rows", lambda: flight(big), lambda: http_arrow(big), lambda: http_arrow_buffered(big), None, 10),
            ("aggregate over them", lambda: flight(agg), lambda: http_arrow(agg), lambda: http_arrow_buffered(agg),
             lambda: http_json(agg, 500), 50),
        ]
        print(f"     {'':24s} {'Flight SQL':>12s} {'HTTP Arrow':>12s} {'(buffered)':>12s} {'HTTP JSON':>12s}")
        for name, f, a, b, j, n in rows:
            cells = [median_ms(f, n), median_ms(a, n), median_ms(b, n), median_ms(j, n) if j else None]
            print(f"     {name:24s} " + " ".join(f"{c:9.1f} ms" if c is not None else f"{'(capped)':>12s}" for c in cells))
        conn.close()
    finally:
        srv.terminate()
        srv.wait(10)
    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
