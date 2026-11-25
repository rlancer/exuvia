"""End-to-end test of exuvia as a headless hub, driven over HTTP.

Two ways to run it:

* **Container mode** (default): builds the image from the repo's Dockerfile,
  starts it as ``exuvia hub /data``, streams ``examples/markets`` in as a
  template, tests it, and removes the container.
* **URL mode** (``E2E_URL`` set): tests a hub that is already running, for
  example one started from ``deploy/compose.yaml`` (Compose or Swarm) or
  ``deploy/kubernetes.yaml``. The markets project is uploaded over HTTP
  instead, and the projects the test creates are deleted afterwards.

Either way it walks the flow a host app relies on: ``/healthz``; the markets
project created (a repeat create is a 409), its graph read, every node run
and checked, a node's output and its stored Parquet artifact read, the view
listed and the views runtime fetched; then a second project written node by
node over HTTP, run, read back and deleted.

Nothing but the standard library is needed (and a container engine in
container mode)::

    python tests/e2e/container_e2e.py                            # or: mise run e2e
    E2E_URL=http://localhost:7420 E2E_TOKEN=change-me python tests/e2e/container_e2e.py

Environment:

    E2E_URL            test the hub at this address instead of starting one
    E2E_TOKEN          bearer token (the hub's EXUVIA_API_TOKEN), if it has one
    CONTAINER_ENGINE   podman | docker (default: podman if on PATH, else docker)
    E2E_IMAGE          image tag to build / use (default: exuvia:e2e)
    E2E_SKIP_BUILD     1: use E2E_IMAGE as it is, don't build it
    E2E_RUN_TIMEOUT    seconds a run may take (default: 180)

In container mode the container is always removed, and its logs are printed
when a step fails. Not collected by ``pytest`` (no ``test_`` prefix).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "examples" / "markets"
MARKETS_NODES = {"universe", "prices", "returns", "rolling_vol", "sector_summary"}
SKIP_PARTS = {".exuvia", "__pycache__", "node_modules"}
CONTAINER_PORT = 7420
TEMPLATES_DIR = "/tmp/exuvia-templates"
RUN_TIMEOUT = float(os.environ.get("E2E_RUN_TIMEOUT", "180"))


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def engine() -> str:
    chosen = os.environ.get("CONTAINER_ENGINE") or ("podman" if shutil.which("podman") else "docker")
    if not shutil.which(chosen):
        sys.exit(f"[e2e] container engine {chosen!r} is not on PATH (set CONTAINER_ENGINE)")
    return chosen


def sh(*args: str, check: bool = True, capture: bool = False, stdin: bytes | None = None) -> str:
    res = subprocess.run(args, input=stdin, capture_output=capture, check=False)
    if check and res.returncode != 0:
        err = res.stderr.decode(errors="replace") if capture and res.stderr else ""
        raise RuntimeError(f"`{' '.join(args)}` exited {res.returncode}\n{err}")
    return res.stdout.decode(errors="replace") if capture and res.stdout else ""


class Http:
    def __init__(self, base: str, token: str | None = None):
        self.base = base.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: Any = None, timeout: float = 30) -> tuple[int, dict[str, str], bytes]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def json(self, method: str, path: str, body: Any = None, expect: int = 200, timeout: float = 30) -> Any:
        status, _, raw = self.request(method, path, body, timeout)
        if status != expect:
            raise AssertionError(f"{method} {path}: expected {expect}, got {status}: {raw[:2000].decode(errors='replace')}")
        return json.loads(raw)


def check(cond: bool, what: str, detail: Any = None) -> None:
    if not cond:
        extra = f"\n{json.dumps(detail, indent=2, default=str)[:4000]}" if detail is not None else ""
        raise AssertionError(f"{what}{extra}")
    log(f"ok: {what}")


def template_files() -> list[Path]:
    """``examples/markets``'s files, minus local run state."""
    return [
        p for p in sorted(TEMPLATE.rglob("*"))
        if p.is_file() and not SKIP_PARTS.intersection(p.relative_to(TEMPLATE).parts)
    ]  # fmt: skip


def template_tar() -> bytes:
    """``examples/markets`` as ``markets/...``, for ``tar -x`` in the container."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in template_files():
            tar.add(p, arcname=f"markets/{p.relative_to(TEMPLATE).as_posix()}", recursive=False)
    return buf.getvalue()


def upload_markets(http: Http, api: str) -> None:
    """Write ``examples/markets``'s nodes and views into a project over HTTP."""
    for p in template_files():
        rel = p.relative_to(TEMPLATE)
        source = p.read_text(encoding="utf-8")
        if rel.parts[0] == "nodes":
            kind = "sql" if p.suffix == ".sql" else "python"
            http.json("PUT", f"{api}/nodes/{p.stem}", {"source": source, "kind": kind})
        elif rel.parts[0] == "views":
            view, file = rel.parts[1], "/".join(rel.parts[2:])
            if file == "view.json":
                http.json("PUT", f"{api}/views/{view}", json.loads(source))
            else:
                http.json("PUT", f"{api}/views/{view}/files/{file}", {"source": source})


def host_port(eng: str, name: str) -> int:
    # `port` prints e.g. "127.0.0.1:41234" (docker may add an IPv6 line).
    out = sh(eng, "port", name, f"{CONTAINER_PORT}/tcp", capture=True)
    for line in out.splitlines():
        line = line.strip()
        if line:
            return int(line.rsplit(":", 1)[1])
    raise RuntimeError(f"no host port published for {name}: {out!r}")


def wait_healthy(http: Http, alive: Callable[[], bool] = lambda: True, timeout: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if not alive():
            raise RuntimeError("container exited before it became healthy")
        try:
            status, _, raw = http.request("GET", "/healthz", timeout=3)
            if status == 200:
                return json.loads(raw)
            last = f"HTTP {status}"
        except (OSError, ValueError) as e:
            last = repr(e)
        time.sleep(1)
    raise RuntimeError(f"/healthz did not answer within {timeout:.0f}s (last: {last})")


def exercise(http: Http, demo: str, scratch: str, from_template: bool) -> None:
    listing = http.json("GET", "/api/projects")
    log(f"ok: the project list answers ({len(listing['projects'])} projects)")

    # -- the markets project ---------------------------------------------------
    api = f"/p/{demo}/api"
    if from_template:
        check("markets" in listing["templates"], "the markets template is offered", listing)
        created = http.json("POST", "/api/projects", {"id": demo, "template": "markets"})
        check(created["id"] == demo and created["name"] == "markets demo", "project created from the template", created)
    else:
        created = http.json("POST", "/api/projects", {"id": demo, "name": "markets demo"})
        check(created["id"] == demo, "project created", created)
        upload_markets(http, api)
        log("ok: examples/markets uploaded over HTTP")
    http.json("POST", "/api/projects", {"id": demo}, expect=409)
    log("ok: creating it again is a 409")

    graph = http.json("GET", f"{api}/project")
    names = {n["name"] for n in graph["nodes"]}
    check(names == MARKETS_NODES, "the graph has the markets nodes", sorted(names))
    check([v["name"] for v in graph["views"]] == ["vol_board"], "the graph has the markets view", graph["views"])

    run = http.json("POST", f"{api}/run", {"wait": RUN_TIMEOUT}, timeout=RUN_TIMEOUT + 30)
    check(run.get("running") is False, "the run finished within the wait", run)
    check(run["ok"] is True and not run["failed"], "every node ran successfully", run)
    check(set(run["runs"]) == MARKETS_NODES and set(run["runs"].values()) == {"success"}, "each node reports success", run["runs"])

    graph = http.json("GET", f"{api}/project")
    states = {n["name"]: n["state"] for n in graph["nodes"]}
    check(set(states.values()) == {"fresh"}, "every node is fresh afterwards", states)
    check(graph["views"][0]["state"] == "ready", "the view is ready", graph["views"][0])

    data = http.json("GET", f"{api}/nodes/sector_summary/data")
    cols = [c["name"] for c in data["columns"]]
    check(data["kind"] == "table" and data["rows"] and "avg_vol" in cols, "sector_summary has rows", data)
    prices = http.json("GET", f"{api}/nodes/prices/data?limit=5")
    # prices is partitioned: every business day since 2025-01-01, so the count grows with the date.
    check(
        prices["total"] % 8 == 0 and prices["total"] >= 8 * 261 and len(prices["rows"]) == 5,
        "prices has 8 tickers x every business day since 2025",
        prices,
    )
    parts = http.json("GET", f"{api}/nodes/prices/partitions")
    check(len(parts["partitions"]) * 8 == prices["total"], "prices has one partition per day", len(parts["partitions"]))

    rv = next(n for n in graph["nodes"] if n["name"] == "rolling_vol")
    status, headers, body = http.request("GET", f"{api}/artifacts/rolling_vol/{rv['artifact_id']}.parquet")
    check(status == 200 and body[:4] == b"PAR1" and body[-4:] == b"PAR1", "the rolling_vol artifact is a Parquet file", status)

    status, headers, body = http.request("GET", f"{api}/view-runtime/runtime.js")
    ctype = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
    check(status == 200 and "javascript" in ctype and len(body) > 10_000, f"the views runtime is served ({len(body)} bytes)", status)

    # The hub serves the web UI itself: no separate frontend to deploy.
    status, _, body = http.request("GET", f"/p/{demo}/")
    check(status == 200 and b'<div id="root">' in body, "the project's workbench page is served", status)
    status, _, body = http.request("GET", "/")
    check(status == 200 and b'name="exuvia-hub" content="home"' in body, "the hub's home page is served at /", status)

    view = http.json("GET", f"{api}/views/vol_board")
    paths = {s["path"] for s in view["sources"]}
    check({"view.json", "App.tsx"} <= paths, "the view's files are readable", sorted(paths))

    # -- a project built over HTTP --------------------------------------------
    http.json("POST", "/api/projects", {"id": scratch, "name": "Scratch"})
    source = (
        "import exuvia\n\n\n"
        "@exuvia.node()\n"
        "def squares():\n"
        "    import pandas as pd\n\n"
        "    return pd.DataFrame({'n': range(10), 'sq': [i * i for i in range(10)]})\n"
    )
    http.json("PUT", f"/p/{scratch}/api/nodes/squares", {"source": source, "kind": "python"})
    http.json("PUT", f"/p/{scratch}/api/nodes/total", {"source": "select sum(sq) as total from squares", "kind": "sql"})
    graph = http.json("GET", f"/p/{scratch}/api/project")
    deps = {n["name"]: n.get("deps") for n in graph["nodes"]}
    check(deps == {"squares": [], "total": ["squares"]}, "nodes written over HTTP are wired into the graph", deps)

    run = http.json("POST", f"/p/{scratch}/api/nodes/total/run", {"wait": RUN_TIMEOUT}, timeout=RUN_TIMEOUT + 30)
    check(run["ok"] is True and run["head"] == [{"total": 285}], "running a node runs its upstream and returns its head", run)

    http.json("DELETE", f"/api/projects/{scratch}")
    http.json("GET", f"/p/{scratch}/api/project", expect=404)
    ids = [p["id"] for p in http.json("GET", "/api/projects")["projects"]]
    check(scratch not in ids and demo in ids, "the scratch project is gone", ids)


def check_health(health: dict[str, Any]) -> None:
    check(health.get("ok") is True and health.get("mode") == "hub", "/healthz answers in hub mode", health)


def run_against_url(url: str) -> bool:
    http = Http(url, os.environ.get("E2E_TOKEN"))
    suffix = uuid.uuid4().hex[:6]
    demo, scratch = f"e2e-demo-{suffix}", f"e2e-scratch-{suffix}"
    log(f"testing the hub at {url}")
    try:
        check_health(wait_healthy(http))
        status, _, raw = Http(url).request("GET", "/api/projects")
        if http.token:
            check(status == 401, "the API refuses a request without the token", status)
        exercise(http, demo, scratch, from_template=False)
        return True
    except Exception as e:  # noqa: BLE001 -- report any failure, then clean up
        log(f"FAILED: {type(e).__name__}: {e}")
        return False
    finally:
        for pid in (demo, scratch):  # leave a long-lived hub as it was
            try:
                http.request("DELETE", f"/api/projects/{pid}")
            except OSError:
                pass


def run_in_container() -> bool:
    eng = engine()
    image = os.environ.get("E2E_IMAGE", "exuvia:e2e")
    name = f"exuvia-e2e-{uuid.uuid4().hex[:8]}"
    log(f"engine: {eng} ({sh(eng, '--version', capture=True).strip()})")

    if os.environ.get("E2E_SKIP_BUILD") == "1":
        log(f"using the existing image {image}")
    else:
        log(f"building {image}")
        sh(eng, "build", "-t", image, str(REPO))

    ok = False
    try:
        sh(
            eng, "run", "-d", "--name", name,
            "-p", f"127.0.0.1::{CONTAINER_PORT}",
            "-e", "EXUVIA_SCHEDULER=external",
            image,
            "exuvia", "hub", "/data", "--host", "0.0.0.0", "--port", str(CONTAINER_PORT), "--templates", TEMPLATES_DIR,
        )  # fmt: skip
        # Templates are read on each request, so they can arrive after start.
        sh(eng, "exec", "-i", name, "sh", "-c", f"mkdir -p {TEMPLATES_DIR} && tar -xf - -C {TEMPLATES_DIR}", stdin=template_tar())
        port = host_port(eng, name)
        log(f"container {name} serving on http://127.0.0.1:{port}")
        http = Http(f"http://127.0.0.1:{port}")

        def alive() -> bool:
            return sh(eng, "inspect", "-f", "{{.State.Running}}", name, capture=True, check=False).strip() != "false"

        check_health(wait_healthy(http, alive))
        exercise(http, "demo", "scratch", from_template=True)
        ok = True
    except Exception as e:  # noqa: BLE001 -- report any failure, then clean up
        log(f"FAILED: {type(e).__name__}: {e}")
    finally:
        if not ok:
            log(f"container logs ({name}):")
            sh(eng, "logs", name, check=False)
        sh(eng, "rm", "-f", name, check=False, capture=True)
    return ok


def main() -> int:
    url = os.environ.get("E2E_URL")
    ok = run_against_url(url) if url else run_in_container()
    log("PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
