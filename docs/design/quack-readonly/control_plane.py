"""Serve the lake over Quack from inside exuvia's own server process, and load both.

    control_plane.py serve PROJECT_DIR HTTP_PORT QUACK_PORT MODE [NATIVE_EXT]
    control_plane.py load HTTP_PORT QUACK_PORT DATA_PATH OUT_JSON

MODE is none (no Quack: the baseline), python (the Python UDF policy) or native (the C
extension). `serve` builds exuvia's real app (exuvia.server.create_app) and, in the same
process, the Quack server with the policy, the lockdown and a watchdog, plus two threads that
stand for what the control plane itself would do with the lake:

  - a writer committing to the lake through its own Quack server, every 0.2 s;
  - a sandboxed reader (pinned snapshot, allowlisted files, locked: see sandbox_reader.py)
    attaching and counting, every 0.5 s.

Both connect back to the same process, so their statements reach the authorization hook on a
Quack thread while the caller waits: the case where a hook that needs the GIL could deadlock.
A /_test/hog switch starts a pure-Python busy loop that holds the GIL in 5 ms slices, standing
for control-plane work in Python.

`load` runs phases against a running `serve`: HTTP plus a "ping" (quack_query with the reader
token: a connection and one statement, so two hook calls, per round trip); the same plus
two lake reader processes; the same with the GIL hog on. It measures exuvia's HTTP latency (GET /api/project, and
POST /api/query, which runs DuckDB), lake reads, and the in-process writer and reader.
"""
import json
import os
import statistics
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * (len(xs) - 1)))], 2) if xs else None


def summary(ms):
    return {"n": len(ms), "p50": pct(ms, 0.5), "p95": pct(ms, 0.95), "max": pct(ms, 1.0)}


# ---------------------------------------------------------------- serve
def serve(project, http_port, quack_port, mode, native_ext=None):
    import uvicorn
    from fastapi import FastAPI

    from common import READER_TOKEN, WRITER_TOKEN, connect, ext_path, lake_client
    from exuvia.server import create_app
    from policy import ReadOnlyPolicy, lock_down, self_test

    os.makedirs(project, exist_ok=True)
    app: FastAPI = create_app(project)
    lat = {"writer": [], "sandbox": []}
    errors = {"writer": [], "sandbox": []}
    hog = threading.Event()
    lock = threading.Lock()

    @app.post("/_test/reset")
    def reset():
        with lock:
            for k in lat:
                lat[k].clear()
                errors[k].clear()
        return {"ok": True}

    @app.get("/_test/stats")
    def stats():
        with lock:
            out = {k: {**summary(v), "errors": errors[k][:3], "n_errors": len(errors[k])} for k, v in lat.items()}
        return {"quack": mode != "none", **out}

    @app.post("/_test/hog")
    def set_hog(on: int):
        (hog.set if on else hog.clear)()
        return {"hog": hog.is_set()}

    def hogger():
        while True:
            if not hog.is_set():
                time.sleep(0.05)
                continue
            end = time.perf_counter() + 0.005
            n = 0
            while time.perf_counter() < end:  # pure Python: holds the GIL until the interpreter switches
                n += 1

    threading.Thread(target=hogger, daemon=True).start()
    # exuvia's catch-all for the UI would answer GET /_test/... first: put the test routes in front
    test_routes = [r for r in app.router.routes if getattr(r, "path", "").startswith("/_test/")]
    app.router.routes[:] = test_routes + [r for r in app.router.routes if r not in test_routes]

    if mode != "none":
        data = os.path.join(project, ".exuvia", "lake-data").replace("\\", "/") + "/"
        os.makedirs(data, exist_ok=True)
        catalog = os.path.join(project, ".exuvia", "lake.duckdb")
        cfg = {"allow_unsigned_extensions": "true"} if mode == "native" else None
        s = connect(catalog, config=cfg)
        if mode == "native":
            import policy_native
            policy_native.install(s, native_ext, [READER_TOKEN])
            role = lambda cur, cid: policy_native.role(cur, cid)
            forget = lambda cur, ids: policy_native.forget(cur, ids)
        else:
            pol = ReadOnlyPolicy({WRITER_TOKEN: "writer", READER_TOKEN: "reader"})
            pol.install(s)
            role = lambda cur, cid: pol.roles.get(cid)
            forget = lambda cur, ids: pol.forget(set(ids))
        uri = f"quack://127.0.0.1:{quack_port}"
        s.execute(f"SELECT * FROM quack_serve('{uri}', token := '{WRITER_TOKEN}')").fetchall()
        lock_down(s)
        self_test(connect(), uri, READER_TOKEN)

        def watchdog():
            wd = connect()
            wd.execute(f"CREATE SECRET (TYPE quack, TOKEN '{WRITER_TOKEN}')")
            wd.execute(f"ATTACH 'quack:{uri}' AS self")
            cur = s.cursor()
            while True:
                time.sleep(0.5)
                now_us = time.time() * 1e6
                rows = cur.execute("SELECT connection_id, state, epoch_us(query_started_at) "
                                   "FROM quack_active_connections()").fetchall()
                forget(cur, [r[0] for r in rows])
                for cid, state, started in rows:
                    if role(cur, cid) == "reader" and str(state).lower() == "active" and started \
                            and now_us - started > 30e6:  # 5 s cancelled attaches once the box was overloaded
                        wd.execute(f"SELECT * FROM quack_cancel('self', '{cid}')").fetchall()

        def record(kind, fn):
            t = time.perf_counter()
            try:
                fn()
                with lock:
                    lat[kind].append((time.perf_counter() - t) * 1000)
            except Exception as e:
                with lock:
                    errors[kind].append(str(e)[:150])

        w = lake_client(quack_port, WRITER_TOKEN, data)
        w.execute("CREATE TABLE IF NOT EXISTS lake.t AS SELECT i AS x FROM range(1000) r(i)")

        def writer():
            i = 0
            while True:
                i += 1
                record("writer", lambda: w.execute(f"INSERT INTO lake.t SELECT i FROM range({i * 10}, {i * 10 + 10}) r(i)"))
                time.sleep(0.2)

        pinner = w.cursor()  # one connection per thread: a DuckDB connection refuses concurrent calls

        def sandboxed_read():
            import duckdb
            pin = pinner.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]
            files = [os.path.join(r, n).replace("\\", "/") for r, _, ns in os.walk(data) for n in ns]
            c = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
            for n in ("httpfs", "ducklake", "quack"):
                c.execute(f"LOAD '{ext_path(n)}'")
            c.execute(f"CREATE SECRET (TYPE quack, TOKEN '{READER_TOKEN}')")
            c.execute(f"ATTACH 'ducklake:quack:{uri}' AS lake (DATA_PATH '{data}', READ_ONLY, SNAPSHOT_VERSION {pin})")
            c.execute("SET allowed_paths = ?", [files + [f"http://127.0.0.1:{quack_port}/quack"]])
            c.execute("SET enable_external_access = false")
            c.execute("SET lock_configuration = true")
            c.execute("SELECT count(*) FROM lake.t").fetchall()
            c.close()

        def sandbox():
            while True:
                record("sandbox", sandboxed_read)
                time.sleep(0.5)

        for fn in (watchdog, writer, sandbox):
            threading.Thread(target=fn, daemon=True).start()

    print("serving", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=http_port, log_level="warning")


# ---------------------------------------------------------------- load
def http(method, url, body=None, timeout=30):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def lake_reader(port, data, stop_at, out):
    from common import READER_TOKEN, lake_client
    t = time.perf_counter()
    r = lake_client(port, READER_TOKEN, data, read_only=True)
    attach_ms = (time.perf_counter() - t) * 1000
    ms, errs = [], []
    while time.time() < stop_at:
        t = time.perf_counter()
        try:
            r.execute("SELECT count(*), max(x) FROM lake.t").fetchall()
            ms.append((time.perf_counter() - t) * 1000)
        except Exception as e:
            errs.append(str(e)[:150])
    out.put((ms, errs, attach_ms))


def load(http_port, quack_port, data, out_json, phase_seconds=25):
    import multiprocessing as mp
    base = f"http://127.0.0.1:{http_port}"
    results = {}
    quack = http("GET", f"{base}/_test/stats")["quack"]
    phases = [("http + ping", 0, False), ("http + ping + 2 lake readers", 2, False),
              ("http + ping + 2 lake readers + GIL hog", 2, True)]
    for name, readers, hog in phases:
        if not quack and name != phases[0][0]:
            continue
        http("POST", f"{base}/_test/hog?on={int(hog)}")
        http("POST", f"{base}/_test/reset")
        stop_at = time.time() + phase_seconds
        q = mp.Queue()
        procs = [mp.Process(target=lake_reader, args=(quack_port, data, stop_at, q)) for _ in range(readers)]
        for p in procs:
            p.start()
        lat = {"GET /api/project": [], "POST /api/query": []}
        errs = []

        def hit(label, method, path, body=None):
            while time.time() < stop_at:
                t = time.perf_counter()
                try:
                    http(method, base + path, body)
                    lat[label].append((time.perf_counter() - t) * 1000)
                except Exception as e:
                    errs.append(f"{label}: {str(e)[:100]}")
                time.sleep(0.02)

        ping = []

        def pinger():
            # one reader-token connection and one allowed statement per call (quack_query: a plain
            # Quack attach rewrites the statement into a form the policy denies)
            from common import READER_TOKEN, connect
            c = connect(exts=("httpfs", "quack"))
            sql = "SELECT * FROM quack_query(?, 'SELECT count(*) FROM ducklake_snapshot', token := ?)"
            args = [f"quack://127.0.0.1:{quack_port}", READER_TOKEN]
            while time.time() < stop_at:
                t = time.perf_counter()
                try:
                    c.execute(sql, args).fetchall()
                    ping.append((time.perf_counter() - t) * 1000)
                except Exception as e:
                    errs.append(f"ping: {str(e)[:100]}")
                time.sleep(0.02)

        threads = [threading.Thread(target=hit, args=("GET /api/project", "GET", "/api/project")),
                   threading.Thread(target=hit, args=("POST /api/query", "POST", "/api/query", {"sql": "SELECT 42 AS x"}))]
        if quack:
            threads.append(threading.Thread(target=pinger))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        reads, read_errs, attaches = [], [], []
        for _ in procs:
            ms, e, a = q.get(timeout=300)
            reads += ms
            read_errs += e
            attaches.append(round(a))
        for p in procs:
            p.join()
        server = http("GET", f"{base}/_test/stats")
        results[name] = {
            **{k: summary(v) for k, v in lat.items()},
            "quack ping (1 statement)": summary(ping),
            "http errors": errs[:3],
            "lake attach ms": attaches,
            "lake reads (other processes)": {**summary(reads), "errors": len(read_errs), "first error": read_errs[:1]},
            "in-process writer commit": server.get("writer"),
            "in-process sandboxed read": server.get("sandbox"),
        }
        print(f"== {name}\n{json.dumps(results[name], indent=1)}", flush=True)
    http("POST", f"{base}/_test/hog?on=0")
    with open(out_json, "w") as f:
        json.dump(results, f, indent=1)


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5], sys.argv[6] if len(sys.argv) > 6 else None)
    else:
        load(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5])
        os._exit(0)
