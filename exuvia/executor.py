"""Where nodes actually run.

The control plane (``Workspace``) decides *what* runs and *when*; an
``Executor`` runs one node and hands back a content-addressed artifact.

* ``LocalExecutor`` — a pool of pre-warmed, single-use subprocesses on this
  machine (the default).
* ``RemoteExecutor`` — the same thing behind HTTP (``exuvia executor``), so
  execution can live in a container anywhere: Cloudflare Containers, Fly,
  Cloud Run, Kubernetes, a beefy box at the office. All traffic flows from the
  control plane to the executor, so a laptop can drive a cloud executor
  without being reachable from it.

Both satisfy the same contract and are exercised by the same tests.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from . import packages
from .pool import WarmPool
from .project import VIEW_DIR

RUN_TIMEOUT_S = int(os.environ.get("EXUVIA_RUN_TIMEOUT", "1800"))
BUNDLE_MAX_BYTES = int(os.environ.get("EXUVIA_BUNDLE_MAX_MB", "50")) * 1024 * 1024
SKIP_DIRS = {"__pycache__", "node_modules"}

LogFn = Callable[[str], None]


@dataclass
class ExecRequest:
    run_id: str
    node: str
    kind: str  # python | sql
    path: str  # node file, relative to root
    root: Path  # project root (code + helpers + data)
    out_dir: Path  # where the artifact must end up (control-plane side)
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)  # dep -> {path, kind, artifact_id}
    params: dict[str, Any] = field(default_factory=dict)  # the run parameters this node declared
    env: dict[str, str] = field(default_factory=dict)  # the project's secrets (project_secrets.py)


@dataclass
class ExecResult:
    ok: bool
    error: str | None = None
    artifact_id: str | None = None
    artifact_path: str | None = None
    kind: str | None = None
    rows: int | None = None
    schema: list[dict[str, str]] | None = None

    @classmethod
    def from_runner(cls, d: dict[str, Any]) -> "ExecResult":
        return cls(
            ok=bool(d.get("ok")),
            error=d.get("error"),
            artifact_id=d.get("artifact_id"),
            artifact_path=d.get("artifact_path"),
            kind=d.get("kind"),
            rows=d.get("rows"),
            schema=d.get("schema"),
        )


class Executor(Protocol):
    def start(self) -> None: ...
    def close(self) -> None: ...
    def execute(self, req: ExecRequest, on_log: LogFn) -> ExecResult:
        """Blocking. Streams output lines to ``on_log``."""
        ...
    def cancel(self, run_id: str) -> bool: ...


# -- local ------------------------------------------------------------------
class LocalExecutor:
    def __init__(self, cwd: Path, workers: int, packages_dir: Path | None = None):
        """``packages_dir`` holds the installs of the packages projects
        declare in their pyproject.toml, shared by every project run here (an
        executor service); by default each project keeps its own, under
        ``.exuvia/packages``."""
        self.pool = WarmPool(cwd, size=workers)
        self.packages_dir = packages_dir
        # A workspace's live view of its project's packages (set by
        # Workspace): installs through it show up in the workbench.
        self.packages: packages.Packages | None = None
        self._procs: dict[str, Any] = {}
        self._cancelled: set[str] = set()

    def start(self) -> None:
        self.pool.start()

    def close(self) -> None:
        for p in list(self._procs.values()):
            p.kill()
        self.pool.close()

    def execute(self, req: ExecRequest, on_log: LogFn) -> ExecResult:
        try:
            if self.packages is not None:
                site = self.packages.ensure_for_run(on_log)
            else:
                site = packages.ensure(
                    req.root,
                    self.packages_dir or req.root / ".exuvia" / "packages",
                    on_log,
                    prune=self.packages_dir is None,
                )
        except packages.PackagesError as e:
            on_log(f"{e}\n")
            return ExecResult(ok=False, error=f"the packages in {packages.PYPROJECT} could not be installed: {_summary(str(e))}")
        with tempfile.TemporaryDirectory(prefix="exuvia-") as tmp:
            result_path = Path(tmp) / "result.json"
            spec = {
                "root": str(req.root),
                "site": str(site) if site else None,
                "node": req.node,
                "kind": req.kind,
                "path": req.path,
                "inputs": {d: {"path": i["path"], "kind": i["kind"]} for d, i in req.inputs.items()},
                "params": req.params,
                "out_dir": str(req.out_dir),
                "result_path": str(result_path),
            }
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")

            proc = self.pool.take()
            assert proc.stdin and proc.stdout
            proc.stdin.write(f"{spec_path}\n".encode())
            proc.stdin.write(json.dumps(req.env).encode() + b"\n")  # over the pipe, never in a file
            proc.stdin.close()
            self._procs[req.run_id] = proc
            timer = threading.Timer(RUN_TIMEOUT_S, proc.kill)
            timer.start()
            try:
                for raw in iter(proc.stdout.readline, b""):
                    on_log(raw.decode("utf-8", errors="replace"))
                code = proc.wait()
            finally:
                timer.cancel()
                self._procs.pop(req.run_id, None)

            if req.run_id in self._cancelled:
                self._cancelled.discard(req.run_id)
                return ExecResult(ok=False, error="cancelled")
            if not result_path.exists():
                return ExecResult(ok=False, error=f"runner exited with code {code}")
            return ExecResult.from_runner(json.loads(result_path.read_text(encoding="utf-8")))

    def cancel(self, run_id: str) -> bool:
        proc = self._procs.get(run_id)
        if proc is None:
            return False
        self._cancelled.add(run_id)
        proc.kill()
        return True


def _summary(text: str) -> str:
    """What went wrong in an installer's output, on one line, for a run's
    error; the log has all of it. uv explains a failed resolution over
    several lines, after `╰─▶` (or `cause:` in its plain output), and other
    failures in its last line."""
    lines = [ln.strip() for ln in text.strip().splitlines()]
    for i, line in enumerate(lines):
        if "╰─▶" in line or line.startswith("cause:"):
            parts = [line.split("╰─▶", 1)[1] if "╰─▶" in line else line[len("cause:"):]]
            for more in lines[i + 1:]:
                if not more or more.startswith(("hint:", "help:", "cause:")):
                    break
                parts.append(more)
            if packages.CONFLICT_HINT in text:
                parts.append("(the == versions are the ones exuvia runs with)")
            return " ".join(p.strip() for p in parts)[:500]
    rest = [ln for ln in lines if ln and ln != packages.CONFLICT_HINT]
    return (rest[-1] if rest else text)[:500]


# -- project bundles ----------------------------------------------------------
def bundle_files(root: Path) -> list[Path]:
    """Files shipped to a remote executor: the project minus state, VCS,
    hidden files, caches and views (UI code no node can read, so editing a
    view does not ship a new bundle)."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        skip = SKIP_DIRS | {VIEW_DIR} if Path(dirpath) == root else SKIP_DIRS
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in skip)
        for f in sorted(filenames):
            if not f.startswith("."):
                out.append(Path(dirpath) / f)
    return out


def bundle_hash_of(entries: list[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for rel, data in sorted(entries):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()[:24]


def make_bundle(root: Path) -> tuple[str, bytes]:
    entries: list[tuple[str, bytes]] = []
    total = 0
    for p in bundle_files(root):
        data = p.read_bytes()
        total += len(data)
        if total > BUNDLE_MAX_BYTES:
            raise RuntimeError(
                f"project is larger than EXUVIA_BUNDLE_MAX_MB ({BUNDLE_MAX_BYTES // 2**20} MB); "
                "keep large data in object storage, not the project folder"
            )
        entries.append((p.relative_to(root).as_posix(), data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, data in entries:
            z.writestr(rel, data)
    return bundle_hash_of(entries), buf.getvalue()


def bundle_hash_of_zip(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return bundle_hash_of([(n, z.read(n)) for n in z.namelist() if not n.endswith("/")])


# -- remote -------------------------------------------------------------------
class RemoteExecutor:
    """Client for ``exuvia executor``. Protocol (all requests control plane → executor):

    HEAD /v1/bundles/{hash}                 is this project version cached?
    PUT  /v1/bundles/{hash}                 upload project zip (verified by hash)
    PUT  /v1/runs/{id}/inputs/{dep}         upload an upstream artifact
    POST /v1/runs/{id}                      run (with the project's secrets in ``env``);
                                            response is NDJSON: log lines, then a result
    GET  /v1/runs/{id}/output               download the artifact
    DELETE /v1/runs/{id}                    clean up
    POST /v1/runs/{id}/cancel
    """

    def __init__(self, url: str, token: str | None = None):
        import httpx

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.url = url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.url, headers=headers, timeout=httpx.Timeout(60.0, read=RUN_TIMEOUT_S + 60)
        )
        self._bundle_cache: tuple[tuple, str, bytes] | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        pass

    def close(self) -> None:
        self.http.close()

    def _bundle(self, root: Path) -> str:
        files = bundle_files(root)
        sig = tuple((p.as_posix(), p.stat().st_size, p.stat().st_mtime_ns) for p in files)
        with self._lock:
            if self._bundle_cache and self._bundle_cache[0] == sig:
                digest = self._bundle_cache[1]
                blob = self._bundle_cache[2]
            else:
                digest, blob = make_bundle(root)
                self._bundle_cache = (sig, digest, blob)
        if self.http.head(f"/v1/bundles/{digest}").status_code != 200:
            self.http.put(f"/v1/bundles/{digest}", content=blob).raise_for_status()
        return digest

    def execute(self, req: ExecRequest, on_log: LogFn) -> ExecResult:
        try:
            digest = self._bundle(req.root)
            for dep, inp in req.inputs.items():
                if inp.get("path"):
                    with open(inp["path"], "rb") as f:
                        self.http.put(f"/v1/runs/{req.run_id}/inputs/{dep}", content=f.read()).raise_for_status()
            spec = {
                "bundle": digest,
                "node": req.node,
                "kind": req.kind,
                "path": req.path,
                "inputs": {d: {"kind": i["kind"], "has_file": bool(i.get("path"))} for d, i in req.inputs.items()},
                "params": req.params,
                "env": req.env,
            }
            result: dict[str, Any] | None = None
            with self.http.stream("POST", f"/v1/runs/{req.run_id}", json=spec) as resp:
                if resp.status_code != 200:
                    resp.read()
                    return ExecResult(ok=False, error=f"executor HTTP {resp.status_code}: {resp.text[:300]}")
                for line in resp.iter_lines():
                    if not line:
                        continue
                    ev = json.loads(line)
                    if ev["type"] == "log":
                        on_log(ev["text"])
                    elif ev["type"] == "result":
                        result = ev["result"]
            if result is None:
                return ExecResult(ok=False, error="executor closed the stream without a result")
            res = ExecResult.from_runner(result)
            if res.ok and res.kind not in (None, "none"):
                res.artifact_path = self._download(req, res)
            return res
        except Exception as e:  # network / protocol errors become run failures
            return ExecResult(ok=False, error=f"executor error: {type(e).__name__}: {e}")
        finally:
            try:
                self.http.delete(f"/v1/runs/{req.run_id}")
            except Exception:
                pass

    def _download(self, req: ExecRequest, res: ExecResult) -> str:
        ext = {"table": "parquet", "json": "json"}[res.kind or ""]
        req.out_dir.mkdir(parents=True, exist_ok=True)
        final = req.out_dir / f"{res.artifact_id}.{ext}"
        if final.exists():
            return str(final)
        fd, tmp = tempfile.mkstemp(dir=req.out_dir, suffix=".part")
        os.close(fd)
        h = hashlib.sha256()
        with self.http.stream("GET", f"/v1/runs/{req.run_id}/output") as resp, open(tmp, "wb") as f:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest()[:16] != res.artifact_id:
            os.unlink(tmp)
            raise RuntimeError("artifact hash mismatch after download")
        os.replace(tmp, final)
        return str(final)

    def cancel(self, run_id: str) -> bool:
        try:
            return self.http.post(f"/v1/runs/{run_id}/cancel").json().get("ok", False)
        except Exception:
            return False


def make_executor(root: Path, workers: int) -> Executor:
    url = os.environ.get("EXUVIA_EXECUTOR_URL")
    if url:
        return RemoteExecutor(url, os.environ.get("EXUVIA_EXECUTOR_TOKEN"))
    return LocalExecutor(root, workers)
