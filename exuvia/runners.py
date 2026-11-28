"""Runners: nodes that run on a person's own machine (the hub's half).

Some data can only be read from one person's desktop: a licensed data
terminal's API answers on that machine's ``localhost``, logged in as that
person, and other analysis processes leave their results on local disk.
Heavy runs can also be spread over people's workstations. A node marked
``@exuvia.node(runs_on="desk")`` (SQL: ``-- runs_on: desk``) is not run by the project's executor. Its runs
wait here until a runner with that label takes them:

    exuvia runner --url https://hub.example.com --label desk

on that desktop (desktop.py). The runner dials out over HTTPS only, so a
machine behind NAT or a corporate firewall works:

    POST /api/runners/claim               long-poll for a run (labels, name)
    POST /api/runners/jobs/{id}/log       log text, and a heartbeat; says when the run was cancelled
    PUT  /api/runners/jobs/{id}/output    the output file, re-verified here by its hash
    POST /api/runners/jobs/{id}/result    how the run ended
    GET  /api/runners                     runners online or not, and runs waiting for one

A runner is bound to a person: it signs in with its owner's agent token and
takes runs only from projects that person can edit. A claimed run is
answered only by the runner that claimed it (its claim token).

The run carries the node's file, the ``lib/`` files it imports, the
project's ``pyproject.toml``, ``uv.lock`` and ``exuvia.json``, and only the
project variables those files name: not the whole project, and not every
secret. The runner decides what it runs: only code its owner has approved
on that machine (desktop.py).

A run nobody takes waits (``waiting for a runner``) for
``EXUVIA_RUNNER_WAIT`` seconds (default 3600), then fails; nodes downstream
keep reading the last good output, as after any failed run. A runner that
stops sending heartbeats for ``EXUVIA_RUNNER_SILENCE`` seconds (default 120)
fails the run it held.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .executor import ExecRequest, ExecResult

LogFn = Callable[[str], None]
EXT = {"table": "parquet", "json": "json", "multi": "zip"}
# A runner seen this recently is online (it long-polls every ~25 s).
ONLINE_S = 60.0
# Files of the project a runner gets besides the node and its helpers.
PROJECT_FILES = ("pyproject.toml", "uv.lock", "exuvia.json")


def wait_limit() -> float:
    return _seconds("EXUVIA_RUNNER_WAIT", 3600.0)


def silence_limit() -> float:
    return _seconds("EXUVIA_RUNNER_SILENCE", 120.0)


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


class RunnerError(Exception):
    """A runner's request that can't be honoured: ``status`` is the HTTP code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def files_hash(files: dict[str, str]) -> str:
    """The hash of the code a run sends: what a runner's owner approves."""
    h = hashlib.sha256()
    for path in sorted(files):
        data = files[path].encode("utf-8")
        h.update(f"{path}\0{len(data)}\0".encode())
        h.update(data)
    return h.hexdigest()


def used_env(env: dict[str, str], files: dict[str, str]) -> dict[str, str]:
    """The variables the code names, as a word anywhere in its files: the
    only ones sent to the runner."""
    text = "\n".join(files.values())
    return {k: v for k, v in env.items() if re.search(rf"(?<![A-Za-z0-9_]){re.escape(k)}(?![A-Za-z0-9_])", text)}


def run_files(root: Path, node_path: str, helpers: list[str]) -> dict[str, str]:
    """The files a runner gets for one node, by path relative to the project."""
    out: dict[str, str] = {}
    for rel in [node_path, *helpers, *PROJECT_FILES]:
        p = root / rel
        if p.is_file():
            out[rel] = p.read_text(encoding="utf-8")
    return out


@dataclass
class Job:
    id: str  # the run's id
    project: str  # "" on `exuvia serve`
    label: str
    node: str
    spec: dict[str, Any]  # what the runner gets
    out_dir: Path
    on_log: LogFn
    created: float = field(default_factory=time.time)
    state: str = "waiting"  # waiting | claimed | done
    token: str = ""  # the claim: only that runner answers for the job
    runner: str = ""  # the key of the runner that claimed it
    claimed_at: float = 0.0
    seen: float = 0.0  # its runner's last heartbeat
    output: tuple[str, str, str] | None = None  # (kind, artifact_id, path)
    result: ExecResult | None = None
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class Runner:
    key: str
    name: str
    owner: str
    owner_uid: str | None
    labels: list[str]
    seen: float
    version: str = ""
    job: str | None = None

    def as_dict(self, now: float) -> dict[str, Any]:
        return {
            "name": self.name, "owner": self.owner, "labels": self.labels, "last_seen": self.seen,
            "online": now - self.seen < ONLINE_S, "busy": self.job, "version": self.version,
        }


class Broker:
    """The runs waiting for or held by runners, for every project a process
    serves (one per hub). Thread-safe: runs wait in executor threads, runners
    call in from the server's."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._runners: dict[str, Runner] = {}

    # -- the hub's side: a run handed to a runner ------------------------------------
    def execute(
        self, project: str, label: str, req: ExecRequest, files: dict[str, str], on_log: LogFn
    ) -> ExecResult:
        """Blocking: queue ``req`` for a runner labelled ``label`` and wait
        for it to end (or to be cancelled, or for nobody to take it)."""
        spec = {
            "id": req.run_id,
            "project": project,
            "node": req.node,
            "kind": req.kind,
            "path": req.path,
            "files": files,
            "code": files_hash(files),
            "params": req.params,
            "env": used_env(req.env, files),
            "partition_by": req.partition_by,
            "partitions": req.partitions,
            "timeout": req.timeout,
        }
        job = Job(req.run_id, project, label, req.node, spec, req.out_dir, on_log)
        with self._lock:
            self._jobs[job.id] = job
            online = any(label in r.labels and time.time() - r.seen < ONLINE_S for r in self._runners.values())
        on_log(f"exuvia: waiting for a runner labelled {label}" + ("" if online else " (none is online now)") + "\n")
        limit = wait_limit()
        try:
            while not job.done.wait(0.5):
                now = time.time()
                with self._lock:
                    if req.is_cancelled():
                        return ExecResult(ok=False, error="cancelled")
                    if job.state == "waiting" and now - job.created > limit:
                        error = (
                            f"no runner labelled {label} took the run within {limit:g}s (EXUVIA_RUNNER_WAIT): "
                            f"start `exuvia runner --label {label}` on the machine that has the data"
                        )
                        on_log(f"exuvia: {error}\n")
                        return ExecResult(ok=False, error=error)
                    if job.state == "claimed" and now - job.seen > silence_limit():
                        error = f"the runner {job.runner.split('|', 1)[-1]} stopped answering"
                        on_log(f"\nexuvia: {error}\n")
                        return ExecResult(ok=False, error=error)
            assert job.result is not None
            return job.result
        finally:
            with self._lock:
                self._jobs.pop(job.id, None)
                job.state = "done"
                for r in self._runners.values():
                    if r.job == job.id:
                        r.job = None

    def cancel(self, run_id: str) -> bool:
        """The run's wait ends (``ExecRequest.cancelled`` says so); its runner
        hears of it at its next heartbeat. False when no runner holds or
        waits for it."""
        with self._lock:
            return run_id in self._jobs

    # -- the runner's side --------------------------------------------------------------
    def heard(self, owner: str, owner_uid: str | None, name: str, labels: list[str], version: str = "") -> Runner:
        key = f"{owner_uid or owner}|{name}"
        with self._lock:
            r = self._runners.get(key)
            if r is None:
                r = self._runners[key] = Runner(key, name, owner, owner_uid, labels, time.time(), version)
            r.labels, r.seen, r.version = labels, time.time(), version or r.version
            return r

    def try_claim(self, runner: Runner, can_take: Callable[[str], bool]) -> dict[str, Any] | None:
        """The oldest waiting run for one of the runner's labels in a
        project its owner can edit, now claimed by it; None when there is
        none. ``can_take`` runs outside the lock (it may read a database)."""
        with self._lock:
            waiting = sorted(
                (j for j in self._jobs.values() if j.state == "waiting" and j.label in runner.labels),
                key=lambda j: j.created,
            )
        allowed: dict[str, bool] = {}
        for job in waiting:
            if job.project not in allowed:
                allowed[job.project] = can_take(job.project)
            if not allowed[job.project]:
                continue
            with self._lock:
                if job.state != "waiting" or self._jobs.get(job.id) is not job:
                    continue
                job.state, job.token, job.runner = "claimed", secrets.token_urlsafe(24), runner.key
                job.claimed_at = job.seen = time.time()
                runner.job = job.id
            job.on_log(f"exuvia: taken by the runner {runner.name} ({runner.owner})\n")
            return {**job.spec, "token": job.token}
        return None

    def _held(self, job_id: str, token: str, runner_key: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None or job.state != "claimed":
            raise RunnerError(410, f"run {job_id} is not waiting for this runner any more (it was cancelled, or ended)")
        if not secrets.compare_digest(job.token, token or "") or job.runner != runner_key:
            raise RunnerError(403, f"run {job_id} was claimed by another runner")
        return job

    def log(self, job_id: str, token: str, runner_key: str, text: str) -> None:
        """A heartbeat, with what the run printed since the last one."""
        with self._lock:
            job = self._held(job_id, token, runner_key)
            job.seen = time.time()
            r = self._runners.get(runner_key)
            if r is not None:
                r.seen = job.seen
        if text:
            job.on_log(text)

    def output(self, job_id: str, token: str, runner_key: str, kind: str, artifact_id: str, src: Path) -> None:
        """Keep the output file the runner sent, at ``src``, once its bytes
        hash to ``artifact_id`` (moved into the run's output folder)."""
        if kind not in EXT:
            raise RunnerError(400, f"kind must be one of {', '.join(EXT)}, not {kind!r}")
        if not re.fullmatch(r"[0-9a-f]{16}", artifact_id or ""):
            raise RunnerError(400, "artifact_id is the first 16 hex digits of the output's sha256")
        with self._lock:
            job = self._held(job_id, token, runner_key)
        h = hashlib.sha256()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest()[:16] != artifact_id:
            raise RunnerError(422, "the output's bytes don't match its artifact_id: it was not kept")
        job.out_dir.mkdir(parents=True, exist_ok=True)
        final = job.out_dir / f"{artifact_id}.{EXT[kind]}"
        os.replace(src, final)
        with self._lock:
            job.output = (kind, artifact_id, str(final))

    def finish(self, job_id: str, token: str, runner_key: str, result: dict[str, Any]) -> None:
        """How the run ended, from its runner."""
        with self._lock:
            job = self._held(job_id, token, runner_key)
            res = ExecResult(
                ok=bool(result.get("ok")),
                error=str(result["error"])[:4000] if result.get("error") else None,
                artifact_id=result.get("artifact_id"),
                kind=result.get("kind"),
                rows=result.get("rows") if isinstance(result.get("rows"), int) else None,
                schema=result.get("schema") if isinstance(result.get("schema"), list) else None,
            )
            if res.ok and res.kind not in (None, "none"):
                if job.output is None or job.output[:2] != (res.kind, res.artifact_id):
                    raise RunnerError(400, "send the output (PUT .../output) before saying the run succeeded")
                res.artifact_path = job.output[2]
            elif res.ok:
                res.artifact_id = res.artifact_path = None
            elif not res.error:
                res.error = "the runner reported a failure without an error"
            job.result = res
            job.done.set()

    # -- what the workbench shows ----------------------------------------------------------
    def describe(self, visible: Callable[[Runner], bool], can_see: Callable[[str], bool]) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            runners = [r.as_dict(now) for r in self._runners.values() if visible(r)]
            jobs = list(self._jobs.values())
        waiting = [
            {"run": j.id, "project": j.project, "node": j.node, "label": j.label, "state": j.state, "since": j.created}
            for j in jobs
            if can_see(j.project)
        ]
        return {"runners": sorted(runners, key=lambda r: (not r["online"], r["name"])), "runs": waiting}

    def waiting_for(self, project: str) -> list[dict[str, Any]]:
        """The project's runs waiting for, or held by, a runner."""
        with self._lock:
            return [
                {"run": j.id, "node": j.node, "label": j.label, "state": j.state, "since": j.created}
                for j in self._jobs.values()
                if j.project == project
            ]
