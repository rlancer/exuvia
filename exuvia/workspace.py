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
  node to re-run on its cadence and lets the cascade carry the result.

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
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import Context, ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from croniter import croniter

from .arrowstream import ArrowStream
from .artifacts import artifact_key, artifact_store
from .events import EventBus
from .executor import CappedLog, ExecRequest, ExecResult, make_executor
from .project import NODE_SUFFIXES, VIEW_DIR, VIEW_MANIFEST, Graph, Project, View, _hash, _validate_cron
from .store import CodeChange, clean_origin, open_store

MAX_PARALLEL = int(os.environ.get("EXUVIA_MAX_PARALLEL", "4"))
# "internal": a loop in this process evaluates schedules every few seconds.
# "external": something else POSTs /api/schedule/tick (Durable Object alarm,
# k8s CronJob, system cron) — for hosts that sleep or scale to zero.
SCHEDULER = os.environ.get("EXUVIA_SCHEDULER", "internal")
HISTORY_VIEW_LIMIT = 400  # parameter sets unioned into a `<node>__runs` view
_ALL_ROWS = 2**62  # the limit of an offset-only read
# Project files besides nodes/ whose versions are kept as code revisions.
VERSIONED_FILES = ("exuvia.json", "AGENTS.md")

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
    def __init__(self, root: str | Path, origin: str = "load"):
        """``origin`` labels code found changed since the last recorded
        revision when the project loads (a template applied, an import, edits
        made while nothing was watching)."""
        self.project = Project(root)
        # Held while project files are written and reloaded (see editing()).
        self._edit_lock = threading.RLock()
        self.store = open_store(self.project)
        self.store.fail_interrupted()
        self.bus = EventBus()
        self.graph: Graph = self.project.load()
        self.record_code(origin)
        self._node_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._slots = asyncio.Semaphore(MAX_PARALLEL)
        self._inflight: dict[tuple[str, str], str] = {}  # (node, params_key) -> run_id
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop_watching: asyncio.Event | None = None
        self._backfill: dict[str, Any] | None = None
        self.executor = make_executor(self.project.root, MAX_PARALLEL)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self.bus.bind(asyncio.get_running_loop())
        self.executor.start()
        # A fresh context: a change the watcher sees was made by editing files,
        # not by whichever request happened to load the project.
        self._stop_watching = asyncio.Event()
        self._tasks = [asyncio.create_task(self._watch_files(self._stop_watching), context=Context())]
        if artifact_store() is not None:
            self._tasks.append(asyncio.create_task(self._sync_artifacts()))
        if SCHEDULER == "internal":
            self._tasks.append(asyncio.create_task(self._schedule_loop()))

    async def stop(self) -> None:
        """Stop the background tasks and wait for them to finish. Once this
        returns the file watcher has no project file open, so the caller may
        replace or delete the project's files (on Windows a file another
        thread is reading cannot be deleted; see Hub.apply_template)."""
        if self._stop_watching is not None:
            self._stop_watching.set()  # the watch thread checks it every 50 ms
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
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
            self.record_code(_ORIGIN.get() or "file")
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

    def record_code(self, origin: str) -> int:
        """Store a revision for every versioned file whose content differs
        from its latest revision, and for every one that disappeared. Returns
        how many were recorded."""
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
        n = self.store.record_code(changes, origin)
        if n:
            self.bus.touch()
        return n

    def code_revisions(
        self, node: str | None = None, limit: int = 200, *, view: str | None = None
    ) -> list[dict[str, Any]]:
        """Saved versions newest first (one node's, one view's files, or every file's)."""
        current = {}
        for rel, (_, _, p) in self._code_files().items():
            try:
                current[rel] = _hash(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
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

    def restore_node(self, name: str, digest: str) -> None:
        """Write a recorded version of node ``name`` back as its current
        code. A restore is itself a new revision; history only grows."""
        revs = [r for r in self.store.code_revisions(node=name, limit=100000) if r["hash"] == digest]
        source = self.store.code_source(digest)
        if not revs or source is None:
            raise KeyError(f"no saved version {digest} of {name}")
        kind = NODE_SUFFIXES.get(Path(revs[0]["path"]).suffix, "python")
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
        if source is None or not any(r["hash"] == digest for r in self.store.code_revisions(path=path, limit=100000)):
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
            current[name] = ls["artifact_id"] if ls else None
            reason = None
            if node.parse_error:
                state, reason = "error", node.parse_error
            elif ls is None:
                state, reason = "new", "never run"
            elif ls["code_hash"] != node.code_hash:
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
            if lr and lr["status"] == "failed" and (ls is None or lr["queued_at"] > ls["queued_at"]):
                if state != "error":
                    state, reason = "failed", lr.get("error")
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
            outputs.append(
                {"node": n, "state": st["state"], "artifact_id": st["artifact_id"], "kind": st["output_kind"]}
            )
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
        return {**view.to_dict(), "state": state, "reason": reason, "outputs": outputs}

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
    ) -> dict[str, Any]:
        """Run targets (and stale ancestors) for one parameter set.
        Returns {node: final run row}."""
        self.graph = self.project.load()
        g = self.graph
        force = set(force or ())
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
                wanted |= {a for a in g.ancestors(t) if status[a]["state"] != "fresh"}
        plan = [n for n in g.topo_order(wanted) if n in force or status[n]["state"] != "fresh"]
        if not plan:
            return {}

        batch = uuid.uuid4().hex[:8]
        run_ids: dict[str, str] = {}
        keys: dict[str, str] = {}
        for n in plan:
            own, keys[n] = self.node_params(n, params)
            run_ids[n] = self.store.create_run(n, batch, trigger, own, keys[n])
            self._inflight[(n, keys[n])] = run_ids[n]
            self._emit_run(run_ids[n])
        prev_artifacts = {n: status[n]["artifact_id"] for n in plan}

        tasks: dict[str, asyncio.Task[str]] = {}

        async def run_one(name: str) -> str:
            deps_in_plan = [d for d in g.upstream(name) if d in tasks]
            results = await asyncio.gather(*(tasks[d] for d in deps_in_plan))
            run_id, key = run_ids[name], keys[name]
            if any(r != "success" for r in results):
                bad = [d for d, r in zip(deps_in_plan, results) if r != "success"]
                return self._finish(run_id, name, key, status="skipped", error=f"upstream failed: {', '.join(bad)}")
            lock = self._node_locks.setdefault((name, key), asyncio.Lock())
            async with lock, self._slots:
                if name not in force and self._is_fresh_ignoring_inflight(name, key, params):
                    return self._finish(run_id, name, key, status="cached")
                return await self._execute(run_id, name, key, params)

        for n in plan:
            tasks[n] = asyncio.create_task(run_one(n))
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
                asyncio.create_task(
                    self.run(
                        sorted(downstream), params=params, trigger="cascade", pull_upstream=False, cascade=False
                    )
                )
        return finals

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
        if self._inflight.get((name, key)) == run_id:
            del self._inflight[(name, key)]
        self._emit_run(run_id)
        self.bus.publish({"type": "graph"})
        return status

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
        if not p:
            return None
        local = Path(p) if Path(p).is_absolute() else self.project.root / p
        if not local.exists():
            self._download(p, local)
        return str(local)

    # -- remote artifact store (see artifacts.py) ------------------------------
    def _download(self, rel: str, local: Path) -> None:
        store = artifact_store()
        key = artifact_key(self.project.root.name, rel)
        if store is None or key is None:
            return
        try:
            if not store.fetch(key, local):
                print(f"exuvia: output {rel} is not in {store.describe()}", file=sys.stderr)
        except Exception as e:  # the read that needed it reports the missing file
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
        except Exception as e:
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

    async def _execute(self, run_id: str, name: str, key: str, params: dict[str, Any]) -> str:
        g = self.graph
        node = g.nodes[name]
        if node.parse_error:
            return self._finish(run_id, name, key, status="failed", error=node.parse_error)
        inputs, input_ids = {}, {}
        for dep in g.upstream(name):
            ls = self.store.last_success(dep, self.node_params(dep, params)[1])
            if ls is None:
                return self._finish(run_id, name, key, status="failed", error=f"upstream {dep} has no output yet")
            path = await asyncio.to_thread(self.artifact_path, ls)
            inputs[dep] = {"path": path, "kind": ls["kind"], "artifact_id": ls["artifact_id"]}
            input_ids[dep] = ls["artifact_id"]

        self.store.update_run(
            run_id, status="running", started_at=time.time(), code_hash=node.code_hash, inputs=input_ids
        )
        self._emit_run(run_id)

        def sink(text: str) -> None:
            self.store.append_log(run_id, text)
            self.bus.publish({"type": "log", "run_id": run_id, "node": name, "text": text})

        log = CappedLog(sink)  # bounds what the store and the UI receive

        def execute() -> ExecResult:
            try:
                return self.executor.execute(req, log)
            finally:
                log.close()

        req = ExecRequest(
            run_id=run_id,
            node=name,
            kind=node.kind,
            path=node.path,
            root=self.project.root,
            out_dir=self.project.state_dir / "artifacts" / name,
            inputs=inputs,
            # Only what the node declared: reading anything else is an error,
            # because the output would not be keyed by it.
            params={p: params.get(p) for p in node.params},
        )
        # Executors block (subprocess pipes / HTTP streams); keep them off the loop.
        res = await asyncio.to_thread(execute)
        if not res.ok:
            return self._finish(run_id, name, key, status="failed", error=res.error)
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
        return self.executor.cancel(run_id)

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
        task = asyncio.create_task(self._run_backfill(param, resolved, targets, base, force))
        self._tasks.append(task)
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
        except Exception as e:  # a bad value or a vanished node must not kill the server
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
            except Exception as e:  # never let the scheduler die
                print(f"[exuvia] scheduler error: {e}", file=sys.stderr)
            await asyncio.sleep(5)

    async def tick(self, now: float | None = None) -> list[str]:
        """Start every scheduled node that is due; returns their names.

        Idempotent and restart-safe: the last fire time is persisted, so an
        external caller can tick as often or as rarely as it likes. Fires
        missed while the host was asleep are coalesced into one catch-up run.
        A newly seen (or changed) schedule starts counting from now rather
        than firing immediately; paused schedules skip their backlog.
        Scheduled runs use the default parameters, resolved at fire time.
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
            if self._next_fire(node.schedule, st["last_fired"]) > now:
                continue
            self.store.set_schedule_state(node.name, node.schedule, now)
            clock_moved = True
            if node.name in paused or node.name in running:
                continue
            due.append(node.name)
            asyncio.create_task(self.run([node.name], force={node.name}, trigger="schedule"))
        if clock_moved:
            self.bus.publish({"type": "graph"})
        return due

    def next_fire_at(self, now: float | None = None) -> float | None:
        """When the next ``tick`` has work to do (None: nothing is scheduled).
        Lets an external scheduler sleep until then instead of polling."""
        now = time.time() if now is None else now
        paused = self.store.paused()
        states = self.store.schedule_states()
        times: list[float] = []
        for node in self.graph.nodes.values():
            if not node.schedule or node.parse_error or node.name in paused:
                continue
            st = states.get(node.name)
            if st is None or st["cron"] != node.schedule:
                times.append(now)  # a tick has to see it once to start its clock
            else:
                times.append(self._next_fire(node.schedule, st["last_fired"]))
        return min(times) if times else None

    def _next_fire(self, cron: str, after: float) -> float:
        tz = self.project.tz
        base = datetime.fromtimestamp(after, tz) if tz else datetime.fromtimestamp(after).astimezone()
        return croniter(cron, base).get_next(datetime).timestamp()

    def set_paused(self, name: str, paused: bool) -> None:
        self.store.set_paused(name, paused)
        self.bus.publish({"type": "graph"})

    def set_schedule(self, name: str, cron: str | None) -> None:
        """Set a node's schedule from outside its code (stored in
        ``exuvia.json``). ``""`` turns scheduling off for the node; ``None``
        drops the override and falls back to the schedule in the node file."""
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

    def preview(
        self,
        name: str,
        limit: int = 200,
        offset: int = 0,
        params: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """A node's current output for a parameter set, or the output of one
        specific historical run."""
        ls = self._output_run(name, params, run_id)
        if ls is None:
            return {"kind": None, "rows": [], "columns": [], "total": 0}
        meta = {"run_id": ls["id"], "run_params": ls.get("params") or {}, "finished_at": ls.get("finished_at")}
        kind = ls["kind"]
        if kind not in ("table", "json"):
            return {"kind": "none", "value": None, **meta}  # incl. a retired pickle output: never read
        path = self.artifact_path(ls)
        if kind == "table":
            import duckdb

            con = duckdb.connect()
            rel = con.sql(f"select * from read_parquet(?) limit {int(limit)} offset {int(offset)}", params=[path])
            cols = rel.columns
            rows = [list(r) for r in rel.fetchall()]
            return {
                "kind": "table",
                "columns": [{"name": c, "type": str(t)} for c, t in zip(cols, rel.types)],
                "rows": _jsonable(rows),
                "total": ls.get("rows"),
                **meta,
            }
        if kind == "json":
            return {"kind": "json", "value": json.loads(Path(path or "").read_text(encoding="utf-8")), **meta}
        return {"kind": "none", "value": None, **meta}

    def preview_arrow(
        self,
        name: str,
        limit: int | None = None,
        offset: int = 0,
        params: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> ArrowStream | None:
        """The table ``preview`` would answer with, as an Arrow stream: every
        row unless ``limit`` is given. None when there is no table output."""
        ls = self._output_run(name, params, run_id)
        if ls is None or ls["kind"] != "table":
            return None
        import duckdb

        con = duckdb.connect()
        rel = con.sql("select * from read_parquet(?)", params=[self.artifact_path(ls)])
        if limit is not None or offset:
            rel = rel.limit(_ALL_ROWS if limit is None else int(limit), int(offset))
        meta = {
            "kind": "table",
            "total": ls.get("rows"),
            "run_id": ls["id"],
            "run_params": ls.get("params") or {},
            "finished_at": ls.get("finished_at"),
        }
        return ArrowStream(rel, meta, owner=con)

    def artifact_file(self, node: str, artifact_id: str) -> tuple[Path, str] | None:
        """The stored file of one of ``node``'s outputs and its kind (``table``
        or ``json``), fetched from the remote artifact store first when this
        process does not have it. None when no successful run produced it.
        Blocking: call it off the event loop."""
        run = self.store.run_with_artifact(node, artifact_id)
        if run is None or run["kind"] not in ("table", "json"):
            return None
        path = self.artifact_path(run)
        if not path or not Path(path).is_file():
            return None
        return Path(path), run["kind"]

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
        """A DuckDB connection with every table output as a view.

        ``<node>`` is the node's current table for ``params`` (the defaults
        when omitted). ``<node>__runs`` unions its output across every
        parameter set it has run with, adding one ``_<param>`` column per
        parameter — the view to compare runs over time.

        With ``only`` the connection is a sandbox for code that may read just
        those nodes (a view's ``reads``): it has their views and nothing
        else, and it is locked so the SQL cannot reach any other file, the
        network or an extension (``read_parquet`` of another node's output,
        ``COPY ... TO``, ``ATTACH`` of a file, ``INSTALL`` all fail) nor undo
        the lock.
        """
        import duckdb

        resolved = self.resolve_params(params)
        sandbox = only is not None
        con = duckdb.connect(
            config={"autoinstall_known_extensions": False, "autoload_known_extensions": False} if sandbox else {}
        )
        names = [n for n in self.graph.nodes if not sandbox or n in only]
        views: list[tuple[str, str]] = []  # (view name, select)
        files: list[str] = []

        def read(path: str | None, cols: str = "") -> str:
            files.append(path or "")
            return f"select {cols}* from read_parquet({_lit(path)})"

        for name in names:
            ls = self.store.last_success(name, self.node_params(name, resolved)[1])
            if ls and ls["kind"] == "table":
                views.append((name, read(self.artifact_path(ls))))
            if not self.graph.effective_params(name):
                continue
            parts = []
            for run in self.store.history(name, HISTORY_VIEW_LIMIT):
                if run["kind"] != "table":
                    continue
                cols = ", ".join(f'{_lit(v)} as "_{k}"' for k, v in sorted((run.get("params") or {}).items()))
                parts.append(read(self.artifact_path(run), cols + ", " if cols else ""))
            if parts:
                views.append((f"{name}__runs", " union all by name ".join(parts)))
        if sandbox:
            # Only the files behind these views stay readable once external
            # access is off; the lock makes every setting final.
            con.execute("set allowed_paths = ?", [sorted({f for f in files if f})])
        for view, select in views:
            try:
                con.execute(f'create view "{view}" as {select}')
            except duckdb.Error:
                if not view.endswith("__runs"):
                    raise
                # incompatible schemas across runs: leave the history view out
        if sandbox:
            con.execute("set enable_external_access = false")
            con.execute("set lock_configuration = true")
        return con

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
        return self._query(self._outputs_db(params, only=self._view_reads(view)), sql, limit)

    def view_query_arrow(
        self, view: str, sql: str, limit: int | None = None, params: dict[str, Any] | None = None
    ) -> ArrowStream:
        return self._query_arrow(self._outputs_db(params, only=self._view_reads(view)), sql, limit)

    def query(self, sql: str, limit: int = 500, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ad-hoc DuckDB SQL over node outputs (the views of ``_outputs_db``)."""
        return self._query(self._outputs_db(params), sql, limit)

    def query_arrow(self, sql: str, limit: int | None = None, params: dict[str, Any] | None = None) -> ArrowStream:
        """``query`` as an Arrow stream: every row unless ``limit`` is given."""
        return self._query_arrow(self._outputs_db(params), sql, limit)

    @staticmethod
    def _query(con: Any, sql: str, limit: int) -> dict[str, Any]:
        rel = con.sql(sql)
        if rel is None:
            return {"columns": [], "rows": []}
        rows = rel.limit(limit).fetchall()
        return {
            "columns": [{"name": c, "type": str(t)} for c, t in zip(rel.columns, rel.types)],
            "rows": _jsonable([list(r) for r in rows]),
        }

    @staticmethod
    def _query_arrow(con: Any, sql: str, limit: int | None) -> ArrowStream:
        rel = con.sql(sql)
        if rel is not None and limit is not None:
            rel = rel.limit(int(limit))
        return ArrowStream(rel, owner=con)


def _lit(value: Any) -> str:
    """A SQL literal (DDL cannot take prepared parameters)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _jsonable(rows: list[list[Any]]) -> list[list[Any]]:
    import math
    from datetime import date
    from decimal import Decimal

    def conv(v: Any) -> Any:
        if v is None or isinstance(v, (str, bool, int)):
            return v
        if isinstance(v, float):
            return None if math.isnan(v) or math.isinf(v) else v
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, (datetime, date)):
            return v.isoformat()
        if isinstance(v, (list, tuple)):
            return [conv(x) for x in v]
        if isinstance(v, dict):
            return {str(k): conv(x) for k, x in v.items()}
        return str(v)

    return [[conv(v) for v in r] for r in rows]


def _view_of(rel: str) -> str | None:
    parts = rel.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == VIEW_DIR else None


def _node_of(rel: str) -> str | None:
    p = Path(rel)
    if p.parent.as_posix() == "nodes" and p.suffix in NODE_SUFFIXES and not p.name.startswith("_"):
        return p.stem
    return None
