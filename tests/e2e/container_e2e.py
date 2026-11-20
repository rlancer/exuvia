"""End-to-end test of the exuvia container image, driven over HTTP.

Builds the image from the repo's Dockerfile, starts it as a headless hub
(``exuvia hub /data``), and walks the flow a host app relies on:

* ``/healthz``;
* a project created from a template (``examples/markets``, streamed into the
  container), its graph read, every node run and checked, a node's output
  and its stored artifact read, the view listed and the views runtime
  fetched;
* a project built from scratch over HTTP: a node written, run, read back,
  then the project deleted.

Runs with podman locally and docker in CI; nothing but the standard library
and a container engine is needed::

    python tests/e2e/container_e2e.py          # or: mise run e2e

Environment:

    CONTAINER_ENGINE   podman | docker (default: podman if on PATH, else docker)
    E2E_IMAGE          image tag to build / use (default: exuvia:e2e)
    E2E_SKIP_BUILD     1: use E2E_IMAGE as it is, don't build it
    E2E_RUN_TIMEOUT    seconds a run may take (default: 180)

The container is always removed; its logs are printed when a step fails.
Not collected by ``pytest`` (no ``test_`` prefix).
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
from typing import Any

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "examples" / "markets"
MARKETS_NODES = {"universe", "prices", "returns", "rolling_vol", "sector_summary"}
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
    def __init__(self, base: str):
        self.base = base

    def request(self, method: str, path: str, body: Any = None, timeout: float = 30) -> tuple[int, dict[str, str], bytes]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
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


def template_tar() -> bytes:
    """``examples/markets`` as ``markets/...``, minus local run state."""
    buf = io.BytesIO()
    skip = {".exuvia", "__pycache__", "node_modules"}
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in sorted(TEMPLATE.rglob("*")):
            rel = p.relative_to(TEMPLATE)
            if p.is_dir() or skip.intersection(rel.parts):
                continue
            tar.add(p, arcname=f"markets/{rel.as_posix()}", recursive=False)
    return buf.getvalue()


def host_port(eng: str, name: str) -> int:
    # `port` prints e.g. "127.0.0.1:41234" (docker may add an IPv6 line).
    out = sh(eng, "port", name, f"{CONTAINER_PORT}/tcp", capture=True)
    for line in out.splitlines():
        line = line.strip()
        if line:
            return int(line.rsplit(":", 1)[1])
    raise RuntimeError(f"no host port published for {name}: {out!r}")


def wait_healthy(http: Http, eng: str, name: str, timeout: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        state = sh(eng, "inspect", "-f", "{{.State.Running}}", name, capture=True, check=False).strip()
        if state == "false":
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


def exercise(http: Http, eng: str, name: str) -> None:
    health = wait_healthy(http, eng, name)
    check(health.get("ok") is True and health.get("mode") == "hub", "/healthz answers in hub mode", health)

    listing = http.json("GET", "/api/projects")
    check("markets" in listing["templates"], "the markets template is offered", listing)

    # -- a project from a template --------------------------------------------
    created = http.json("POST", "/api/projects", {"id": "demo", "template": "markets"})
    check(created["id"] == "demo" and created["name"] == "markets demo", "project created from the template", created)
    http.json("POST", "/api/projects", {"id": "demo", "template": "markets"}, expect=409)
    log("ok: creating it again is a 409")

    api = "/p/demo/api"
    graph = http.json("GET", f"{api}/project")
    names = {n["name"] for n in graph["nodes"]}
    check(names == MARKETS_NODES, "the graph has the template's nodes", sorted(names))
    check([v["name"] for v in graph["views"]] == ["vol_board"], "the graph has the template's view", graph["views"])

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
    check(prices["total"] == 8 * 260 and len(prices["rows"]) == 5, "prices has 8 tickers x 260 days", prices)

    rv = next(n for n in graph["nodes"] if n["name"] == "rolling_vol")
    status, headers, body = http.request("GET", f"{api}/artifacts/rolling_vol/{rv['artifact_id']}.parquet")
    check(status == 200 and body[:4] == b"PAR1" and body[-4:] == b"PAR1", "the rolling_vol artifact is a Parquet file", status)

    status, headers, body = http.request("GET", f"{api}/view-runtime/runtime.js")
    ctype = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
    check(status == 200 and "javascript" in ctype and len(body) > 10_000, f"the views runtime is served ({len(body)} bytes)", status)

    view = http.json("GET", f"{api}/views/vol_board")
    paths = {s["path"] for s in view["sources"]}
    check({"view.json", "App.tsx"} <= paths, "the view's files are readable", sorted(paths))

    # -- a project built over HTTP --------------------------------------------
    http.json("POST", "/api/projects", {"id": "scratch", "name": "Scratch"})
    source = (
        "import exuvia\n\n\n"
        "@exuvia.node()\n"
        "def squares():\n"
        "    import pandas as pd\n\n"
        "    return pd.DataFrame({'n': range(10), 'sq': [i * i for i in range(10)]})\n"
    )
    http.json("PUT", "/p/scratch/api/nodes/squares", {"source": source, "kind": "python"})
    sql = "select sum(sq) as total from squares"
    http.json("PUT", "/p/scratch/api/nodes/total", {"source": sql, "kind": "sql"})
    graph = http.json("GET", "/p/scratch/api/project")
    deps = {n["name"]: n.get("deps") for n in graph["nodes"]}
    check(deps == {"squares": [], "total": ["squares"]}, "nodes written over HTTP are wired into the graph", deps)

    run = http.json("POST", "/p/scratch/api/nodes/total/run", {"wait": RUN_TIMEOUT}, timeout=RUN_TIMEOUT + 30)
    check(run["ok"] is True and run["head"] == [{"total": 285}], "running a node runs its upstream and returns its head", run)

    http.json("DELETE", "/api/projects/scratch")
    http.json("GET", "/p/scratch/api/project", expect=404)
    ids = [p["id"] for p in http.json("GET", "/api/projects")["projects"]]
    check(ids == ["demo"], "the scratch project is gone", ids)


def main() -> int:
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
        exercise(Http(f"http://127.0.0.1:{port}"), eng, name)
        ok = True
    except Exception as e:  # noqa: BLE001 -- report any failure, then clean up
        log(f"FAILED: {type(e).__name__}: {e}")
    finally:
        if not ok:
            log(f"container logs ({name}):")
            sh(eng, "logs", name, check=False)
        sh(eng, "rm", "-f", name, check=False, capture=True)
    log("PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
