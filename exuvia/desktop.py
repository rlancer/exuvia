"""``exuvia runner``: run ``runs_on`` nodes on this machine (the runner's half).

    exuvia runner --url https://hub.example.com --label desk

signs in with ``EXUVIA_API_TOKEN`` (your own agent token from the
workbench's account menu), long-polls the hub for runs of nodes marked
``runs_on="desk"`` in projects you can edit, runs each one here with
the usual subprocess runner, streams its log back and uploads its output,
which the hub checks by hash. Only outbound HTTPS: nothing has to reach
this machine. See runners.py for the hub's half.

Code approval. Anyone who can edit a project (its agent included) could
otherwise run code on this machine. So the runner runs only code you have
approved here: a node's file, the ``lib/`` files it imports and the
project's ``pyproject.toml``, ``uv.lock`` and ``exuvia.json``, by their
hash. New or changed code shows its diff from the last version you
approved and waits for a yes when the runner runs in a terminal; run in the
background, it fails the run, keeps the code under ``pending/`` and
``exuvia runner approve`` shows and approves it. Approvals live on this
machine only (``EXUVIA_RUNNER_HOME``, default ``~/.exuvia/runner``): the hub
can't grant one.

Packages: the project's ``pyproject.toml`` is installed here (``uv``) into
the runner's own store, as the hub's installer does.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .runners import files_hash

LOG_EVERY_S = 1.0  # how often buffered log text goes to the hub
HEARTBEAT_S = 10.0  # at most this long between two calls while a run is held


class RunnerStop(Exception):
    """The runner can't go on (bad address, refused token): the message says why."""


def home() -> Path:
    return Path(os.environ.get("EXUVIA_RUNNER_HOME") or Path.home() / ".exuvia" / "runner")


def hub_url(url: str) -> str:
    """The hub's address from a project's (``<hub>/p/<project>``) or the hub's own."""
    parts = urlsplit(url.strip())
    path = re.sub(r"/p/[^/]+(/.*)?$", "", parts.path).rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


# -- approvals ----------------------------------------------------------------------
class Approvals:
    """The code this machine's owner has approved for runners here, by
    (hub, project, node): the hashes approved and the last approved files
    (to show what changed). ``pending/`` holds code a run brought that
    nobody has approved yet."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.file = folder / "approved.json"
        self.pending_dir = folder / "pending"

    @staticmethod
    def key(where: str, project: str, node: str) -> str:
        return f"{where}|{project}|{node}"

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def is_approved(self, key: str, code: str) -> bool:
        return code in self._read().get(key, {}).get("hashes", [])

    def last(self, key: str) -> dict[str, str] | None:
        return self._read().get(key, {}).get("files")

    def approve(self, key: str, files: dict[str, str]) -> None:
        data = self._read()
        entry = data.setdefault(key, {"hashes": []})
        code = files_hash(files)
        if code not in entry["hashes"]:
            entry["hashes"].append(code)
        entry["files"] = files
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.file)
        self._pending_file(key, code).unlink(missing_ok=True)

    def _pending_file(self, key: str, code: str) -> Path:
        return self.pending_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:12]}-{code[:12]}.json"

    def add_pending(self, key: str, files: dict[str, str]) -> Path:
        path = self._pending_file(key, files_hash(files))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": key, "files": files, "at": time.time()}), encoding="utf-8")
        return path

    def pending(self) -> list[tuple[Path, dict[str, Any]]]:
        out = []
        for p in sorted(self.pending_dir.glob("*.json")) if self.pending_dir.is_dir() else []:
            try:
                out.append((p, json.loads(p.read_text(encoding="utf-8"))))
            except ValueError:
                continue
        return out


def diff(old: dict[str, str] | None, new: dict[str, str]) -> str:
    """What changed from the last approved files: a unified diff per file."""
    old = old or {}
    out: list[str] = []
    for path in sorted(set(old) | set(new)):
        a, b = old.get(path), new.get(path)
        if a == b:
            continue
        out += difflib.unified_diff(
            (a or "").splitlines(keepends=True), (b or "").splitlines(keepends=True),
            fromfile=f"a/{path}" if a is not None else "/dev/null", tofile=f"b/{path}" if b is not None else "/dev/null",
        )
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
    return "".join(out)


def describe_key(key: str) -> str:
    where, project, node = key.split("|", 2)
    return f"node {node}" + (f" of project {project}" if project else "") + f" on {where}"


def ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def approve_pending(approvals: Approvals, out: Callable[[str], None] = print, confirm: Callable[[str], bool] = ask) -> int:
    """``exuvia runner approve``: show each piece of pending code with its
    diff and approve it on a yes (a no drops it). Returns how many were approved."""
    items = approvals.pending()
    if not items:
        out("nothing waits for approval")
        return 0
    done = 0
    for path, item in items:
        key, files = item["key"], item["files"]
        out(f"\n{describe_key(key)} wants to run this code here:\n")
        out(diff(approvals.last(key), files) or "(the same files as one approved before)")
        if confirm("Approve it, so runners on this machine run it? [y/N] "):
            approvals.approve(key, files)
            done += 1
        else:
            path.unlink(missing_ok=True)
    return done


# -- the runner ----------------------------------------------------------------------
def _safe(rel: str) -> bool:
    p = PurePosixPath(rel)
    return bool(rel) and not p.is_absolute() and ".." not in p.parts and "\\" not in rel and ":" not in rel


class DesktopRunner:
    """Takes runs from the hub at ``url`` for nodes with one of ``labels``
    and runs them here, one at a time."""

    def __init__(
        self,
        url: str,
        labels: list[str],
        *,
        name: str | None = None,
        folder: Path | None = None,
        allow_http: bool = False,
        token: Any = None,
        interactive: bool | None = None,
        log: Callable[[str], None] = print,
        confirm: Callable[[str], bool] = ask,
    ):
        import httpx

        from . import __version__
        from .executor import LocalExecutor
        from .remote import FROM_ENV, connect

        if not labels:
            raise RunnerStop("give at least one --label: the runs_on value of the nodes this runner runs")
        self.folder = folder or home()
        self.name = (name or socket.gethostname() or "runner").strip()[:100]
        self.labels = sorted(set(labels))
        self.version = __version__
        try:
            self.http = connect(
                hub_url(url), allow_http, origin="runner", token=FROM_ENV if token is None else token,
                timeout=httpx.Timeout(60.0, read=60.0),
            )
        except ValueError as e:
            raise RunnerStop(str(e)) from None
        self.http.headers["X-Exuvia-Runner"] = self.name
        self.where = self.http.where
        self.approvals = Approvals(self.folder)
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty() if interactive is None else interactive
        self.out = log
        self.confirm = confirm
        (self.folder / "work").mkdir(parents=True, exist_ok=True)
        self.executor = LocalExecutor(self.folder / "work", workers=1, packages_dir=self.folder / "packages")
        self._started = False

    def close(self) -> None:
        if self._started:
            self.executor.close()
        self.http.close()

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        from .remote import Unreachable

        try:
            r = self.http.request(method, path, **kw)
        except Unreachable as e:
            raise ConnectionError(str(e)) from e
        if r.status_code == 401:
            raise RunnerStop(
                f"{self.where} needs a token: set EXUVIA_API_TOKEN to your own agent token from the workbench's account menu"
            )
        if r.status_code == 404 and path.endswith("/claim"):
            raise RunnerStop(f"{self.where} has no runners endpoint: is it an exuvia hub, and new enough?")
        return r

    def claim(self, wait: float = 25.0) -> dict[str, Any] | None:
        r = self._call(
            "POST", "/api/runners/claim",
            json={"labels": self.labels, "name": self.name, "version": self.version, "wait": wait},
        )
        if r.status_code >= 400:
            raise RunnerStop(f"{self.where} refused the runner: {r.text[:500]}")
        return r.json().get("job")

    def run_once(self, wait: float = 25.0) -> dict[str, Any] | None:
        """Claim one run (waiting up to ``wait`` seconds for one) and run it.
        Returns the result sent back, or None when no run came."""
        job = self.claim(wait)
        if job is None:
            return None
        return self.handle(job)

    def run_forever(self) -> None:
        self.out(f"runner {self.name} on {self.where}, for nodes with runs_on {', '.join(self.labels)}; Ctrl-C stops it")
        backoff = 2.0
        while True:
            try:
                self.run_once()
                backoff = 2.0
            except ConnectionError as e:
                self.out(f"{e}; trying again in {backoff:g}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # -- one run --------------------------------------------------------------------
    def handle(self, job: dict[str, Any]) -> dict[str, Any]:
        jid, token, node = job["id"], job["token"], job["node"]
        files: dict[str, str] = job.get("files") or {}
        what = f"{node}" + (f" ({job['project']})" if job.get("project") else "")
        self.out(f"run {jid}: {what}")
        beat = _Heartbeat(self, jid, token)
        beat.start()
        try:
            result = self._run(job, files, beat)
        except Exception as e:  # noqa: BLE001 - anything that went wrong here is the run's failure, said to the hub
            result = {"ok": False, "error": f"the runner {self.name} failed: {type(e).__name__}: {e}"}
        beat.stop()
        if beat.gone.is_set():
            self.out(f"run {jid}: cancelled on the hub")
            return {"ok": False, "error": "cancelled"}
        output = result.pop("_output", None)
        if output is not None:
            with open(output, "rb") as f:
                r = self._call(
                    "PUT", f"/api/runners/jobs/{jid}/output",
                    params={"token": token, "kind": result["kind"], "artifact_id": result["artifact_id"]}, content=f,
                )
            if r.status_code >= 400:
                result = {"ok": False, "error": f"the hub did not keep the output: {r.text[:300]}"}
        r = self._call("POST", f"/api/runners/jobs/{jid}/result", json={"token": token, "result": result})
        if r.status_code >= 400 and r.status_code != 410:
            self.out(f"run {jid}: the hub refused the result: {r.text[:300]}")
        self.out(f"run {jid}: " + ("done" if result.get("ok") else f"failed: {result.get('error')}"))
        return result

    def _run(self, job: dict[str, Any], files: dict[str, str], beat: "_Heartbeat") -> dict[str, Any]:
        from .executor import ExecRequest

        bad = [p for p in files if not _safe(p)]
        if bad or job.get("path") not in files:
            return {"ok": False, "error": f"the run's files are not ones a project has: {bad[:3] or job.get('path')}"}
        key = self.approvals.key(self.where, job.get("project") or "", job["node"])
        if not self.approvals.is_approved(key, files_hash(files)):  # the hash of what came, worked out here
            if not self._approve(key, files):
                return {"ok": False, "error": self._not_approved(key)}
        if beat.gone.is_set():
            return {"ok": False, "error": "cancelled"}
        root = self.folder / "projects" / hashlib.sha256(f"{self.where}|{job.get('project')}".encode()).hexdigest()[:16]
        code = root / "code"
        shutil.rmtree(code, ignore_errors=True)
        for rel, text in files.items():
            p = code / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        (code / "nodes").mkdir(exist_ok=True)
        out_dir = root / "out"
        shutil.rmtree(out_dir, ignore_errors=True)
        if not self._started:
            self.executor.start()
            self._started = True
        req = ExecRequest(
            run_id=job["id"],
            node=job["node"],
            kind=job["kind"],
            path=job["path"],
            root=code,
            out_dir=out_dir,
            params=job.get("params") or {},
            env=job.get("env") or {},
            partition_by=job.get("partition_by"),
            partitions=job.get("partitions") or [],
            timeout=job.get("timeout"),
            cancelled=beat.gone.is_set,
        )
        beat.cancel = lambda: self.executor.cancel(job["id"])
        res = self.executor.execute(req, beat.write)
        result: dict[str, Any] = {
            "ok": res.ok, "error": res.error, "kind": res.kind, "artifact_id": res.artifact_id,
            "rows": res.rows, "schema": res.schema,
        }
        if res.ok and res.kind not in (None, "none") and res.artifact_path:
            result["_output"] = res.artifact_path
        return result

    def _approve(self, key: str, files: dict[str, str]) -> bool:
        if not self.interactive:
            self.approvals.add_pending(key, files)
            return False
        self.out(f"\n{describe_key(key)} wants to run this code here:\n")
        self.out(diff(self.approvals.last(key), files) or "(the same files as one approved before)")
        if not self.confirm("Run it on this machine, now and whenever it is this code again? [y/N] "):
            return False
        self.approvals.approve(key, files)
        return True

    def _not_approved(self, key: str) -> str:
        if self.interactive:
            return f"its code was not approved on the runner {self.name}"
        return (
            f"its code is new or changed since it was last approved on the runner {self.name}: "
            "its owner approves it there with `exuvia runner approve`, then runs it again"
        )


class _Heartbeat:
    """Sends a held run's log text to the hub every second, and a heartbeat
    at least every HEARTBEAT_S; sets ``gone`` (and calls ``cancel``) once
    the hub says the run is no longer this runner's."""

    def __init__(self, runner: DesktopRunner, jid: str, token: str):
        self.runner, self.jid, self.token = runner, jid, token
        self.gone = threading.Event()
        self.cancel: Callable[[], Any] = lambda: None
        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def write(self, text: str) -> None:
        with self._lock:
            self._buf.append(text)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
        self._send()

    def _loop(self) -> None:
        last = 0.0
        while not self._stop.wait(LOG_EVERY_S):
            with self._lock:
                pending = bool(self._buf)
            if pending or time.monotonic() - last >= HEARTBEAT_S:
                self._send()
                last = time.monotonic()

    def _send(self) -> None:
        if self.gone.is_set():
            return
        with self._lock:
            text, self._buf = "".join(self._buf), []
        try:
            r = self.runner._call("POST", f"/api/runners/jobs/{self.jid}/log", json={"token": self.token, "text": text})
        except (ConnectionError, RunnerStop):
            with self._lock:  # keep it for the next try
                self._buf.insert(0, text)
            return
        if r.status_code in (403, 410):
            self.gone.set()
            self.cancel()
