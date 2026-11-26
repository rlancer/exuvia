"""The live workspace: graph + run store + executor + scheduler + file watcher.

Execution model
---------------
* Every node's output is a content-addressed artifact. A node is **fresh** when
  its code is unchanged since its last successful run *and* every upstream's
  current artifact is the one that run consumed. Otherwise it is **stale**.
* ``run(targets)`` runs each target plus any stale ancestors, in dependency
  order, in parallel where the graph allows. Each node runs in its own
  subprocess (see ``runner.py``).
* When a run produces a *different* artifact, downstream nodes with
  ``cascade=True`` are refreshed automatically. Unchanged output stops the
  cascade, so a scheduled source that returns the same data costs nothing
  downstream.
* Each node may carry its own cron ``schedule``; the scheduler forces that
  node to re-run on its cadence and lets the cascade carry the result. A
  scheduled run is pinned to its cron slot: a ``date`` parameter whose default
  is ``today`` gets the slot's date, so a late or catch-up run still computes
  the day it was due for. A fire that comes while the node is still running
  waits for that run to end (``overlap="queue"``) or is dropped (``"skip"``).
* A node with ``retries`` tries a failed run again, after ``retry_delay``
  seconds, doubling each time. The attempts share one run row and one log.
* A **push** node (``push=True``) is never run here: its output is uploaded
  from another machine (``push()``, behind ``exuvia push``), recorded as a run
  triggered ``push``, and cascades like any other. Runs leave it out, and
  its output stays current until the next push, whatever its code says.

Run parameters
--------------
A project may declare parameters in ``exuvia.json`` (``as_of`` is the classic
one). Every run resolves them to concrete values, and a node's "current
output" is tracked **per value of the parameters it depends on** (its own and
its ancestors'). So replaying ``as_of=2026-03-02`` neither overwrites nor is
overwritten by today's run, replaying it again is a cache hit, and nodes that
depend on no parameter are computed once and shared by every replay.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager, suppress
from contextvars import Context, ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Iterator

from croniter import croniter

from . import auth, envvars
from .arrowstream import ArrowStream
from .artifacts import artifact_key, artifact_store
from .events import EventBus
from .executor import ExecRequest, LocalExecutor, make_executor
from .notify import failure_webhook, payload, send
from .npm import Npm, bare_imports
from .packages import Packages, missing_package
from .keys import KeyViolation, enforce_file
from .partitioned import Manifest, PartitionError, Writer, commit, in_range, prune, sort_values
from .pushing import save_pushed
from .project import NODE_SUFFIXES, VIEW_DIR, VIEW_MANIFEST, Graph, Project, View, _hash, _validate_cron, check_stage, same_code
from . import outputs
from .outputs import jsonable, lock_outputs, output_parts, outputs_connection  # noqa: F401 - lock_outputs, outputs_connection: re-exported
from .runner import PART_EXT, PART_NAME, finite
from .store import CodeChange, clean_origin, open_store

MAX_PARALLEL = int(os.environ.get("EXUVIA_MAX_PARALLEL", "4"))
# "internal": a loop in this process evaluates schedules every few seconds.
# "external": something else POSTs /api/schedule/tick (Durable Object alarm,
# k8s CronJob, system cron) — for hosts that sleep or scale to zero.
SCHEDULER = os.environ.get("EXUVIA_SCHEDULER", "internal")
_ALL_ROWS = 2**62  # the limit of an offset-only read
# How soon an external scheduler should tick again while a fire waits for the
# node's run in progress to end (overlap="queue"; see next_fire_at).
QUEUED_RECHECK_S = 30
# What _execute returns for a failed attempt that will be tried again.
RETRY = "retry"
# Run statuses that leave a node's output good to read: "cached" is a run
# that found its output already current, as good as one that just made it.
OK = ("success", "cached")
# Project files besides nodes/ whose versions are kept as code revisions.
VERSIONED_FILES = ("exuvia.json", "AGENTS.md", "pyproject.toml", "package.json")
# A code tag's name: what git takes as a tag (`exuvia mirror` makes one of
# each), kept short and plain: v1, v1.2.0, q3-review, before_refactor.
TAG_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9_])?")
TAG_MESSAGE_CHARS = 2000

# Who is changing the code right now (the agent, an API caller, ...). Code
# revisions record it; a change nobody claimed came from editing the files.
_ORIGIN: ContextVar[str | None] = ContextVar("exuvia_origin", default=None)


@contextmanager
def acting_as(origin: str) -> Iterator[None]:
    """Attribute code changes made inside this block to ``origin``."""
    token = _ORIGIN.set(clean_origin(origin, "api"))
    try:
        yield
    finally:
        _ORIGIN.reset(token)


def params_key(params: dict[str, Any]) -> str:
    """Stable id for a parameter set; empty when there are no parameters."""
    if not params:
        return ""
    canon = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()[:12]


class Workspace:
    def __init__(
        self, root: str | Path, origin: str = "load", packages_store: Path | None = None, npm_store: Path | None = None
    ):
        """``origin`` labels code found changed since the last recorded
        revision when the project loads (a template applied, an import, edits
        made while nothing was watching). ``packages_store`` is where a
        server shared by many projects (a hub) keeps their package installs
        (see packages.py), and ``npm_store`` the modules its views' npm
        packages are made of (npm.py); without them the project keeps its own."""
        self.project = Project(root)
        # Held while project files are written and reloaded (see editing()).
        self._edit_lock = threading.RLock()
        self.store = open_store(self.project)
        self.store.fail_interrupted()
        # Encrypts the project's secrets (envvars.py). A malformed
        # EXUVIA_SECRETS_KEY stops the project from loading.
        self.keyring = envvars.Keyring(self.project.state_dir)
        self.env = envvars.Env(self.store, self.keyring)
        # Variables someone asked a person to set (request_secret), by name.
        self._env_requests: dict[str, dict[str, Any]] = {}
        # On a hub: the variables the project's group gives it (groups.py),
        # as ``{"group": {id, name}, "rows": [decrypted rows]}``, or None
        # outside a group. Set by Hub.get; read for every run.
        self.inherited_env: Callable[[], dict[str, Any] | None] | None = None
        self._seal_env_vars()
        self.bus = EventBus()
        self.graph: Graph = self.project.load()
        self.record_code(origin)
        self._node_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._slots = asyncio.Semaphore(MAX_PARALLEL)
        self._inflight: dict[tuple[str, str], str] = {}  # (node, params_key) -> run_id
        # Every run of it that has not ended, oldest first: a second one can be
        # queued behind the first, and when either ends the other is still in flight.
        self._unfinished: dict[tuple[str, str], list[str]] = {}
        # Live refreshes started but whose runs may not have registered yet
        # (see refresh_view): (node, params_key).
        self._live_pending: set[tuple[str, str]] = set()
        self._tasks: list[asyncio.Task[Any]] = []  # cancelled by stop()
        self._cancelled: set[str] = set()  # runs asked to stop: never retried
        # Runs waiting to retry: run_id -> (loop, event that cuts the wait short).
        self._retry_waits: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
        # Every run not finished yet: run_id -> (loop, its task). One waiting
        # for its upstream nodes, the node's lock or a slot is cancelled by
        # cancelling the task; one in _executing by its executor.
        self._run_tasks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Task[str]]] = {}
        self._executing: set[str] = set()
        self._stop_watching: asyncio.Event | None = None
        self._backfill: dict[str, Any] | None = None
        self.executor = make_executor(self.project.root, MAX_PARALLEL)
        # The packages the project lists in its pyproject.toml (packages.py).
        local = isinstance(self.executor, LocalExecutor)
        self.packages = Packages(self.project.root, publish=self.bus.publish, local=local, store=packages_store)
        if local:
            self.executor.packages = self.packages
        self.packages.on_edit = self.reload  # records pyproject.toml as a revision
        # The npm packages the project's views import, from its package.json (npm.py).
        self.npm = Npm(self.project.root, store=npm_store)
        self.npm.on_edit = self.reload  # records package.json; view hashes change
        # Where this project's API is mounted: "/api" alone, "/p/<id>/api" in a
        # hub (set by Hub.get). Used for the run link in failure webhooks.
        self.api_prefix = "/api"

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self.bus.bind(asyncio.get_running_loop())
        self.executor.start()
        self.packages.prebuild()  # install a changed list now, not at the next run
        # Fresh contexts: a change the watcher sees was made by editing files,
        # and a scheduled run was started by the clock, not by whichever
        # request (and signed-in person) happened to load the project.
        self._stop_watching = asyncio.Event()
        self.spawn(self._watch_files(self._stop_watching), context=Context())
        if artifact_store() is not None:
            self.spawn(self._sync_artifacts(), context=Context())
        if SCHEDULER == "internal":
            self.spawn(self._schedule_loop(), context=Context())

    def spawn(self, coro: Coroutine[Any, Any, Any], *, context: Context | None = None) -> asyncio.Task[Any]:
        """Start a background task and keep a reference to it until it ends:
        the event loop only holds tasks weakly, and stop() cancels them."""
        task = asyncio.create_task(coro, context=context)
        self._tasks.append(task)
        task.add_done_callback(self._forget)
        return task

    def _forget(self, task: asyncio.Task[Any]) -> None:
        with suppress(ValueError):
            self._tasks.remove(task)

    async def stop(self) -> None:
        """Stop the background tasks and wait for them to finish. Once this
        returns the file watcher has no project file open, so the caller may
        replace or delete the project's files (on Windows a file another
        thread is reading cannot be deleted; see Hub.apply_template)."""
        if self._stop_watching is not None:
            self._stop_watching.set()  # the watch thread checks it every 50 ms
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = []
        self.executor.close()

    @contextmanager
    def editing(self) -> Iterator[None]:
        """Hold while writing project files and reloading. The file watcher
        reloads under the same lock, so it cannot record a change made through
        the API or the agent as a hand edit ("file") in the moment between the
        write and the caller's own reload; by the time it gets the lock, that
        reload has recorded the change with the caller's origin."""
        with self._edit_lock:
            yield

    def reload(self) -> None:
        with self._edit_lock:
            self.graph = self.project.load()
            origin = _ORIGIN.get()
            # A change nobody claimed was a hand edit, whoever is signed in.
            if origin:
                self.record_code(origin, author=auth.current_id(), author_user=auth.current_uid())
            else:
                self.record_code("file")
        self.bus.publish({"type": "graph"})

    # -- code revisions ------------------------------------------------------
    def _code_files(self) -> dict[str, tuple[str | None, str | None, Path]]:
        """Every versioned file: relative path -> (node, view, path), where
        node / view name what the file belongs to (None for neither)."""
        root = self.project.root
        files: dict[str, tuple[str | None, str | None, Path]] = {}
        for p in sorted(self.project.nodes_dir.iterdir()):
            if p.is_file() and p.suffix in NODE_SUFFIXES:
                node = None if p.name.startswith("_") else p.stem
                files[p.relative_to(root).as_posix()] = (node, None, p)
        for rel in self.project.helper_files():  # shared modules under lib/
            files[rel] = (None, None, root / rel)
        for view, rel in self.project.all_view_files():  # views/<name>/...
            files[rel] = (None, view, root / rel)
        for name in VERSIONED_FILES:
            p = root / name
            if p.is_file():
                files[name] = (None, None, p)
        return files

    def record_code(self, origin: str, author: str | None = None, author_user: str | None = None) -> int:
        """Store a revision for every versioned file whose content differs
        from its latest revision, and for every one that disappeared, saved
        by ``author`` (a signed-in person's name; ``author_user`` their user
        id) through ``origin``. Returns how
        many were recorded."""
        latest = self.store.latest_code()
        changes: list[CodeChange] = []
        files = self._code_files()
        for rel, (node, view, path) in files.items():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # mid-write or not text; the next reload sees it
            digest = _hash(text)
            if latest.get(rel) != digest:
                changes.append((rel, node, view, digest, text))
        for rel, digest in latest.items():
            if digest is not None and rel not in files:
                changes.append((rel, _node_of(rel), _view_of(rel), None, None))
        n = self.store.record_code(changes, origin, author=author, author_user=author_user)
        if n:
            self.bus.touch()
        return n

    def code_revisions(
        self, node: str | None = None, limit: int = 200, *, view: str | None = None
    ) -> list[dict[str, Any]]:
        """Saved versions newest first (one node's, one view's files, or every file's)."""
        current = self._current_hashes()
        return [
            {**r, "current": r["hash"] is not None and current.get(r["path"]) == r["hash"]}
            for r in self.store.code_revisions(node=node, view=view, limit=limit)
        ]

    def code_source(self, digest: str) -> str | None:
        return self.store.code_source(digest)

    def run_source(self, run_id: str) -> dict[str, Any] | None:
        """The code a run executed, when its version was recorded."""
        run = self.store.run(run_id)
        if run is None:
            return None
        digest = run.get("code_hash")
        return {
            "run_id": run_id,
            "node": run["node"],
            "code_hash": digest,
            "source": self.store.code_source(digest) if digest else None,
            "code_current": bool(
                digest and (n := self.graph.nodes.get(run["node"])) is not None and n.code_hash == digest
            ),
        }

    def _current_hashes(self) -> dict[str, str]:
        """Each code file's content hash as it is on disk now, by path."""
        out = {}
        for rel, (_, _, p) in self._code_files().items():
            try:
                out[rel] = _hash(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
        return out

    # -- code tags ---------------------------------------------------------
    def create_tag(self, name: str, message: str | None = None, revision: str | None = None) -> dict[str, Any]:
        """Name the whole project's code as it is now, or as of ``revision``
        (the save it belongs to, whole). ValueError for a bad or taken name,
        KeyError when there is no such revision (or no code at all yet)."""
        name = (name or "").strip()
        if not TAG_NAME.fullmatch(name) or ".." in name or name.endswith(".lock"):
            raise ValueError(
                f"not a tag name: {name!r}. Use letters, digits, '.', '_' and '-' (up to 64, starting and ending "
                "with a letter or digit), e.g. v1, v1.2.0 or q3-review"
            )
        message = (message or "").strip()
        if len(message) > TAG_MESSAGE_CHARS:
            raise ValueError(f"the message is {len(message)} characters: keep it under {TAG_MESSAGE_CHARS}")
        with self._edit_lock:
            if not revision:
                # Edits on disk nobody recorded yet are part of "now".
                self.record_code("file")
            end = self.store.save_end(revision or "")
            if end is None:
                raise KeyError(f"no revision {revision}" if revision else "there is no saved code to tag yet")
            tag = self.store.create_tag(name, end, message, author=auth.current_id(), author_user=auth.current_uid())
        self.bus.publish({"type": "tags"})
        return tag

    def code_tags(self) -> list[dict[str, Any]]:
        """Every tag, newest first, each with how many files it holds and how
        many of them differ from the code now (changed, deleted or added since)."""
        current = self._current_hashes()
        out = []
        for t in self.store.code_tags():
            files = self.store.code_at(t["revision"])
            changed = sum(1 for p, r in files.items() if current.get(p) != r["hash"])
            changed += sum(1 for p in current if p not in files)
            out.append({**t, "files": len(files), "changed_since": changed})
        return out

    def code_tag(self, name: str) -> dict[str, Any]:
        """One tag and every file it holds, each beside the file now:
        ``status`` is ``same``, ``changed``, ``deleted`` (gone since) or
        ``added`` (new since; ``hash`` is null). KeyError when there is no such tag."""
        tag = self.store.code_tag(name)
        if tag is None:
            raise KeyError(f"no tag {name}")
        files = self.store.code_at(tag["revision"])
        current = self._current_hashes()
        rows = []
        for path in sorted(set(files) | set(current)):
            r = files.get(path)
            now = current.get(path)
            status = "added" if r is None else "deleted" if now is None else "same" if now == r["hash"] else "changed"
            rows.append({
                "path": path,
                "node": r["node"] if r else _node_of(path),
                "view": r["view"] if r else _view_of(path),
                "hash": r["hash"] if r else None,
                "revision": r["id"] if r else None,
                "saved_at": r["saved_at"] if r else None,
                "current_hash": now,
                "status": status,
            })
        return {**tag, "files": rows}

    def tag_source(self, name: str, path: str) -> str:
        """The text of ``path`` as tag ``name`` holds it. KeyError when there
        is no such tag, or the tag holds no such file."""
        tag = self.store.code_tag(name)
        if tag is None:
            raise KeyError(f"no tag {name}")
        r = self.store.code_at(tag["revision"]).get(path)
        source = self.store.code_source(r["hash"]) if r else None
        if source is None:
            raise KeyError(f"tag {name} holds no file {path}")
        return source

    def delete_tag(self, name: str) -> None:
        """Remove a tag (the code it named stays in the history). KeyError when there is none."""
        if not self.store.delete_tag(name):
            raise KeyError(f"no tag {name}")
        self.bus.publish({"type": "tags"})

    def restore_node(self, name: str, digest: str) -> None:
        """Write a recorded version of node ``name`` back as its current
        code. A restore is itself a new revision; history only grows."""
        rev = self.store.code_revision_of(digest, node=name)
        source = self.store.code_source(digest)
        if rev is None or source is None:
            raise KeyError(f"no saved version {digest} of {name}")
        kind = NODE_SUFFIXES.get(Path(rev["path"]).suffix, "python")
        with acting_as("restore"):
            with self.editing():
                self.project.write_node(name, source, kind)
                self.reload()

    def restore_view_file(self, name: str, rel: str, digest: str) -> dict[str, Any]:
        """Write a recorded version of file ``rel`` of view ``name`` back (a
        deleted file or view comes back). Raises KeyError when no such version
        was recorded, ValueError for a bad name or path."""
        self.project.view_file_path(name, rel)
        path = f"{VIEW_DIR}/{name}/{rel}"
        source = self.store.code_source(digest)
        if source is None or self.store.code_revision_of(digest, path=path) is None:
            raise KeyError(f"no saved version {digest} of {path}")
        with acting_as("restore"):
            with self.editing():
                self.project.write_view_file(name, rel, source)
                self.reload()
        return self.view_info(name)

    async def _watch_files(self, stop: asyncio.Event) -> None:
        from watchfiles import awatch

        # Helpers count too: an edit in lib/ changes the code hash of every
        # node that imports it.
        # Views too: their files are versioned like node files.
        dirs = (self.project.nodes_dir, self.project.lib_dir, self.project.views_dir)
        async for _ in awatch(*dirs, debounce=200, stop_event=stop):
            # In a thread: it may wait for an API or agent edit to finish. A
            # thread cannot be cancelled, so when the workspace stops mid-reload
            # this waits for the reload to finish instead of leaving it reading
            # files that the caller of stop() is about to delete.
            reload = asyncio.ensure_future(asyncio.to_thread(self.reload))
            try:
                await asyncio.shield(reload)
            except asyncio.CancelledError:
                await reload
                raise

    @property
    def busy(self) -> bool:
        """True while any node is queued/running or a backfill is in progress."""
        return bool(self._inflight) or bool(self._backfill and self._backfill["state"] == "running")

    # -- parameters ----------------------------------------------------------
    def resolve_params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Concrete values for every declared parameter (raises ValueError)."""
        return self.project.resolve_params(params)

    def node_params(self, name: str, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The slice of a resolved parameter set ``name`` depends on, and its key."""
        own = {k: params.get(k) for k in self.graph.effective_params(name)}
        return own, params_key(own)

    def param_values(self) -> dict[str, list[Any]]:
        """Every value each parameter has been run with (for history pickers)."""
        seen: dict[str, set[Any]] = {name: set() for name in self.project.params}
        for used in self.store.param_sets():
            for k, v in used.items():
                if k in seen and v is not None:
                    seen[k].add(v)
        return {k: sorted(v, key=str, reverse=True) for k, v in seen.items()}

    def set_param(self, name: str, spec: dict[str, Any] | None) -> None:
        """Declare, change, or (``spec=None``) remove a project parameter."""

        def mutate(cfg: dict[str, Any]) -> None:
            params = cfg.get("params") if isinstance(cfg.get("params"), dict) else {}
            if spec is None:
                params.pop(name, None)
            else:
                params[name] = spec
            if params:
                cfg["params"] = params
            else:
                cfg.pop("params", None)

        with self.editing():
            self.project.update_config(mutate)
            self.reload()

    # -- stages: steps of the graph on the canvas (exuvia.json `stages`) --------
    def set_stage(
        self, name: str, nodes: list[str] | None, title: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        """Create or change a stage, or (``nodes=None``) remove it. A node
        moves into it from any other stage it was in. Stages only arrange the
        canvas: no node file changes, so no output goes stale."""
        spec: dict[str, Any] = {"nodes": nodes or []}
        if title is not None:
            spec["title"] = title
        if description is not None:
            spec["description"] = description
        clean, problem = check_stage(name, spec)
        if problem:
            raise ValueError(problem)
        unknown = [n for n in clean["nodes"] if n not in self.graph.nodes]
        if nodes is not None and unknown:
            raise ValueError(f"no node named {', '.join(unknown)}")

        def mutate(cfg: dict[str, Any]) -> None:
            stages = cfg.get("stages") if isinstance(cfg.get("stages"), dict) else {}
            if nodes is None:
                stages.pop(name, None)
            else:
                moved = set(clean["nodes"])
                for other, ospec in stages.items():
                    if other == name:
                        continue
                    if isinstance(ospec, list):
                        stages[other] = [n for n in ospec if n not in moved]
                    elif isinstance(ospec, dict) and isinstance(ospec.get("nodes"), list):
                        ospec["nodes"] = [n for n in ospec["nodes"] if n not in moved]
                old = stages.get(name) if isinstance(stages.get(name), dict) else {}
                entry: dict[str, Any] = {}
                for key in ("title", "description"):
                    value = clean[key] if key in spec else old.get(key)
                    if value:
                        entry[key] = value
                entry["nodes"] = clean["nodes"]
                stages[name] = entry
            if stages:
                cfg["stages"] = stages
            else:
                cfg.pop("stages", None)

        with self.editing():
            self.project.update_config(mutate)
            self.reload()
        return {"stages": [g.to_dict() for g in self.graph.stages.values()]}

    # -- environment variables (envvars.py) ----------------------------------------
    def _env_rows(self) -> list[dict[str, Any]]:
        """Every variable with its value decrypted, for runs and nothing else."""
        return self.env.rows()

    def _seal_env_vars(self) -> None:
        for name in self.env.seal_all():
            print(f"exuvia: the secret {name} cannot be decrypted with this server's key", file=sys.stderr)

    def _inherited(self) -> dict[str, Any] | None:
        return self.inherited_env() if self.inherited_env is not None else None

    def _run_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(what a run sets, what its logs mask). The project's own variable
        wins over its group's of the same name, the group's over the hub's
        (and all of them over the server's environment, which the executor
        starts from). Every secret is masked, one a closer layer overrides
        too."""
        own = self._env_rows()
        inherited = self._inherited()
        above = inherited["rows"] if inherited else []  # the group's, then the hub's
        seen = {r["name"] for r in own}
        run = list(own)
        for r in above:
            if r["name"] not in seen:
                seen.add(r["name"])
                run.append(r)
        return run, own + above

    def env_vars(self) -> list[dict[str, Any]]:
        """The project's variables as a client may see them: no secret's value."""
        return envvars.public(self._env_rows())

    def inherited_vars(self) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """(the project's group, the variables its group and then the hub
        give the project, as a client may see them). Each has ``source``
        (``group`` or ``hub``) and is marked ``overridden`` when a closer
        layer sets the same name: the project for a group's, the project or
        its group for the hub's."""
        inherited = self._inherited()
        if not inherited:
            return None, []
        group = inherited["group"]
        closer = {r["name"] for r in self.store.env_vars()}
        group_names = {r["name"] for r in inherited["rows"] if r["source"] == "group"}
        out = []
        for r, v in zip(inherited["rows"], envvars.public(inherited["rows"]), strict=True):
            if r["source"] == "group":
                out.append({**v, "source": "group", "group": group, "overridden": v["name"] in closer})
            else:
                out.append({**v, "source": "hub", "overridden": v["name"] in closer or v["name"] in group_names})
        return group, out

    def effective_env(self) -> list[dict[str, Any]]:
        """Every variable a run gets, by name, with where it comes from
        (``source``: project, group or hub), as a client may see them. One
        that replaces a group's says so (``overrides``: the group), one
        that replaces a hub variable too (``overrides_hub``)."""
        group, inherited = self.inherited_vars()
        by_source: dict[str, set[str]] = {"group": set(), "hub": set()}
        for v in inherited:
            by_source[v["source"]].add(v["name"])

        def shadows(v: dict[str, Any], *sources: str) -> dict[str, Any]:
            out: dict[str, Any] = {}
            if "group" in sources and v["name"] in by_source["group"]:
                out["overrides"] = group
            if v["name"] in by_source["hub"]:
                out["overrides_hub"] = True
            return out

        own = [{**v, "source": "project", **shadows(v, "group", "hub")} for v in self.env_vars()]
        above = [
            {**{k: x for k, x in v.items() if k != "overridden"}, **(shadows(v, "hub") if v["source"] == "group" else {})}
            for v in inherited if not v["overridden"]
        ]
        return sorted(own + above, key=lambda v: v["name"])

    def env_state(self) -> dict[str, Any]:
        """``GET /api/env``: the project's own variables, the ones its group
        and the hub give it (``inherited``, each with its ``source``), and
        the requests for ones that are not set yet (a request is answered by
        setting its variable, from the workbench, the API or ``exuvia env
        set``, or by the group's or the hub's)."""
        variables = self.env_vars()
        group, inherited = self.inherited_vars()
        ready = {v["name"] for v in (*variables, *inherited) if v["readable"]}
        for name in [n for n in self._env_requests if n in ready]:
            self._env_requests.pop(name, None)
        return {
            "variables": variables, "requests": list(self._env_requests.values()),
            "group": group, "inherited": inherited,
        }

    def set_env_var(self, name: str, value: str | None, secret: bool) -> list[dict[str, Any]]:
        """Add or change a variable (``envvars.Env.set``)."""
        self.env.set(name, value, secret, auth.current_id())
        self._env_requests.pop(name, None)
        self.bus.publish({"type": "env"})
        return self.env_vars()

    def delete_env_var(self, name: str) -> list[dict[str, Any]]:
        if self.env.delete(name):
            self.bus.publish({"type": "env"})
        return self.env_vars()

    def request_env_var(self, name: str, reason: str, origin: str) -> dict[str, Any]:
        """Someone (the agent, an MCP client) asks a person to set a secret.
        Only the name and why travel: the value is entered by the person, in
        the workbench or with ``exuvia env set``. Waits in memory until the
        variable is set or the request dismissed."""
        envvars.check_name(name)
        reason = " ".join((reason or "").split())[:300]
        have = next((v for v in self.effective_env() if v["name"] == name), None)
        if have is not None and have["readable"]:
            out = {"status": "set", "name": name, "secret": have["secret"]}
            if have["source"] == "group":
                out["group"] = have["group"]["name"]
            elif have["source"] == "hub":
                out["from"] = "hub"
            return out
        if name not in self._env_requests and len(self._env_requests) >= envvars.MAX_VARIABLES:
            raise envvars.EnvError("Too many variables are already waiting to be set.")
        self._env_requests[name] = {"name": name, "reason": reason, "origin": origin, "at": time.time()}
        self.bus.publish({"type": "env"})
        return {"status": "requested", "name": name, "reason": reason}

    def dismiss_env_request(self, name: str) -> None:
        if self._env_requests.pop(name, None) is not None:
            self.bus.publish({"type": "env"})

    def set_settings(self, *, name: str | None = None, timezone: str | None = None) -> None:
        if timezone:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise ValueError(f"unknown timezone {timezone!r}") from e

        def mutate(cfg: dict[str, Any]) -> None:
            if name is not None:
                cfg["name"] = name
            if timezone is not None:
                if timezone:
                    cfg["timezone"] = timezone
                else:
                    cfg.pop("timezone", None)

        with self.editing():
            self.project.update_config(mutate)
            self.reload()

    # -- status ------------------------------------------------------------
    def status(self, params: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        """Per-node status for one parameter set (the defaults when omitted),
        computed in topological order so staleness propagates."""
        g = self.graph
        params = self.resolve_params(params)
        out: dict[str, dict[str, Any]] = {}
        try:
            order = g.topo_order()
        except ValueError:
            order = sorted(g.nodes)
        current: dict[str, str | None] = {}
        paused = self.store.paused()
        sched_state = self.store.schedule_states()
        for name in order:
            node = g.nodes[name]
            own, key = self.node_params(name, params)
            ls = self.store.last_success(name, key)
            lr = self.store.last_run(name, key)
            la = self.store.last_attempt(name, key)
            current[name] = ls["artifact_id"] if ls else None
            reason = None
            if node.parse_error:
                state, reason = "error", node.parse_error
            elif ls is None:
                state, reason = "new", ("waiting for a push" if node.push else "never run")
            elif not node.push and not same_code(ls, node):
                state, reason = "stale", "code changed"
            elif ls.get("kind") == "pickle":
                # Never read (see runner.load_artifact): re-run before use.
                state, reason = "stale", "output saved with pickle, a retired format"
            else:
                state = "fresh"
                for dep in g.upstream(name):
                    dep_state = out.get(dep, {}).get("state")
                    if current.get(dep) != (ls.get("inputs") or {}).get(dep):
                        state, reason = "stale", f"{dep} has new output"
                        break
                    if dep_state in ("stale", "new", "error"):
                        state, reason = "stale", f"{dep} is {dep_state}"
                        break
            # Fresh apart from a later failed run: the last good output is still
            # what the code and the inputs would produce, so it can be read.
            output_current = state == "fresh"
            # A failure stands until the code runs again: a later run that was
            # cached, skipped or is only queued doesn't clear it.
            if la and la["status"] == "failed" and (ls is None or la["queued_at"] > ls["queued_at"]):
                if state != "error":
                    state, reason = "failed", la.get("error")
            if (name, key) in self._inflight:
                run = self.store.run(self._inflight[(name, key)])
                state = run["status"] if run else "running"
            sched = None
            if node.schedule:
                st = sched_state.get(name)
                nxt = None
                if name not in paused and st and st["cron"] == node.schedule:
                    nxt = self._next_fire(node.schedule, st["last_fired"])
                sched = {
                    "cron": node.schedule,
                    "paused": name in paused,
                    "next": nxt,
                    "source": node.schedule_source,
                }
            out[name] = {
                "state": state,
                "reason": reason,
                "last_run": lr,
                "rows": ls.get("rows") if ls else None,
                "schema": ls.get("schema") if ls else None,
                "output_kind": ls.get("kind") if ls else None,
                "artifact_id": ls.get("artifact_id") if ls else None,
                "finished_at": ls.get("finished_at") if ls else None,
                "schedule": sched,
                "run_params": own,
                "params_key": key,
                "output_current": output_current,
                # A run that failed importing something not installed: the
                # package to offer to install (see packages.missing_package).
                "missing_package": missing_package(reason) if state == "failed" else None,
            }
        return out

    def snapshot(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resolved = self.resolve_params(params)
        st = self.status(resolved)
        return {
            "name": self.project.name,
            "root": str(self.project.root),
            "timezone": self.project.config.get("timezone"),
            "params": [{"name": n, **spec, "value": resolved.get(n)} for n, spec in self.project.params.items()],
            "backfill": self.backfill_status(),
            "nodes": [{**n.to_dict(), **st.get(n.name, {})} for n in self.graph.nodes.values()],
            "views": [self._view_dict(v, st) for v in self.graph.views.values()],
            "stages": [g.to_dict() for g in self.graph.stages.values()],
        }

    # -- views ---------------------------------------------------------------
    def _view_dict(self, view: View, status: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """A view with its state for one parameter set, derived from the nodes
        it reads: ``ready`` when each has a fresh output, ``missing`` when one
        has none yet, ``stale`` when one has an output that is out of date
        (or being refreshed), ``error`` when the view itself is broken.
        ``outputs`` lists, per read node, the output the view would show now
        (``artifact_id`` names its stored file, see /artifacts)."""
        outputs = []
        for n in view.reads:
            st = status.get(n)
            if st is None:
                continue  # an unknown node: already the view's parse_error
            out = {"node": n, "state": st["state"], "artifact_id": st["artifact_id"], "kind": st["output_kind"]}
            if st["output_kind"] == "multi":
                out["parts"] = output_parts(st["schema"])
            outputs.append(out)
        live = self._view_live(view)
        missing = [o["node"] for o in outputs if not o["artifact_id"]]
        stale = [f"{o['node']} ({o['state']})" for o in outputs if o["artifact_id"] and o["state"] != "fresh"]
        if view.parse_error:
            state, reason = "error", view.parse_error
        elif missing:
            state, reason = "missing", f"no output yet: {', '.join(missing)}"
        elif stale:
            state, reason = "stale", f"out of date: {', '.join(stale)}"
        else:
            state, reason = "ready", None
        out = {**view.to_dict(), "state": state, "reason": reason, "outputs": outputs, "live": live}
        sig = self.npm.signature()
        if sig:  # the npm packages a view may import are part of what it runs
            out["hash"] = hashlib.sha256(f"{out['hash']}\0{sig}".encode()).hexdigest()[: len(out["hash"]) or 16]
        return out

    def _view_live(self, view: View) -> list[str]:
        """The live nodes a view depends on: those it reads and those
        upstream of them (see ``Node.live``), in run order."""
        g = self.graph
        reads = [n for n in view.reads if n in g.nodes]
        around = set(reads).union(*(g.ancestors(n) for n in reads)) if reads else set()
        live = {n for n in around if g.nodes[n].live is not None and not g.nodes[n].parse_error}
        try:
            return g.topo_order(live) if live else []
        except ValueError:
            return sorted(live)

    def refresh_view(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Re-run the live nodes a view depends on, for one parameter set, as
        the view is opened: the view shows what is stored now and updates
        when the new outputs land. A live node runs unless it is already
        running (or about to) for these parameters, or its output is newer
        than its ``live`` seconds; the nodes between it and what the view
        reads are rechecked after it, so the view's reads come up to date
        in the same batch. Never waits for the runs. Raises KeyError when
        there is no such view.

        Returns ``{"live", "started", "skipped", "params"}``: the view's live
        nodes, the ones re-run now, and why each other one was not."""
        self.graph = self.project.load()
        view = self.graph.views.get(name)
        if view is None:
            raise KeyError(name)
        g = self.graph
        params = self.resolve_params(params)
        live = self._view_live(view)
        status = self.status(params) if live else {}
        now = time.time()
        started: list[str] = []
        skipped: dict[str, str] = {}
        keys: dict[str, str] = {}
        for n in live:
            st = status[n]
            keys[n] = st["params_key"]
            age = now - st["finished_at"] if st.get("finished_at") else None
            if (n, keys[n]) in self._inflight or (n, keys[n]) in self._live_pending:
                skipped[n] = "already running"
            elif st["state"] == "fresh" and age is not None and age < (g.nodes[n].live or 0):
                skipped[n] = f"refreshed {int(age)}s ago"
            else:
                started.append(n)
        if started:
            reads = {n for n in view.reads if n in g.nodes}
            # What the view reads, between a live node and the view: re-run
            # after it when its output changed, cached otherwise.
            below = set().union(*(g.descendants(n) for n in started))
            between = (reads.union(*(g.ancestors(r) for r in reads)) & below) - set(started)
            pending = {(n, keys[n]) for n in started}
            self._live_pending |= pending

            async def go() -> None:
                try:
                    await self.run(
                        sorted(set(started) | between), params=params, force=set(started), recheck=between, trigger="live"
                    )
                finally:
                    self._live_pending -= pending

            self.spawn(go())
        return {"live": live, "started": started, "skipped": skipped, "params": params}

    def views(self, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        st = self.status(params)
        return [self._view_dict(v, st) for v in self.graph.views.values()]

    def view_info(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One view as ``views`` lists it. Raises KeyError when it does not exist."""
        view = self.graph.views.get(name)
        if view is None:
            raise KeyError(name)
        return self._view_dict(view, self.status(params))

    def read_view(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """A view, its parsed manifest (null when ``view.json`` is missing or
        not JSON) and the source of every file. Raises KeyError / ValueError."""
        self.project.view_dir(name)
        info = self.view_info(name, params)
        sources = []
        for rel in info["files"]:
            try:
                sources.append({"path": rel, "source": self.project.read_view_file(name, rel)})
            except (KeyError, OSError, UnicodeDecodeError):
                continue  # removed or not text since the last reload
        text = next((f["source"] for f in sources if f["path"] == VIEW_MANIFEST), None)
        try:
            manifest = json.loads(text) if text is not None else None
        except json.JSONDecodeError:
            manifest = None
        return {**info, "manifest": manifest, "sources": sources}

    def view_packages(self, name: str) -> dict[str, Any]:
        """The npm modules a view needs: for each bare import of its code of
        a package in the project's package.json, the modules it is made of,
        fetched through the cache (npm.Npm.modules). Raises KeyError / ValueError."""
        self.project.view_dir(name)
        view = self.graph.views.get(name)
        if view is None:
            raise KeyError(name)
        sources: dict[str, str] = {}
        for rel in view.files:
            try:
                sources[rel] = self.project.read_view_file(name, rel)
            except (KeyError, OSError, UnicodeDecodeError):
                continue
        return self.npm.modules(bare_imports(sources))

    def write_view_manifest(self, name: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Create or replace a view's manifest (creates the view)."""
        with self.editing():
            self.project.write_view_manifest(name, manifest)
            self.reload()
        return self.view_info(name)

    def write_view_file(self, name: str, rel: str, source: str) -> dict[str, Any]:
        """Write one file of a view (creating the view folder if needed)."""
        with self.editing():
            self.project.write_view_file(name, rel, source)
            self.reload()
        return self.view_info(name)

    def delete_view_file(self, name: str, rel: str) -> dict[str, Any]:
        with self.editing():
            self.project.delete_view_file(name, rel)
            self.reload()
        return self.view_info(name)

    def delete_view(self, name: str) -> dict[str, Any]:
        with self.editing():
            self.project.delete_view(name)
            self.reload()
        return {"deleted": name}

    # -- helper files ------------------------------------------------------
    def helper_users(self, rel: str) -> list[str]:
        """Nodes that import the helper ``rel``, directly or through other helpers."""
        return sorted(n.name for n in self.graph.nodes.values() if rel in n.helpers)

    def helpers(self) -> list[dict[str, Any]]:
        return [
            {"path": rel, "bytes": self.project.helper_path(rel).stat().st_size, "used_by": self.helper_users(rel)}
            for rel in self.project.helper_files()
        ]

    def read_helper(self, rel: str) -> dict[str, Any]:
        """Raises KeyError (no such file) or ValueError (not a helper path)."""
        source = self.project.read_helper(rel)
        return {"path": rel, "source": source, "used_by": self.helper_users(rel)}

    def write_helper(self, rel: str, source: str) -> dict[str, Any]:
        """Write a helper and reload the graph. ``used_by`` are the nodes that
        import it (now stale when it changed); ``parse_error`` is set when it is
        not valid Python (the file is still written, like a node file)."""
        with self.editing():
            self.project.write_helper(rel, source)
            self.reload()
        try:
            ast.parse(source)
            error = None
        except SyntaxError as e:
            error = f"SyntaxError line {e.lineno}: {e.msg}"
        return {"path": rel, "bytes": len(source.encode("utf-8")), "used_by": self.helper_users(rel), "parse_error": error}

    def delete_helper(self, rel: str) -> dict[str, Any]:
        users = self.helper_users(rel)
        with self.editing():
            self.project.delete_helper(rel)
            self.reload()
        return {"deleted": rel, "was_used_by": users}

    def is_fresh(self, name: str, params: dict[str, Any] | None = None) -> bool:
        return self.status(params).get(name, {}).get("state") == "fresh"

    # -- execution ---------------------------------------------------------
    async def run(
        self,
        targets: list[str],
        *,
        params: dict[str, Any] | None = None,
        force: set[str] | None = None,
        trigger: str = "manual",
        pull_upstream: bool = True,
        cascade: bool = True,
        recheck: set[str] | None = None,
        full_refresh: set[str] | None = None,
        on_queued: Callable[[dict[str, str]], None] | None = None,
    ) -> dict[str, Any]:
        """Run targets (and stale ancestors) for one parameter set.
        ``recheck`` are targets to queue even while they are fresh, and to
        check again once what they read has run: they run only if that
        changed their inputs, and are cached otherwise. ``full_refresh``
        are partitioned nodes to rebuild from an empty output (they run
        even when fresh): what the run returns becomes the whole output.
        ``on_queued`` is called with {node: run id} once the runs are
        queued, before any of them starts (not at all when nothing runs).
        Returns {node: final run row}."""
        self.graph = self.project.load()
        g = self.graph
        full_refresh = set(full_refresh or ())
        unpartitioned = sorted(n for n in full_refresh if n in g.nodes and not g.nodes[n].partition_by)
        if unpartitioned:
            raise ValueError(
                f"a full refresh is for partitioned nodes (partition_by); not: {', '.join(unpartitioned)}"
            )
        force = set(force or ()) | full_refresh
        recheck = set(recheck or ())
        unknown = [t for t in targets if t not in g.nodes]
        if unknown:
            raise KeyError(f"unknown node(s): {', '.join(unknown)}")
        # Resolved once, so "today" cannot roll over in the middle of a batch.
        params = self.resolve_params(params)
        status = self.status(params)
        wanted: set[str] = set()
        for t in targets:
            wanted.add(t)
            if pull_upstream:
                wanted |= {a for a in g.ancestors(t) if self._pull(status[a])}
        # A push node's output only comes from a push: nothing here runs it.
        plan = [
            n for n in g.topo_order(wanted)
            if (n in force or n in recheck or status[n]["state"] != "fresh") and not g.nodes[n].push
        ]
        if not plan:
            return {}

        batch = uuid.uuid4().hex[:8]
        # Whoever is signed in started it; a cascade task inherits the context
        # of the run it follows, so it names the same person.
        started_by, started_by_user = auth.current_id(), auth.current_uid()
        run_ids: dict[str, str] = {}
        keys: dict[str, str] = {}
        for n in plan:
            own, keys[n] = self.node_params(n, params)
            run_ids[n] = self.store.create_run(n, batch, trigger, own, keys[n], started_by, started_by_user)
            self._in_flight(n, keys[n], run_ids[n])
            self._emit_run(run_ids[n])
        prev_artifacts = {n: status[n]["artifact_id"] for n in plan}

        tasks: dict[str, asyncio.Task[str]] = {}

        async def run_one(name: str) -> str:
            run_id, key = run_ids[name], keys[name]
            try:
                return await attempts_of(name)
            except asyncio.CancelledError:
                if run_id not in self._cancelled:  # the workspace is stopping
                    raise
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                return self._finish(run_id, name, key, status="failed", error="cancelled")
            finally:
                self._run_tasks.pop(run_id, None)

        async def attempts_of(name: str) -> str:
            deps_in_plan = [d for d in g.upstream(name) if d in tasks]
            # asyncio.wait, not gather: cancelling this run must not cancel
            # the upstream runs it waits for.
            if deps_in_plan:
                await asyncio.wait([tasks[d] for d in deps_in_plan])
            results = [
                "failed" if tasks[d].cancelled() or tasks[d].exception() is not None else tasks[d].result()
                for d in deps_in_plan
            ]
            run_id, key = run_ids[name], keys[name]
            bad = [d for d, r in zip(deps_in_plan, results, strict=True) if r not in OK]
            if bad:
                return self._finish(run_id, name, key, status="skipped", error=f"upstream failed: {', '.join(bad)}")
            lock = self._node_locks.setdefault((name, key), asyncio.Lock())
            node = g.nodes[name]
            attempts = 1 + node.retries
            # The node's lock is held across retries, so another run of the
            # same output waits for the last attempt; the parallelism slot is
            # given back while waiting to retry.
            async with lock:
                for attempt in range(1, attempts + 1):
                    async with self._slots:
                        if attempt == 1 and name not in force and self._is_fresh_ignoring_inflight(name, key, params):
                            return self._finish(run_id, name, key, status="cached")
                        if run_id in self._cancelled:
                            return self._finish(run_id, name, key, status="failed", error="cancelled")
                        self._executing.add(run_id)
                        try:
                            result = await self._execute(
                                run_id, name, key, params, attempt=attempt, attempts=attempts,
                                full_refresh=name in full_refresh,
                            )
                        finally:
                            self._executing.discard(run_id)
                    if result != RETRY:
                        return result
                    if not await self._wait_to_retry(run_id, node.retry_delay * 2 ** (attempt - 1)):
                        return self._finish(run_id, name, key, status="failed", error="cancelled")
                return self._finish(run_id, name, key, status="failed", error="no attempts left")  # unreachable

        loop = asyncio.get_running_loop()
        for n in plan:
            tasks[n] = asyncio.create_task(run_one(n))
            self._run_tasks[run_ids[n]] = (loop, tasks[n])
        if on_queued is not None:
            on_queued(dict(run_ids))
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        finals = {n: self.store.run(run_ids[n]) for n in plan}

        if cascade:
            changed = [
                n for n in plan
                if finals[n] and finals[n]["status"] == "success"
                and finals[n]["artifact_id"] != prev_artifacts.get(n)
            ]
            downstream = self._cascade_set(changed) - set(plan)
            if downstream:
                self.spawn(
                    self.run(
                        sorted(downstream), params=params, trigger="cascade", pull_upstream=False, cascade=False
                    )
                )
        return finals

    async def push(
        self, name: str, data: Path, fmt: str, *, params: dict[str, Any] | None = None, note: str | None = None
    ) -> dict[str, Any]:
        """Record ``data`` (a ``parquet`` or ``json`` file, per ``fmt``) as
        push node ``name``'s output, for one parameter set: one run,
        triggered ``push``, whose new output cascades like a computed one.
        Returns the run. KeyError: no such node; ValueError: not a push node,
        or parameters that don't resolve. Data that can't be read is a failed
        run, as a node that raises is."""
        self.graph = self.project.load()
        g = self.graph
        node = g.nodes.get(name)
        if node is None:
            raise KeyError(f"no node named {name}")
        if not node.push:
            raise ValueError(
                f"{name} is not a push node: add push=True to its @exuvia.node(...) to upload its output"
            )
        if node.parse_error:
            raise ValueError(f"{name} has an error: {node.parse_error}")
        params = self.resolve_params(params)
        own, key = self.node_params(name, params)
        started_by, started_by_user = auth.current_id(), auth.current_uid()
        run_id = self.store.create_run(name, uuid.uuid4().hex[:8], "push", own, key, started_by, started_by_user)
        self._emit_run(run_id)
        # Two pushes of the same output are recorded one after the other.
        async with self._node_locks.setdefault((name, key), asyncio.Lock()):
            prev = self.store.last_success(name, key)
            self._in_flight(name, key, run_id)
            self.store.update_run(
                run_id,
                status="running",
                code_hash=node.code_hash,
                logic_hash=node.logic_hash,
                inputs={},
                started_at=time.time(),
            )
            self._emit_run(run_id)
            by = f" by {started_by}" if started_by else ""
            said = f": {note}" if note else ""
            self.store.append_log(run_id, f"exuvia: output pushed{by}{said}\n")
            out_dir = self._staging(run_id) if node.partition_by else self.project.state_dir / "artifacts" / name
            try:
                res = await asyncio.to_thread(save_pushed, data, fmt, out_dir)
                if node.partition_by and res["kind"] not in ("table", "none"):
                    raise PartitionError(f"{name} is partitioned by `{node.partition_by}`: push a table")
            except Exception as e:  # noqa: BLE001 - unreadable data fails the run, as a node that raises does
                error = f"{type(e).__name__}: {e}"
                self.store.append_log(run_id, f"exuvia: could not read the pushed {fmt}: {error}\n")
                self._finish(run_id, name, key, status="failed", error=error)
                return self.store.run(run_id) or {}
            except BaseException:  # the request went away: the run must not stay "running"
                self._finish(run_id, name, key, status="failed", error="the push was interrupted")
                raise
            try:
                if res["kind"] == "table":
                    self.store.append_log(run_id, f"exuvia: {res['rows']} rows, {len(res['schema'])} columns\n")
                if node.partition_by:
                    fields = await asyncio.to_thread(
                        self._commit_partitions, run_id, name, key, prev, res.get("artifact_path"), False
                    )
                else:
                    keyed = await asyncio.to_thread(self._enforce_key, run_id, name, res["kind"], res.get("artifact_path"))
                    if keyed:
                        res["artifact_path"], res["artifact_id"], res["rows"] = keyed
                    rel = self._rel(res["artifact_path"])
                    await asyncio.to_thread(self._upload, rel)
                    fields = {
                        "artifact_id": res["artifact_id"], "artifact_path": rel,
                        "kind": res["kind"], "rows": res["rows"], "schema": res["schema"],
                    }
            except (PartitionError, KeyViolation) as e:
                self.store.append_log(run_id, f"exuvia: {e}\n")
                self._finish(run_id, name, key, status="failed", error=f"{type(e).__name__}: {e}")
                return self.store.run(run_id) or {}
            except BaseException as e:  # the object store failed, or the request went away
                error = "the push was interrupted" if isinstance(e, asyncio.CancelledError) else f"{type(e).__name__}: {e}"
                self._finish(run_id, name, key, status="failed", error=error)
                raise
            finally:
                if node.partition_by:
                    shutil.rmtree(self._staging(run_id), ignore_errors=True)
            self._finish(run_id, name, key, status="success", **fields)
        if prev is None or prev["artifact_id"] != fields["artifact_id"]:
            downstream = self._cascade_set([name])
            if downstream:
                self.spawn(
                    self.run(sorted(downstream), params=params, trigger="cascade", pull_upstream=False, cascade=False)
                )
        return self.store.run(run_id) or {}

    async def _wait_to_retry(self, run_id: str, delay: float) -> bool:
        """Sleep before a retry. False when the run was cancelled meanwhile."""
        wake = asyncio.Event()
        self._retry_waits[run_id] = (asyncio.get_running_loop(), wake)
        try:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), delay)
        finally:
            self._retry_waits.pop(run_id, None)
        return run_id not in self._cancelled

    @staticmethod
    def _pull(st: dict[str, Any]) -> bool:
        """Whether a run must also run this ancestor first. Not when it is
        fresh, nor when only its latest run failed and its last good output is
        still current: that output is read instead, so a source that fails
        (an API outage, say) does not retry on every run downstream and block
        them. It retries on its own schedule or when run by name."""
        if st["state"] == "fresh":
            return False
        return not (st["state"] == "failed" and st["output_current"])

    def _in_flight(self, name: str, key: str, run_id: str) -> None:
        """Record a run that has begun (queued or running) until ``_finish``."""
        self._inflight[(name, key)] = run_id
        self._unfinished.setdefault((name, key), []).append(run_id)

    def _is_fresh_ignoring_inflight(self, name: str, key: str, params: dict[str, Any]) -> bool:
        run_id = self._inflight.pop((name, key), None)
        try:
            return self.is_fresh(name, params)
        finally:
            if run_id:
                self._inflight[(name, key)] = run_id

    def _cascade_set(self, changed: list[str]) -> set[str]:
        out: set[str] = set()
        stack = list(changed)
        while stack:
            cur = stack.pop()
            for child in self.graph.downstream(cur):
                if child not in out and self.graph.nodes[child].cascade:
                    out.add(child)
                    stack.append(child)
        return out

    def _emit_run(self, run_id: str) -> None:
        run = self.store.run(run_id)
        if run:
            run.pop("log", None)
            self.bus.publish({"type": "run", "run": run})

    def _finish(
        self, run_id: str, name: str, key: str, *, status: str, error: str | None = None, **fields: Any
    ) -> str:
        self.store.update_run(run_id, status=status, error=error, finished_at=time.time(), **fields)
        self._cancelled.discard(run_id)
        # The node is in flight for as long as any run of it is: when this was
        # the one shown, the one before it (still running or queued) is again.
        left = [r for r in self._unfinished.get((name, key), []) if r != run_id]
        if left:
            self._unfinished[(name, key)] = left
        else:
            self._unfinished.pop((name, key), None)
        if self._inflight.get((name, key)) == run_id:
            if left:
                self._inflight[(name, key)] = left[-1]
            else:
                del self._inflight[(name, key)]
        self._emit_run(run_id)
        self.bus.publish({"type": "graph"})
        if status == "failed" and error != "cancelled":  # someone stopped it: nothing to tell them
            self._notify_failure(run_id)
        return status

    def _notify_failure(self, run_id: str) -> None:
        """POST the failure webhook, if one is configured for this run's
        trigger (see notify.py). Never raises: notifying is not the run."""
        try:
            hook = failure_webhook(self.project.config)
            if hook is None:
                return
            url, triggers = hook
            run = self.store.run(run_id)
            if run is None or (triggers is not None and run.get("trigger") not in triggers):
                return
            base = os.environ.get("EXUVIA_PUBLIC_URL", "").strip().rstrip("/")
            link = f"{base}{self.api_prefix}/runs/{run_id}" if base else None
            send(url, payload(self.project.name, run, link))
        except Exception as e:  # noqa: BLE001 - a webhook that fails must never fail the run it reports
            print(f"exuvia: failure webhook skipped for run {run_id}: {e}", file=sys.stderr)

    def _rel(self, path: str | None) -> str | None:
        """Artifact paths are stored relative to the project root so a project
        (with its history) can be moved, exported and restored elsewhere."""
        if not path:
            return path
        try:
            return Path(path).resolve().relative_to(self.project.root).as_posix()
        except ValueError:
            return path

    def artifact_path(self, run: dict[str, Any]) -> str | None:
        """Local path of a run's output, downloading it from the remote
        artifact store first when this process does not have it yet.
        Blocking: call it off the event loop."""
        p = run.get("artifact_path")
        return self._local_file(p) if p else None

    def _local_file(self, rel: str) -> str:
        """Local path of an output file recorded at ``rel``, downloaded from
        the remote artifact store first when this process lacks it. Blocking."""
        local = Path(rel) if Path(rel).is_absolute() else self.project.root / rel
        if not local.exists():
            self._download(rel, local)
        return str(local)

    @staticmethod
    def is_partitioned(run: dict[str, Any] | None) -> bool:
        """Whether ``run``'s output is a partitioned one (a manifest version)."""
        return bool(run) and run.get("version") is not None  # type: ignore[union-attr]

    def manifest_files(self, run: dict[str, Any], lo: str | None = None, hi: str | None = None) -> list[dict[str, Any]]:
        """The chunk files of a partitioned run's output ([{file, partitions,
        rows}]), only those holding a partition in [lo, hi] when given."""
        return prune(self.store.manifest_files(run["node"], run.get("params_key") or "", run.get("version")), lo, hi)

    def table_files(self, run: dict[str, Any], lo: str | None = None, hi: str | None = None) -> list[str]:
        """The local files of a table output: one for an ordinary output,
        the manifest's chunks (pruned to [lo, hi]) for a partitioned one.
        Downloads what this process lacks. Blocking."""
        if self.is_partitioned(run):
            return [self._local_file(f["file"]) for f in self.manifest_files(run, lo, hi)]
        p = self.artifact_path(run)
        return [p] if p else []

    def _staging(self, run_id: str) -> Path:
        """Where a partitioned node's run leaves the rows it returned, until they are committed."""
        return self.project.state_dir / "staging" / run_id

    def _enforce_key(self, run_id: str, name: str, kind: str | None, path: str | None) -> tuple[str, str, int] | None:
        """Check an ordinary (not partitioned) output against its node's key
        (keys.py). Raises KeyViolation when it breaks it. With on_duplicate
        "last" and repeated keys, the output rewritten with one row per key:
        (path, artifact id, rows). None when there is nothing to change. Blocking."""
        node = self.graph.nodes[name]
        if not node.key or kind in (None, "none"):
            return None
        if kind != "table" or not path:
            raise KeyViolation(
                f"{name} declares a key ({', '.join(node.key)}), but its output is {kind}, not one table"
            )
        done = enforce_file(path, node.key, node.on_duplicate)
        if done is None:
            return None
        new_path, artifact_id, rows, dropped = done
        self.store.append_log(
            run_id, f"exuvia: kept the last row for each key ({', '.join(node.key)}); "
            f"dropped {dropped} row{'s' if dropped != 1 else ''} that repeated one\n",
        )
        return new_path, artifact_id, rows

    def _commit_partitions(
        self, run_id: str, name: str, key: str, prev: dict[str, Any] | None, staged: str | None, full_refresh: bool
    ) -> dict[str, Any]:
        """Commit the rows a partitioned node's run returned (the Parquet file
        ``staged``; None for none) on top of its current output ``prev``:
        write the changed partitions' chunks, compact, upload, and record the
        new version with the run. Returns the run's output fields. Raises
        PartitionError for rows that can't be committed. Blocking."""
        node = self.graph.nodes[name]
        column = node.partition_by or ""
        was = prev.get("partition_by") if self.is_partitioned(prev) else None
        if was and was != column and not full_refresh:
            raise PartitionError(
                f"{name} was partitioned by `{was}` and is now partitioned by `{column}`: "
                "run it with a full refresh to rebuild its output"
            )
        version = prev.get("version") if prev and was == column else None
        parts, files = self.store.manifest(name, key, version)
        base = Manifest(column, parts, files, version)
        w = Writer(self.project.state_dir / "artifacts" / name, lambda p: self._rel(str(p)) or "", self._local_file)
        unique_key = json.dumps(node.key) if node.key else None
        done = commit(
            base, staged, w, full_refresh=full_refresh, unique=node.key or None, on_duplicate=node.on_duplicate,
            # The stored output was checked against this key when it was written.
            verified=bool(version) and (prev or {}).get("unique_key") == unique_key,
            merge=node.merge,
            log=lambda text: self.store.append_log(run_id, text),
        )
        if done is None:  # identical rows, or none: the output stays as it is
            self.store.append_log(run_id, "exuvia: no partition changed\n")
            return {
                "artifact_id": base.logical_id, "artifact_path": None, "kind": "table", "rows": base.rows,
                "schema": (prev or {}).get("schema") if version else [], "version": version or 0,
                "partition_by": column, "unique_key": unique_key,
            }
        m = done.manifest
        for rel in done.written:
            self._upload(rel)
        changed = sort_values(v for v in set(m.partitions) | set(parts) if m.partitions.get(v) != parts.get(v))
        shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
        self.store.append_log(
            run_id, f"exuvia: {len(changed)} partition(s) written ({shown}); {len(m.files)} file(s), {m.rows} rows\n"
        )
        fields = {
            "artifact_id": m.logical_id, "artifact_path": None, "kind": "table", "rows": m.rows,
            "schema": w.schema([f["file"] for f in m.files]), "partition_by": column, "unique_key": unique_key,
        }
        fields["version"] = self.store.record_manifest(
            name, key, (m.partitions, m.files), run_id,
            **{k: v for k, v in fields.items() if k != "version"}, finished_at=time.time(),
        )
        return fields

    def part_file(self, run: dict[str, Any], part: str) -> tuple[Path, str] | None:
        """The local file of one part of a run with several outputs, and its
        kind (``table`` or ``json``); None when the run has no such part.

        DuckDB cannot read inside a zip, so a part is extracted once under
        ``.exuvia/parts/<node>/<artifact_id>/`` (a cache: hub exports leave
        it out, and it is rebuilt from the artifact when missing). Blocking."""
        if run.get("kind") != "multi" or not PART_NAME.match(part or ""):
            return None
        kind = dict((p["name"], p["kind"]) for p in output_parts(run.get("schema"))).get(part)
        if kind is None:
            return None
        member = f"{part}.{PART_EXT[kind]}"
        local = self.project.state_dir / "parts" / run["node"] / run["artifact_id"] / member
        if local.is_file():
            return local, kind
        src = self.artifact_path(run)
        if not src or not Path(src).is_file():
            return None
        local.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=local.parent, suffix=".part")
        os.close(fd)
        try:
            with zipfile.ZipFile(src) as z, z.open(member) as f_in, open(tmp, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.replace(tmp, local)
        except KeyError:
            return None
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return local, kind

    # -- remote artifact store (see artifacts.py) ------------------------------
    def _download(self, rel: str, local: Path) -> None:
        store = artifact_store()
        key = artifact_key(self.project.root.name, rel)
        if store is None or key is None:
            return
        try:
            if not store.fetch(key, local):
                print(f"exuvia: output {rel} is not in {store.describe()}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - the read that needed it reports the missing file
            print(f"exuvia: could not download {rel} from {store.describe()}: {e}", file=sys.stderr)

    def _upload(self, rel: str | None) -> bool:
        """Copy one output to the remote store. On failure it stays local only:
        hub exports keep carrying it and the next sync retries."""
        store = artifact_store()
        if store is None or not rel:
            return False
        key = artifact_key(self.project.root.name, rel)
        local = self.project.root / rel
        if key is None or not local.is_file():
            return False
        try:
            store.put(key, local)
        except Exception as e:  # noqa: BLE001 - the output stays local; False says it was not stored
            print(f"exuvia: could not upload {rel} to {store.describe()}: {e}", file=sys.stderr)
            return False
        self.store.mark_stored(rel)
        return True

    async def _sync_artifacts(self) -> None:
        """Upload outputs that are only on local disk: ones recorded before a
        store was configured, or whose upload failed."""
        uploaded = 0
        for rel in self.store.unstored_outputs():
            if await asyncio.to_thread(self._upload, rel):
                uploaded += 1
        if uploaded:
            # Nothing a client needs to hear, but the next export is smaller.
            self.bus.touch()

    async def _execute(
        self,
        run_id: str,
        name: str,
        key: str,
        params: dict[str, Any],
        *,
        attempt: int = 1,
        attempts: int = 1,
        full_refresh: bool = False,
    ) -> str:
        """Run one attempt of ``name``. Returns the run's final status, or
        RETRY when the attempt failed and the caller should try again (the
        run row is then back to ``queued``)."""
        g = self.graph
        node = g.nodes[name]
        if node.parse_error:
            return self._finish(run_id, name, key, status="failed", error=node.parse_error)
        inputs, input_ids = {}, {}
        for dep in g.upstream(name):
            ls = self.store.last_success(dep, self.node_params(dep, params)[1])
            if ls is None:
                why = "has not been pushed yet (exuvia push)" if g.nodes[dep].push else "has no output yet"
                return self._finish(run_id, name, key, status="failed", error=f"upstream {dep} {why}")
            if self.is_partitioned(ls):  # the whole partitioned output, as its chunk files
                paths = await asyncio.to_thread(self.table_files, ls)
                inputs[dep] = {"path": None, "paths": paths, "kind": "table", "artifact_id": ls["artifact_id"]}
            else:
                path = await asyncio.to_thread(self.artifact_path, ls)
                inputs[dep] = {"path": path, "kind": ls["kind"], "artifact_id": ls["artifact_id"]}
            input_ids[dep] = ls["artifact_id"]
        prev = self.store.last_success(name, key) if node.partition_by else None
        have: list[str] = []
        if node.partition_by and not full_refresh and self.is_partitioned(prev) and prev.get("partition_by") == node.partition_by:
            have = sort_values(self.store.manifest(name, key, prev.get("version"))[0])

        # A retry keeps the first attempt's start time, so the run's duration
        # covers every attempt.
        started = {"started_at": time.time()} if attempt == 1 else {}
        self.store.update_run(
            run_id, status="running", code_hash=node.code_hash, logic_hash=node.logic_hash, inputs=input_ids, **started
        )
        self._emit_run(run_id)

        # The project's variables, its group's and the hub's, read for each attempt so a
        # key fixed between retries is used. A secret's value never reaches
        # the log.
        try:
            env_rows, secret_rows = self._run_rows()
        except Exception as e:  # noqa: BLE001 - the hub's database, for the group's: the run must not stay "running"
            error = f"could not read the project's variables: {type(e).__name__}: {e}"
            return self._finish(run_id, name, key, status="failed", error=error)
        redact = envvars.Redactor(secret_rows)

        def on_log(text: str) -> None:
            text = redact(text) or ""
            self.store.append_log(run_id, text)
            self.bus.publish({"type": "log", "run_id": run_id, "node": name, "text": text})

        for r in env_rows:
            if not r["readable"]:
                on_log(f"exuvia: the secret {r['name']} is not set for this run: {r['problem']}.\n")

        req = ExecRequest(
            run_id=run_id,
            node=name,
            kind=node.kind,
            path=node.path,
            root=self.project.root,
            out_dir=self._staging(run_id) if node.partition_by else self.project.state_dir / "artifacts" / name,
            inputs=inputs,
            # Only what the node declared: reading anything else is an error,
            # because the output would not be keyed by it.
            params={p: params.get(p) for p in node.params},
            env=envvars.run_env(env_rows),
            partition_by=node.partition_by,
            partitions=have,
            timeout=node.timeout,
            cancelled=lambda: run_id in self._cancelled,
        )
        if node.partition_by and full_refresh:
            on_log("exuvia: full refresh: what this run returns becomes the whole output\n")
        # Executors block (subprocess pipes / HTTP streams); keep them off the loop.
        try:
            res = await asyncio.to_thread(self.executor.execute, req, on_log)
            if node.partition_by and res.ok:
                staged = res.artifact_path if res.kind == "table" else None
                try:
                    fields = await asyncio.to_thread(self._commit_partitions, run_id, name, key, prev, staged, full_refresh)
                except Exception as e:  # noqa: BLE001 - rows that can't be committed, or a write that failed
                    error = redact(f"{type(e).__name__}: {e}") or ""
                    on_log(f"exuvia: the output was not stored: {error}\n")
                    return self._finish(run_id, name, key, status="failed", error=error)
                return self._finish(run_id, name, key, status="success", **fields)
        finally:
            if node.partition_by:
                shutil.rmtree(self._staging(run_id), ignore_errors=True)
        res.error = redact(res.error)
        if not res.ok:
            if attempt < attempts and run_id not in self._cancelled and res.error != "cancelled":
                delay = node.retry_delay * 2 ** (attempt - 1)
                on_log(f"\nexuvia: attempt {attempt} of {attempts} failed: {res.error}. Retrying in {delay:g}s.\n\n")
                self.store.update_run(run_id, status="queued")
                self._emit_run(run_id)
                return RETRY
            return self._finish(run_id, name, key, status="failed", error=res.error)
        if node.key:
            try:
                keyed = await asyncio.to_thread(self._enforce_key, run_id, name, res.kind, res.artifact_path)
            except KeyViolation as e:  # the same rows would break it again: no retry
                error = redact(f"KeyViolation: {e}") or ""
                on_log(f"exuvia: the output was not stored: {error}\n")
                return self._finish(run_id, name, key, status="failed", error=error)
            if keyed:
                res.artifact_path, res.artifact_id, res.rows = keyed
        rel = self._rel(res.artifact_path)
        await asyncio.to_thread(self._upload, rel)
        return self._finish(
            run_id,
            name,
            key,
            status="success",
            artifact_id=res.artifact_id,
            artifact_path=rel,
            kind=res.kind,
            rows=res.rows,
            schema=res.schema,
        )

    def cancel(self, run_id: str) -> bool:
        """Stop a run that has not finished. A queued one (waiting for its
        upstream nodes, the node's lock or a free slot) ends at once, an
        attempt in progress is killed, a wait to retry is cut short. Either
        way it is not tried again, and it ends failed with the error
        ``cancelled``. False when it is not a run still going here. Safe to
        call from any thread."""
        # Marked first: the killed attempt must not find itself retryable.
        self._cancelled.add(run_id)
        waiting = self._retry_waits.get(run_id)
        if waiting is not None:
            loop, wake = waiting
            loop.call_soon_threadsafe(wake.set)
            return True
        entry = self._run_tasks.get(run_id)
        if entry is not None:
            loop, task = entry
            loop.call_soon_threadsafe(self._cancel_on_loop, run_id, task)
            return True
        if self.executor.cancel(run_id):
            return True
        self._cancelled.discard(run_id)
        return False

    def _cancel_on_loop(self, run_id: str, task: asyncio.Task[str]) -> None:
        """The rest of ``cancel``, on the run's own loop, where whether it is
        executing cannot change underneath: an attempt in progress is the
        executor's to stop (the task must not be cancelled while a thread
        runs it), anything else is the task's."""
        if task.done():
            return
        waiting = self._retry_waits.get(run_id)
        if waiting is not None:
            waiting[1].set()
        elif run_id in self._executing:
            # Before its process starts, the executor sees it in _cancelled
            # (ExecRequest.cancelled) and does not start it.
            threading.Thread(target=self.executor.cancel, args=(run_id,), daemon=True).start()
        else:
            task.cancel()

    # -- backfill ------------------------------------------------------------
    def backfill_status(self) -> dict[str, Any] | None:
        if self._backfill is None:
            return None
        return {k: v for k, v in self._backfill.items() if k != "cancel"}

    def start_backfill(
        self,
        param: str,
        values: list[Any],
        *,
        targets: list[str] | None = None,
        params: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Replay the graph once per value of ``param`` (e.g. a range of
        ``as_of`` dates), one value at a time, in the order given. Values that
        already have fresh outputs are cache hits unless ``force``."""
        if self._backfill and self._backfill["state"] == "running":
            raise RuntimeError("a backfill is already running")
        if param not in self.project.params:
            raise ValueError(f"unknown parameter `{param}`")
        base = dict(params or {})
        resolved = list(dict.fromkeys(self.resolve_params({**base, param: v})[param] for v in values))
        if not resolved:
            raise ValueError("no values to backfill")
        unknown = [t for t in targets or [] if t not in self.graph.nodes]
        if unknown:
            raise KeyError(f"unknown node(s): {', '.join(unknown)}")
        self._backfill = {
            "id": uuid.uuid4().hex[:8],
            "param": param,
            "values": resolved,
            "done": 0,
            "failed": [],
            "current": None,
            "state": "running",
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
        }
        self.spawn(self._run_backfill(param, resolved, targets, base, force))
        self.bus.publish({"type": "backfill", "backfill": self.backfill_status()})
        return self.backfill_status() or {}

    def cancel_backfill(self) -> bool:
        if self._backfill and self._backfill["state"] == "running":
            self._backfill["cancel"] = True
            return True
        return False

    async def _run_backfill(
        self, param: str, values: list[Any], targets: list[str] | None, base: dict[str, Any], force: bool
    ) -> None:
        bf = self._backfill
        assert bf is not None
        try:
            for value in values:
                if bf.get("cancel"):
                    bf["state"] = "cancelled"
                    break
                bf["current"] = value
                self.bus.publish({"type": "backfill", "backfill": self.backfill_status()})
                names = [n for n in (targets or list(self.graph.nodes)) if n in self.graph.nodes]
                finals = await self.run(
                    names,
                    params={**base, param: value},
                    force=set(names) if force else None,
                    trigger="backfill",
                    cascade=False,
                )
                if any(r and r["status"] in ("failed", "skipped") for r in finals.values()):
                    bf["failed"].append(value)
                bf["done"] += 1
            else:
                bf["state"] = "done"
        except Exception as e:  # noqa: BLE001 - a bad value or a vanished node must not kill the server
            bf["state"], bf["error"] = "failed", f"{type(e).__name__}: {e}"
        finally:
            bf["current"] = None
            bf["finished_at"] = time.time()
            self.bus.publish({"type": "backfill", "backfill": self.backfill_status()})
            self.bus.publish({"type": "graph"})

    # -- scheduling --------------------------------------------------------
    async def _schedule_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 - never let the scheduler die
                print(f"[exuvia] scheduler error: {e}", file=sys.stderr)
            await asyncio.sleep(5)

    async def tick(self, now: float | None = None) -> list[str]:
        """Start every scheduled node that is due; returns their names.

        Idempotent and restart-safe: the last fire time is persisted, so an
        external caller can tick as often or as rarely as it likes. Fires
        missed while the host was asleep are coalesced into one catch-up run,
        for the latest missed slot. A newly seen (or changed) schedule starts
        counting from now rather than firing immediately; paused schedules
        skip their backlog.

        A scheduled run uses the default parameters, except that a ``date``
        parameter defaulting to ``today`` gets the date of the cron slot the
        run is for, in the project timezone: a run that starts late, or a
        catch-up run after a weekend asleep, still computes the day it was
        due for. A fire that comes while the node is running waits until the
        run ends (``overlap="queue"``, at most one waits) or is dropped
        (``overlap="skip"``).

        Every change to a schedule's clock is announced on the bus, so the
        change counter moves and a host that persists on change saves it.
        """
        now = time.time() if now is None else now
        self.graph = self.project.load()
        paused = self.store.paused()
        states = self.store.schedule_states()
        running = {name for name, _ in self._inflight}
        due: list[str] = []
        clock_moved = False
        for node in self.graph.nodes.values():
            if not node.schedule or node.parse_error:
                continue
            st = states.get(node.name)
            if st is None or st["cron"] != node.schedule:
                self.store.set_schedule_state(node.name, node.schedule, now)
                clock_moved = True
                continue
            nxt = self._next_fire(node.schedule, st["last_fired"])
            if nxt > now:
                continue
            if node.name in running and node.name not in paused and node.overlap == "queue":
                continue  # still due at the next tick, once the run has ended
            self.store.set_schedule_state(node.name, node.schedule, now)
            clock_moved = True
            if node.name in paused:
                continue
            if node.name in running:
                print(f"[exuvia] {node.name}: scheduled run skipped, the last one is still running", file=sys.stderr)
                continue
            due.append(node.name)
            slot = max(nxt, self._prev_fire(node.schedule, now))
            # The clock started it, not whoever called /api/schedule/tick.
            self.spawn(
                self.run([node.name], params=self._slot_params(slot), force={node.name}, trigger="schedule"),
                context=Context(),
            )
        if clock_moved:
            self.bus.publish({"type": "graph"})
        return due

    def schedule(self, upcoming: int = 5, now: float | None = None) -> dict[str, Any]:
        """The project's schedule at a glance: every node with a cron, soonest
        first, with its next ``upcoming`` fire times (none while paused or
        broken), its last scheduled run, and the nodes a new output from it
        cascades to; and the live nodes, which run when a view that depends
        on them is opened rather than on a clock. Times are Unix seconds;
        ``timezone`` is the clock the crons are read on (null: the host's)."""
        now = time.time() if now is None else now
        g = self.graph
        paused = self.store.paused()
        states = self.store.schedule_states()
        status = self.status()
        nodes: list[dict[str, Any]] = []
        for node in g.nodes.values():
            if not node.schedule:
                continue
            times: list[float] = []
            if not node.parse_error and node.name not in paused:
                st = states.get(node.name)
                # Before a tick has started its clock, it fires from now on.
                t = self._next_fire(node.schedule, st["last_fired"] if st and st["cron"] == node.schedule else now)
                for _ in range(upcoming):
                    times.append(t)
                    t = self._next_fire(node.schedule, t)
            nodes.append({
                "name": node.name,
                "cron": node.schedule,
                "source": node.schedule_source,
                "paused": node.name in paused,
                "overlap": node.overlap,
                "next": times[0] if times else None,
                "upcoming": times,
                "last": self.store.last_by_trigger(node.name, "schedule"),
                "cascades": sorted(self._cascade_set([node.name])),
                "state": status.get(node.name, {}).get("state"),
                "parse_error": node.parse_error,
            })
        nodes.sort(key=lambda n: (n["next"] is None, n["next"] or 0, n["name"]))
        live = []
        for node in g.nodes.values():
            if node.live is None:
                continue
            views = sorted(v.name for v in g.views.values() if node.name in self._view_live(v))
            live.append({
                "name": node.name,
                "live": node.live,
                "views": views,
                "last": self.store.last_by_trigger(node.name, "live"),
                "state": status.get(node.name, {}).get("state"),
            })
        return {
            "timezone": self.project.config.get("timezone"),
            "scheduler": SCHEDULER,
            "nodes": nodes,
            "live": sorted(live, key=lambda n: n["name"]),
        }

    def next_fire_at(self, now: float | None = None) -> float | None:
        """When the next ``tick`` has work to do (None: nothing is scheduled).
        Lets an external scheduler sleep until then instead of polling. A
        fire queued behind a run still in progress is asked about again in
        QUEUED_RECHECK_S, not "now", so the host does not tick in a loop."""
        now = time.time() if now is None else now
        paused = self.store.paused()
        states = self.store.schedule_states()
        running = {name for name, _ in self._inflight}
        times: list[float] = []
        for node in self.graph.nodes.values():
            if not node.schedule or node.parse_error or node.name in paused:
                continue
            st = states.get(node.name)
            if st is None or st["cron"] != node.schedule:
                times.append(now)  # a tick has to see it once to start its clock
            else:
                nxt = self._next_fire(node.schedule, st["last_fired"])
                if nxt <= now and node.name in running and node.overlap == "queue":
                    nxt = now + QUEUED_RECHECK_S
                times.append(nxt)
        return min(times) if times else None

    def _local(self, ts: float) -> datetime:
        """``ts`` on the project's clock (its timezone, else the host's). From UTC,
        as a naive local time near the epoch is an OSError on Windows."""
        tz = self.project.tz
        return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts, timezone.utc).astimezone()

    def _next_fire(self, cron: str, after: float) -> float:
        return croniter(cron, self._local(after)).get_next(datetime).timestamp()

    def _prev_fire(self, cron: str, before: float) -> float:
        return croniter(cron, self._local(before)).get_prev(datetime).timestamp()

    def _slot_params(self, slot: float) -> dict[str, Any]:
        """The parameters a run for the cron slot at ``slot`` overrides: each
        ``date`` parameter whose default is ``today`` gets the slot's date."""
        day = self._local(slot).date().isoformat()
        return {
            name: day
            for name, spec in self.project.params.items()
            if spec.get("type") == "date" and str(spec.get("default") or "").strip().lower() == "today"
        }

    def set_paused(self, name: str, paused: bool) -> None:
        self.store.set_paused(name, paused)
        self.bus.publish({"type": "graph"})

    def set_schedule(self, name: str, cron: str | None) -> None:
        """Set a node's schedule from outside its code (stored in
        ``exuvia.json``). ``""`` turns scheduling off for the node; ``None``
        drops the override and falls back to the schedule in the node file."""
        node = self.graph.nodes.get(name)
        if cron and node is not None and node.push:
            raise ValueError(
                f"{name} is a push node: its output arrives when it is pushed. "
                "Schedule the script that pushes it on the machine that has the data."
            )
        if cron:
            _validate_cron(cron)

        def mutate(cfg: dict[str, Any]) -> None:
            schedules = cfg.get("schedules") if isinstance(cfg.get("schedules"), dict) else {}
            if cron is None:
                schedules.pop(name, None)
            else:
                schedules[name] = cron
            if schedules:
                cfg["schedules"] = schedules
            else:
                cfg.pop("schedules", None)

        with self.editing():
            self.project.update_config(mutate)
            self.reload()

    # -- data access -------------------------------------------------------
    def _output_run(self, name: str, params: dict[str, Any] | None, run_id: str | None) -> dict[str, Any] | None:
        """The successful run whose output is ``name``'s current one for a
        parameter set, or the run ``run_id`` when it is a success of ``name``."""
        if run_id:
            ls = self.store.run(run_id)
            return ls if ls is not None and ls["node"] == name and ls["status"] == "success" else None
        if name in self.graph.nodes:
            return self.store.last_success(name, self.node_params(name, self.resolve_params(params))[1])
        return None

    def _partitioned_read(
        self, ls: dict[str, Any], partitions: tuple[str | None, str | None] | None
    ) -> tuple[Any, Any, int, dict[str, Any]] | None:
        """For a partitioned run: (connection, relation over its files, total
        rows, meta), keeping only the partitions in ``partitions`` (lo, hi)
        when given. The relation is None for an empty output. None for a run
        that isn't partitioned."""
        if not self.is_partitioned(ls):
            return None
        import duckdb

        lo, hi = partitions or (None, None)
        files = self.manifest_files(ls, lo, hi)
        meta: dict[str, Any] = {"partition_by": ls.get("partition_by")}
        con = duckdb.connect()
        if partitions is None:
            total = int(ls.get("rows") or 0)
            keep = None
        else:
            keep = [p for f in files for p in f["partitions"] if in_range(p, lo, hi)]
            parts = self.store.manifest(ls["node"], ls.get("params_key") or "", ls.get("version"))[0]
            total = sum(parts[p][1] for p in keep if p in parts)
            meta["partitions"] = [lo, hi]
        if not files:
            every = self.manifest_files(ls) if partitions is not None else []
            if not every:
                return con, None, total, meta
            # No partition in the range: no rows, but the output's columns.
            return con, con.read_parquet([self._local_file(f["file"]) for f in every], union_by_name=True).limit(0), total, meta
        rel = con.read_parquet([self._local_file(f["file"]) for f in files], union_by_name=True)
        if keep is not None:
            column = '"' + str(ls.get("partition_by")).replace('"', '""') + '"'
            con.execute("create or replace temp table __keep (v varchar)")
            con.executemany("insert into __keep values (?)", [(p,) for p in keep])
            rel = rel.filter(f"cast({column} as varchar) in (select v from __keep)")
        return con, rel, total, meta

    def preview(
        self,
        name: str,
        limit: int = 200,
        offset: int = 0,
        params: dict[str, Any] | None = None,
        run_id: str | None = None,
        part: str | None = None,
        partitions: tuple[str | None, str | None] | None = None,
    ) -> dict[str, Any]:
        """A node's current output for a parameter set, or the output of one
        specific historical run. For a node with several outputs, ``part``
        picks one; without it the answer lists them (``parts``). For a
        partitioned output, ``partitions`` (lo, hi) keeps only those
        partitions, reading only the files that hold them.
        Raises KeyError for a ``part`` the output does not have."""
        ls = self._output_run(name, params, run_id)
        if ls is None:
            return {"kind": None, "rows": [], "columns": [], "total": 0}
        meta = {"run_id": ls["id"], "run_params": ls.get("params") or {}, "finished_at": ls.get("finished_at")}
        kind = ls["kind"]
        total = ls.get("rows")
        read = self._partitioned_read(ls, partitions)
        if read is not None:
            if part is not None:
                raise KeyError(f"{name} has a single output, not several: leave out part")
            con, rel, total, extra = read
            if rel is None:
                return {"kind": "table", "columns": [], "rows": [], "total": total, **meta, **extra}
            rel = rel.limit(int(limit), int(offset))
            return {
                "kind": "table",
                "columns": [{"name": c, "type": str(t)} for c, t in zip(rel.columns, rel.types, strict=True)],
                "rows": jsonable([list(r) for r in rel.fetchall()]),
                "total": total,
                **meta,
                **extra,
            }
        if kind == "multi":
            parts = output_parts(ls.get("schema"))
            meta["parts"] = parts
            if part is None:
                return {"kind": "multi", **meta}
            found = self.part_file(ls, part)
            if found is None:
                raise KeyError(f"{name} has no output {part!r} (it has {', '.join(p['name'] for p in parts)})")
            path, kind = str(found[0]), found[1]
            meta["part"] = part
            total = next((p["rows"] for p in parts if p["name"] == part), None)
        elif part is not None:
            raise KeyError(f"{name} has a single output, not several: leave out part")
        elif kind not in ("table", "json"):
            return {"kind": "none", "value": None, **meta}  # incl. a retired pickle output: never read
        else:
            path = self.artifact_path(ls)
        if kind == "table":
            import duckdb

            con = duckdb.connect()
            rel = con.sql(f"select * from read_parquet(?) limit {int(limit)} offset {int(offset)}", params=[path])
            cols = rel.columns
            rows = [list(r) for r in rel.fetchall()]
            return {
                "kind": "table",
                "columns": [{"name": c, "type": str(t)} for c, t in zip(cols, rel.types, strict=True)],
                "rows": jsonable(rows),
                "total": total,
                **meta,
            }
        if kind == "json":
            # finite(): an output stored before NaN became null when written
            return {"kind": "json", "value": finite(json.loads(Path(path or "").read_text(encoding="utf-8"))), **meta}
        return {"kind": "none", "value": None, **meta}

    def preview_arrow(
        self,
        name: str,
        limit: int | None = None,
        offset: int = 0,
        params: dict[str, Any] | None = None,
        run_id: str | None = None,
        part: str | None = None,
        partitions: tuple[str | None, str | None] | None = None,
    ) -> ArrowStream | None:
        """The table ``preview`` would answer with, as an Arrow stream: every
        row unless ``limit`` is given. None when there is no table output
        (``preview`` then answers, or says what is wrong with ``part``)."""
        ls = self._output_run(name, params, run_id)
        if ls is None:
            return None
        total = ls.get("rows")
        extra: dict[str, Any] = {}
        read = self._partitioned_read(ls, partitions)
        if read is not None:
            con, rel, total, extra = read
            if rel is None or part is not None:
                return None  # nothing to stream: preview answers
            if limit is not None or offset:
                rel = rel.limit(_ALL_ROWS if limit is None else int(limit), int(offset))
            meta = {
                "kind": "table", "total": total, "run_id": ls["id"], "run_params": ls.get("params") or {},
                "finished_at": ls.get("finished_at"), **extra,
            }
            return ArrowStream(rel, meta, owner=con)
        if ls["kind"] == "multi" and part is not None:
            found = self.part_file(ls, part)
            if found is None or found[1] != "table":
                return None
            path = str(found[0])
            total = next((p["rows"] for p in output_parts(ls.get("schema")) if p["name"] == part), None)
            extra["part"] = part
        elif ls["kind"] == "table" and part is None:
            path = self.artifact_path(ls)
        else:
            return None
        import duckdb

        con = duckdb.connect()
        rel = con.sql("select * from read_parquet(?)", params=[path])
        if limit is not None or offset:
            rel = rel.limit(_ALL_ROWS if limit is None else int(limit), int(offset))
        meta = {
            "kind": "table",
            "total": total,
            "run_id": ls["id"],
            "run_params": ls.get("params") or {},
            "finished_at": ls.get("finished_at"),
            **extra,
        }
        return ArrowStream(rel, meta, owner=con)

    def table_stats(
        self,
        name: str,
        params: dict[str, Any] | None = None,
        run_id: str | None = None,
        part: str | None = None,
    ) -> dict[str, Any] | None:
        """What a table output's Parquet footers say (tablestats.py): its
        rows, row groups, sizes and compression, and each column's type (as
        SQL has it), nulls, min and max. Reads no rows. None when the output
        (or ``part``) is not a table. Blocking: call it off the event loop."""
        ls = self._output_run(name, params, run_id)
        if ls is None:
            return None
        if ls["kind"] == "multi" and part is not None:
            found = self.part_file(ls, part)
            if found is None or found[1] != "table":
                return None
            files = [str(found[0])]
        elif part is None and (ls["kind"] == "table" or self.is_partitioned(ls)):
            files = self.table_files(ls)
        else:
            return None
        if not files:
            return None
        import duckdb

        from .tablestats import parquet_stats

        out = parquet_stats(files)
        by_name = out.pop("columns")
        # The types the Data tab and SQL show (DuckDB's), from the footers too.
        con = duckdb.connect()
        try:
            rel = con.read_parquet(files, union_by_name=True).limit(0)
            out["columns"] = [
                {"name": c, "type": str(t), **by_name.get(c, {})} for c, t in zip(rel.columns, rel.types, strict=True)
            ]
        finally:
            con.close()
        return {"kind": "table", "run_id": ls["id"], "finished_at": ls.get("finished_at"), **out}

    def artifact_file(self, node: str, artifact_id: str, part: str | None = None) -> tuple[Path, str] | None:
        """The stored file of one of ``node``'s outputs and its kind (``table``,
        ``json`` or ``multi``), fetched from the remote artifact store first
        when this process does not have it. With ``part``, that part of an
        output with several (kind ``table`` or ``json``). None when no
        successful run produced it. Blocking: call it off the event loop."""
        run = self.store.run_with_artifact(node, artifact_id)
        if run is not None and part is not None:
            return self.part_file(run, part)
        if run is None or self.is_partitioned(run):
            # One chunk of a partitioned output (a partitioned output's own
            # id names no single file: GET /nodes/{node}/partitions lists them).
            rel = self.store.chunk(node, f"{artifact_id}.parquet") if part is None else None
            if rel is None:
                return None
            local = Path(self._local_file(rel))
            return (local, "table") if local.is_file() else None
        if run["kind"] not in ("table", "json", "multi"):
            return None
        path = self.artifact_path(run)
        if not path or not Path(path).is_file():
            return None
        return Path(path), run["kind"]

    def partitions_of(self, name: str, params: dict[str, Any] | None = None, run_id: str | None = None) -> dict[str, Any] | None:
        """A partitioned output's manifest: its partitions (value, digest,
        rows) and the chunk files that hold them. None when ``name``'s output
        for these parameters (or run ``run_id``) is not a partitioned one."""
        ls = self._output_run(name, params, run_id)
        if not self.is_partitioned(ls):
            return None
        assert ls is not None
        parts, files = self.store.manifest(name, ls.get("params_key") or "", ls.get("version"))
        files = prune(files, None, None)  # in partition order
        return {
            "node": name,
            "partition_by": ls.get("partition_by"),
            "artifact_id": ls["artifact_id"],
            "version": ls.get("version"),
            "run_id": ls["id"],
            "run_params": ls.get("params") or {},
            "rows": ls.get("rows"),
            "partitions": [{"value": v, "digest": parts[v][0], "rows": parts[v][1]} for v in sort_values(parts)],
            "files": [
                {"artifact_id": Path(f["file"]).stem, "partitions": sort_values(f["partitions"]), "rows": f["rows"]}
                for f in files
            ],
        }

    def history(self, name: str) -> list[dict[str, Any]]:
        """Every parameter set ``name`` has an output for, newest first."""
        node = self.graph.nodes.get(name)
        return [
            {
                "run_id": r["id"],
                "run_params": r.get("params") or {},
                "params_key": r["params_key"],
                "artifact_id": r["artifact_id"],
                "kind": r["kind"],
                "rows": r["rows"],
                "trigger": r["trigger"],
                "finished_at": r["finished_at"],
                "code_hash": r["code_hash"],
                "code_current": bool(node and r["code_hash"] == node.code_hash),
            }
            for r in self.store.history(name)
        ]

    def _outputs_db(self, params: dict[str, Any] | None, only: list[str] | None = None) -> Any:
        """outputs.outputs_db for this project: the locked DuckDB sandbox
        with every table output (or only ``only``'s) as a view."""
        return outputs.outputs_db(self, params, only)

    def add_output_views(
        self, con: Any, params: dict[str, Any] | None, only: list[str] | None = None, catalog: str | None = None
    ) -> list[str]:
        """outputs.add_output_views for this project."""
        return outputs.add_output_views(self, con, params, only, catalog)

    def _view_reads(self, view: str) -> list[str]:
        """The nodes a view may read: those in its manifest's ``reads`` that
        exist. Raises KeyError when there is no such view."""
        v = self.graph.views.get(view)
        if v is None:
            raise KeyError(view)
        return [n for n in v.reads if n in self.graph.nodes]

    def view_node(self, view: str, node: str) -> None:
        """Raise KeyError for a missing view, PermissionError when ``node`` is
        not one the view declares in ``reads``."""
        if node not in self._view_reads(view):
            raise PermissionError(f"view {view} does not read {node} (add it to reads in view.json)")

    def view_query(self, view: str, sql: str, limit: int = 500, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """``query`` for a view: only the nodes in its ``reads`` exist."""
        return outputs.query_json(self._outputs_db(params, only=self._view_reads(view)), sql, limit)

    def view_query_arrow(
        self, view: str, sql: str, limit: int | None = None, params: dict[str, Any] | None = None
    ) -> ArrowStream:
        return outputs.query_arrow(self._outputs_db(params, only=self._view_reads(view)), sql, limit)

    def query(self, sql: str, limit: int = 500, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ad-hoc DuckDB SQL over node outputs (the views of ``_outputs_db``)."""
        return outputs.query_json(self._outputs_db(params), sql, limit)

    def query_arrow(self, sql: str, limit: int | None = None, params: dict[str, Any] | None = None) -> ArrowStream:
        """``query`` as an Arrow stream: every row unless ``limit`` is given."""
        return outputs.query_arrow(self._outputs_db(params), sql, limit)


def _view_of(rel: str) -> str | None:
    parts = rel.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == VIEW_DIR else None


def _node_of(rel: str) -> str | None:
    p = Path(rel)
    if p.parent.as_posix() == "nodes" and p.suffix in NODE_SUFFIXES and not p.name.startswith("_"):
        return p.stem
    return None
