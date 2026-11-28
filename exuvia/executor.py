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
import re
import tempfile
import threading
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from . import packages
from .pool import WarmPool, kill
from .project import VIEW_DIR

RUN_TIMEOUT_S = int(os.environ.get("EXUVIA_RUN_TIMEOUT", "1800"))
BUNDLE_MAX_BYTES = int(os.environ.get("EXUVIA_BUNDLE_MAX_MB", "50")) * 1024 * 1024
SKIP_DIRS = {"__pycache__", "node_modules"}

# Per-run cap on stored/streamed log text; 0 turns the cap off.
MAX_LOG_BYTES = int(os.environ.get("EXUVIA_MAX_LOG_KB", "1024")) * 1024

LogFn = Callable[[str], None]


def _kb(n: int) -> str:
    return f"{n / 1024:,.0f} KB" if n < 10 * 1024 * 1024 else f"{n / 1024 / 1024:,.1f} MB"


class CappedLog:
    """A ``LogFn`` that passes at most about ``limit`` bytes of one run's
    output on to ``sink``: the first three quarters as they arrive, then the
    last quarter when ``close()`` is called, with a marker line at the cut and
    another saying how much was dropped from the middle. The tail matters
    because a failing node's traceback is written last.

    Sizes are tracked here, so the sink (the run store, the event bus, an
    NDJSON stream) never needs to re-read what it already has. Calls are
    serialized: a runner's output arrives on the threads of its HTTP
    requests. ``close()`` goes after the executor returns, and what comes
    after it on a truncated log is not kept."""

    def __init__(self, sink: LogFn, limit: int | None = None):
        self.sink = sink
        self.limit = MAX_LOG_BYTES if limit is None else limit
        self.tail_limit = self.limit // 4
        self.head_limit = self.limit - self.tail_limit
        self.size = 0  # bytes passed through as the head
        self.dropped = 0  # bytes that will appear in neither head nor tail
        self.truncated = False
        self._tail: deque[bytes] = deque()
        self._tail_size = 0
        self._lock = threading.Lock()

    def __call__(self, text: str) -> None:
        with self._lock:
            self._take(text)

    def _take(self, text: str) -> None:
        if self.limit <= 0:
            self.sink(text)
            return
        data = text.encode("utf-8", errors="replace")
        if not self.truncated:
            room = self.head_limit - self.size
            if len(data) <= room:
                self.size += len(data)
                self.sink(text)
                return
            head, data = data[:room], data[room:]
            if head:
                self.sink(head.decode("utf-8", errors="ignore"))
            self.size = self.head_limit
            self.truncated = True
            self.sink(
                f"\n[exuvia] log truncated: output reached {_kb(self.head_limit)}; from here on only the last "
                f"{_kb(self.tail_limit)} is kept, added when the run ends (EXUVIA_MAX_LOG_KB)\n"
            )
        self._tail.append(data)
        self._tail_size += len(data)
        while self._tail_size > self.tail_limit:
            excess = self._tail_size - self.tail_limit
            first = self._tail[0]
            if len(first) <= excess:
                self._tail.popleft()
                cut = len(first)
            else:
                self._tail[0] = first[excess:]
                cut = excess
            self._tail_size -= cut
            self.dropped += cut

    def close(self) -> None:
        """Write the kept tail. Safe to call more than once."""
        with self._lock:
            self._close()

    def _close(self) -> None:
        if not self.truncated or not self._tail:
            return
        tail = b"".join(self._tail)
        self._tail.clear()
        self._tail_size = 0
        if self.dropped:
            # start the kept tail on a line boundary when there is one
            nl = tail.find(b"\n")
            if 0 <= nl < len(tail) - 1:
                self.dropped += nl + 1
                tail = tail[nl + 1 :]
            self.sink(f"[exuvia] log truncated: {_kb(self.dropped)} dropped; the last part follows\n")
        self.sink(tail.decode("utf-8", errors="ignore"))


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
    # The project's environment variables (envvars.py), set in the node's
    # process on top of the environment it starts with.
    env: dict[str, str] = field(default_factory=dict)
    # A partitioned node (partitioned.py): its partition column, and the
    # partition values its output holds (empty on a full refresh), which
    # the node reads with exuvia.partitions().
    partition_by: str | None = None
    partitions: list[str] = field(default_factory=list)
    # Seconds the attempt may take (the node's ``timeout``); None: RUN_TIMEOUT_S.
    timeout: float | None = None
    # Whether the run was cancelled before its process started (the control
    # plane's view): checked before starting it, so a cancel that arrives
    # while inputs are read or packages install is not lost.
    cancelled: Callable[[], bool] | None = None

    @property
    def time_limit(self) -> float:
        return self.timeout or RUN_TIMEOUT_S

    def is_cancelled(self) -> bool:
        return bool(self.cancelled and self.cancelled())


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
    def __init__(
        self, cwd: Path, workers: int, packages_dir: Path | None = None, pool: WarmPool | None = None,
    ):
        """``packages_dir`` holds the installs of the packages projects
        declare in their pyproject.toml, shared by every project run here (an
        executor service); by default each project keeps its own, under
        ``.exuvia/packages``. ``pool``: warm runners shared with other
        executors (a hub's, Hub.pool), which its owner starts and closes;
        by default the executor has a pool of ``workers`` of its own."""
        self._owns_pool = pool is None
        self.pool = WarmPool(cwd, size=workers) if pool is None else pool
        self.packages_dir = packages_dir
        # A workspace's live view of its project's packages (set by
        # Workspace): installs through it show up in the workbench.
        self.packages: packages.Packages | None = None
        self._procs: dict[str, Any] = {}
        self._starting: set[str] = set()  # runs in execute() whose process has not started yet
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._owns_pool:
            self.pool.start()

    def close(self) -> None:
        """Stop this executor's runs, and its own pool (a shared one stays)."""
        for p in list(self._procs.values()):
            kill(p)
        if self._owns_pool:
            self.pool.close()

    def execute(self, req: ExecRequest, on_log: LogFn) -> ExecResult:
        with self._lock:
            self._starting.add(req.run_id)
        try:
            return self._execute(req, on_log)
        finally:
            with self._lock:
                self._starting.discard(req.run_id)
                self._cancelled.discard(req.run_id)

    def _execute(self, req: ExecRequest, on_log: LogFn) -> ExecResult:
        try:
            if self.packages is not None:
                site = self.packages.ensure_for_run(on_log)
            else:
                site = packages.ensure(
                    req.root,
                    self.packages_dir or req.root / ".exuvia" / "packages",
                    on_log,
                    shared=self.packages_dir is not None,
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
                "inputs": {
                    d: {"path": i.get("path"), "kind": i["kind"], **({"paths": i["paths"]} if "paths" in i else {})}
                    for d, i in req.inputs.items()
                },
                "params": req.params,
                "env": req.env,
                "partition_by": req.partition_by,
                "partitions": req.partitions,
                "out_dir": str(req.out_dir),
                "result_path": str(result_path),
            }
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")

            with self._lock:
                if req.run_id in self._cancelled or req.is_cancelled():  # before it could start
                    return ExecResult(ok=False, error="cancelled")
                proc = self.pool.take()
                self._procs[req.run_id] = proc
            assert proc.stdin and proc.stdout
            proc.stdin.write(f"{spec_path}\n".encode())
            proc.stdin.close()
            timed_out = threading.Event()

            def time_up() -> None:
                timed_out.set()
                kill(proc)

            timer = threading.Timer(req.time_limit, time_up)
            timer.start()
            try:
                for raw in iter(proc.stdout.readline, b""):
                    on_log(raw.decode("utf-8", errors="replace"))
                code = proc.wait()
            finally:
                timer.cancel()
                with self._lock:
                    self._procs.pop(req.run_id, None)

            if req.run_id in self._cancelled:
                return ExecResult(ok=False, error="cancelled")
            if timed_out.is_set():
                why = "the node's timeout" if req.timeout else "EXUVIA_RUN_TIMEOUT; set timeout= on the node to change it"
                error = f"timed out after {req.time_limit:g}s ({why})"
                on_log(f"\nexuvia: {error}: the attempt was stopped\n")
                return ExecResult(ok=False, error=error)
            if not result_path.exists():
                return ExecResult(ok=False, error=exit_reason(code))
            return ExecResult.from_runner(json.loads(result_path.read_text(encoding="utf-8")))

    def cancel(self, run_id: str) -> bool:
        """Stop a run: kill its process, or keep it from starting while its
        packages are still installing. False when it is not running here."""
        with self._lock:
            proc = self._procs.get(run_id)
            if proc is None and run_id not in self._starting:
                return False
            self._cancelled.add(run_id)
        if proc is not None:
            kill(proc)
        return True


def exit_reason(code: int) -> str:
    """Why a runner ended without a result, for the run's error. A negative
    code is the signal that killed it (POSIX); a SIGKILL exuvia did not send
    is nearly always the kernel's out-of-memory killer."""
    if code >= 0:
        return f"runner exited with code {code}"
    import signal

    try:
        name = "SIGKILL" if -code == 9 else signal.Signals(-code).name
    except ValueError:
        name = f"signal {-code}"
    if -code == 9:
        return (
            f"runner was killed ({name}, exit code {code}): most likely it ran out of memory and the system's "
            "out-of-memory killer stopped it, or something outside exuvia killed it"
        )
    return f"runner was killed ({name}, exit code {code})"


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
    POST /v1/chunks/missing                 which of a partitioned input's files it lacks
    PUT  /v1/chunks/{hash}                  upload one (verified by hash, cached across runs)
    POST /v1/runs/{id}                      run; response is NDJSON: log lines, then a result
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
        self._chunks_ok: bool | None = None

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

    def _chunks(self, paths: list[str]) -> list[str] | None:
        """Send the files of a partitioned input that the executor doesn't
        already hold, and return their ids. None when the executor is an
        older one, which takes the input as a single file."""
        if self._chunks_ok is None:
            try:
                self._chunks_ok = bool(self.http.get("/healthz").json().get("chunks"))
            except Exception:  # noqa: BLE001 - an older executor answers /healthz anyhow (or not at all): no chunks then
                self._chunks_ok = False
        ids = [Path(p).stem for p in paths]
        if not self._chunks_ok or not all(CHUNK_ID.match(i) for i in ids):
            return None
        resp = self.http.post("/v1/chunks/missing", json={"chunks": ids})
        resp.raise_for_status()
        missing = set(resp.json()["missing"])
        for p, i in zip(paths, ids, strict=True):
            if i in missing:
                with open(p, "rb") as f:
                    self.http.put(f"/v1/chunks/{i}", content=f.read()).raise_for_status()
        return ids

    def execute(self, req: ExecRequest, on_log: LogFn) -> ExecResult:
        try:
            digest = self._bundle(req.root)
            chunked: dict[str, list[str]] = {}
            for dep, inp in req.inputs.items():
                if inp.get("paths") is not None:
                    ids = self._chunks(inp["paths"])
                    if ids is not None:
                        chunked[dep] = ids
                        continue
                    blob = _combined(inp["paths"])  # an executor without a chunk cache
                    self.http.put(f"/v1/runs/{req.run_id}/inputs/{dep}", content=blob).raise_for_status()
                elif inp.get("path"):
                    with open(inp["path"], "rb") as f:
                        self.http.put(f"/v1/runs/{req.run_id}/inputs/{dep}", content=f.read()).raise_for_status()
            spec = {
                "bundle": digest,
                "node": req.node,
                "kind": req.kind,
                "path": req.path,
                "inputs": {
                    d: {"kind": i["kind"], "chunks": chunked[d]}
                    if d in chunked
                    else {"kind": i["kind"], "has_file": bool(i.get("path")) or i.get("paths") is not None}
                    for d, i in req.inputs.items()
                },
                "params": req.params,
                "env": req.env,
                "partition_by": req.partition_by,
                "partitions": req.partitions,
                "timeout": req.timeout,
            }
            result: dict[str, Any] | None = None
            if req.is_cancelled():
                return ExecResult(ok=False, error="cancelled")
            import httpx

            wait = httpx.Timeout(60.0, read=req.time_limit + 60)
            with self.http.stream("POST", f"/v1/runs/{req.run_id}", json=spec, timeout=wait) as resp:
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
        except Exception as e:  # noqa: BLE001 - network / protocol errors become run failures
            return ExecResult(ok=False, error=f"executor error: {type(e).__name__}: {e}")
        finally:
            try:
                self.http.delete(f"/v1/runs/{req.run_id}")
            except Exception:  # noqa: BLE001 - best effort: cleaning up must not change the run's result
                pass

    def _download(self, req: ExecRequest, res: ExecResult) -> str:
        ext = {"table": "parquet", "json": "json", "multi": "zip"}[res.kind or ""]
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
        except Exception:  # noqa: BLE001 - best effort: an executor that can't be reached has nothing to stop
            return False


CHUNK_ID = re.compile(r"^[0-9a-f]{16}$")


def _combined(paths: list[str]) -> bytes:
    """A partitioned output's chunk files as one Parquet file's bytes."""
    import duckdb

    with tempfile.TemporaryDirectory(prefix="exuvia-") as tmp:
        out = Path(tmp) / "combined.parquet"
        con = duckdb.connect()
        if paths:
            files = ", ".join("'" + p.replace("'", "''") + "'" for p in paths)
            con.execute(f"copy (select * from read_parquet([{files}], union_by_name = true)) to '{out.as_posix()}' (format parquet)")
        else:
            import pandas as pd

            pd.DataFrame().to_parquet(out)
        con.close()
        return out.read_bytes()


def make_executor(root: Path, workers: int, pool: WarmPool | None = None) -> Executor:
    url = os.environ.get("EXUVIA_EXECUTOR_URL")
    if url:
        return RemoteExecutor(url, os.environ.get("EXUVIA_EXECUTOR_TOKEN"))
    return LocalExecutor(root, workers, pool=pool)
