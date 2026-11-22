"""Many projects behind one server.

A hub is a directory whose sub-directories are exuvia projects. Projects are
loaded on first use, each with its own workspace, scheduler state and agent.
A host application (one that treats each of its documents as a project) gets:

* create / delete / list projects, optionally from a template directory;
* one ``tick()`` that advances every project's schedules and says when the
  next one is due, so the host can sleep in between;
* ``state()`` — a change counter and a busy flag — plus ``export()`` /
  ``restore()`` of everything (code, run history, artifacts), so a host with
  an ephemeral disk can persist the hub somewhere durable and bring it back.
  With a remote artifact store (``artifacts.py``) the export leaves out the
  outputs the store already holds; with a shared database (``EXUVIA_DB``,
  see ``db.py``) it leaves out the records, which already live there.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from .artifacts import artifact_store
from .db import database_url, shared_database
from .project import VIEW_DIR, Project
from .store import Store, project_scope, stored_paths_at

PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SKIP_DIRS = {"__pycache__", "node_modules"}
DB_REL = ".exuvia/exuvia.db"
PACKAGES_REL = ".exuvia/packages/"
# Parts extracted from multi-output artifacts (workspace.part_file): a cache.
PARTS_REL = ".exuvia/parts/"
# The hub's user list (users.py), under the hub's directory beside the
# projects, unless EXUVIA_DB holds it. Exports carry it at the same path.
HUB_USERS_DB = ".exuvia/hub.db"
# Package installs shared by the hub's projects (packages.py), beside them.
# Projects that resolve to the same packages share an install, and it outlives
# restarts and exuvia upgrades that leave the packages as they were.
HUB_PACKAGES = ".exuvia/packages"
# Pauses between attempts to remove a folder (see _remove_tree).
REMOVE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


class StillInUse(OSError):
    """A folder could not be removed because a file in it is still open."""


class Hub:
    def __init__(self, root: str | Path, templates: str | Path | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.templates_dir = Path(templates).resolve() if templates else None
        self._ctxs: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._structural = 0  # creates / deletes / restores
        self._retired = 0  # change counts of workspaces that were unloaded
        self._export_path: Path | None = None
        # How to label code changes found when a project next loads.
        self._load_origin: dict[str, str] = {}
        # Everyone who has signed in (set by create_hub_app when people sign in).
        self.users: Any = None
        # Who can open and change each project (sharing.py; set by
        # create_hub_app when the hub has any auth). None: everyone can.
        self.sharing: Any = None
        # The projects a request may see (create_hub_app filters by sharing).
        self.visible: Any = self.list

    # -- projects ------------------------------------------------------------
    def exists(self, project_id: str) -> bool:
        return (self.root / project_id / "nodes").is_dir()

    def ids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and PROJECT_ID.match(p.name) and self.exists(p.name))

    def list(self) -> list[dict[str, Any]]:
        out = []
        for pid in self.ids():
            ctx = self._ctxs.get(pid)
            project = ctx.ws.project if ctx else Project(self.root / pid)
            out.append({"id": pid, "name": project.name, "nodes": len(project.node_files()), "loaded": ctx is not None})
        return out

    def templates(self) -> list[str]:
        if not self.templates_dir or not self.templates_dir.is_dir():
            return []
        return sorted(p.name for p in self.templates_dir.iterdir() if p.is_dir() and PROJECT_ID.match(p.name))

    async def get(self, project_id: str) -> Any:
        """The loaded project (workspace + agent), starting it on first use."""
        from .agent import Agent
        from .server import Ctx
        from .workspace import Workspace

        async with self._lock:
            ctx = self._ctxs.get(project_id)
            if ctx is None:
                ws = Workspace(
                    self.root / project_id,
                    origin=self._load_origin.pop(project_id, "load"),
                    packages_store=self.root / HUB_PACKAGES,
                )
                ws.api_prefix = f"/p/{project_id}/api"
                await ws.start()
                ctx = Ctx(ws, Agent(ws))
                self._ctxs[project_id] = ctx
            return ctx

    async def create(
        self, project_id: str, name: str | None = None, template: str | None = None,
        access: str | None = None, owner: str | None = None,
    ) -> None:
        """A new project. With sharing on, ``owner`` (a user id; default: the
        signed-in person, nobody for the operator) owns it and ``access`` is
        what everyone else signed in gets (default EXUVIA_DEFAULT_ACCESS)."""
        from . import auth
        from .sharing import GENERAL

        if not PROJECT_ID.match(project_id):
            raise ValueError("project ids are 1-64 letters, digits, `-` or `_`")
        if access is not None and access not in GENERAL:
            raise ValueError(f"access must be one of {', '.join(GENERAL)}")
        if owner is None:
            owner = auth.current_uid()
        elif self.users is None or self.users.get(owner) is None:
            raise ValueError(f"no user {owner} to own the project")
        target = self.root / project_id
        if target.exists():
            raise FileExistsError(project_id)
        if template:
            if template not in self.templates():
                raise ValueError(f"unknown template {template!r}")
            assert self.templates_dir is not None
            shutil.copytree(
                self.templates_dir / template, target, ignore=shutil.ignore_patterns("__pycache__", ".exuvia")
            )
        project = Project(target)  # creates nodes/ and .exuvia/
        if name:
            project.update_config(lambda cfg: cfg.__setitem__("name", name))
        if self.sharing is not None:
            self.sharing.init_project(project_id, owner, access)
        self._structural += 1

    async def apply_template(self, project_id: str, template: str) -> None:
        """Bring an existing project's code in line with a template: its node
        files, views, ``exuvia.json`` and notes are replaced by the template's,
        and node files and views the template does not have are removed (a
        view is replaced as a whole folder, so no stale file of an old version
        lingers). The project keeps its name, run history and stored outputs,
        and every removed file stays restorable from its code revisions. For
        projects whose code is maintained somewhere else (a host that ships it
        as a template)."""
        if template not in self.templates():
            raise ValueError(f"unknown template {template!r}")
        if not self.exists(project_id):
            raise KeyError(project_id)
        assert self.templates_dir is not None
        source = self.templates_dir / template
        target = self.root / project_id
        async with self._lock:
            await self._unload(project_id)
            name = Project(target).name
            source_nodes = source / "nodes"
            keep = {p.name for p in source_nodes.iterdir() if p.is_file()} if source_nodes.is_dir() else set()
            for path in (target / "nodes").iterdir():
                if path.is_file() and path.name not in keep:
                    path.unlink()
            await _remove_tree(target / VIEW_DIR)
            shutil.copytree(
                source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", ".exuvia")
            )
            Project(target).update_config(lambda cfg: cfg.__setitem__("name", name))
            self._load_origin[project_id] = "template"
            self._structural += 1

    async def _unload(self, project_id: str) -> None:
        ctx = self._ctxs.pop(project_id, None)
        if ctx is None:
            return
        for sid in list(ctx.agent.running):
            ctx.agent.cancel(sid)
        await ctx.ws.stop()
        ctx.ws.store.close()
        self._retired += ctx.ws.bus.version

    async def delete(self, project_id: str) -> None:
        async with self._lock:
            await self._unload(project_id)
            try:
                await _remove_tree(self.root / project_id)
            finally:
                self._structural += 1
            shared = _shared_store(project_id)
            if shared is not None:
                shared.delete_all()
            if self.sharing is not None:
                self.sharing.forget(project_id)
            self._load_origin.pop(project_id, None)
        store = artifact_store()
        if store is not None:
            try:
                await asyncio.to_thread(store.delete_under, f"{project_id}/")
            except Exception as e:  # the project is gone either way; say what was left behind
                print(f"exuvia: could not delete {project_id}'s outputs from {store.describe()}: {e}", file=sys.stderr)

    async def close(self) -> None:
        async with self._lock:
            for pid in list(self._ctxs):
                await self._unload(pid)
        if self.sharing is not None:
            self.sharing.close()
        if self.users is not None:
            self.users.close()

    # -- scheduling ------------------------------------------------------------
    async def tick(self) -> dict[str, Any]:
        """Advance every project's schedules. A project that fails to load or
        tick is reported under ``errors`` (project id → reason) and skipped;
        it never stops the other projects' schedules."""
        started: dict[str, list[str]] = {}
        upcoming: list[float] = []
        errors: dict[str, str] = {}
        for pid in self.ids():
            try:
                ctx = await self.get(pid)
                due = await ctx.ws.tick()
                nxt = ctx.ws.next_fire_at()
            except Exception as e:  # one broken project must not stall the hub
                errors[pid] = f"{type(e).__name__}: {e}"[:500]
                print(f"exuvia: schedule tick failed for {pid}: {errors[pid]}", file=sys.stderr)
                continue
            if due:
                started[pid] = due
            if nxt is not None:
                upcoming.append(nxt)
        out: dict[str, Any] = {"started": started, "next": min(upcoming) if upcoming else None}
        if errors:
            out["errors"] = errors
        return out

    # -- persistence -------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        ctxs = list(self._ctxs.values())
        return {
            "version": self._structural + self._retired + sum(c.ws.bus.version for c in ctxs),
            "busy": any(c.ws.busy or c.agent.running for c in ctxs),
            "projects": len(self.ids()),
            "loaded": len(ctxs),
        }

    def export(self) -> Path:
        """Write every project to a .tar.gz and return its path. Run databases
        are copied through SQLite's backup API, so this is safe mid-run.
        Outputs already in a remote artifact store (see artifacts.py) are
        left out: the restored project downloads them when it reads them."""
        fd, name = tempfile.mkstemp(prefix="exuvia-hub-", suffix=".tar.gz")
        os.close(fd)
        out = Path(name)
        remote = artifact_store() is not None
        with tempfile.TemporaryDirectory(prefix="exuvia-export-") as scratch, tarfile.open(out, "w:gz") as tar:
            for pid in self.ids():
                base = self.root / pid
                # The database first: it says which outputs the store already has.
                db = base / DB_REL
                copy: Path | None = None
                # With a shared database the records live there, not in the archive.
                shared = _shared_store(pid)
                if shared is None and db.is_file():
                    copy = Path(scratch) / f"{pid}.db"
                    ctx = self._ctxs.get(pid)
                    if ctx is not None:
                        ctx.ws.store.backup_to(copy)
                    else:
                        _backup_db(db, copy)
                if not remote:
                    stored: set[str] = set()
                elif shared is not None:
                    stored = shared.stored_paths()
                else:
                    stored = stored_paths_at(copy) if copy is not None else set()
                # nodes/ marks a project folder for restore(); it is added on its
                # own because a project with no nodes has no file inside it.
                tar.add(base / "nodes", arcname=f"{pid}/nodes", recursive=False)
                for path in sorted(base.rglob("*")):
                    rel = path.relative_to(base)
                    if not path.is_file() or SKIP_DIRS & set(rel.parts):
                        continue
                    posix = rel.as_posix()
                    if posix.startswith(DB_REL):
                        continue  # the live database (and its -wal/-shm) is copied above
                    if posix.startswith(PACKAGES_REL):
                        continue  # installed packages: rebuilt from pyproject.toml when the project loads
                    if posix.startswith(PARTS_REL):
                        continue  # extracted from the artifacts on the next read
                    if rel.name.endswith((".tmp", ".part")):
                        continue  # half-written artifacts
                    if posix in stored:
                        continue  # durable in the remote artifact store
                    tar.add(path, arcname=f"{pid}/{posix}")
                if copy is not None:
                    tar.add(copy, arcname=f"{pid}/{DB_REL}")
            # The user list and the projects' sharing, unless a shared database holds them.
            if (self.users is not None or self.sharing is not None) and not database_url():
                users = Path(scratch) / "hub.db"
                if self.users is not None:
                    self.users.dump_to(users)
                if self.sharing is not None:
                    self.sharing.dump_to(users)
                tar.add(users, arcname=HUB_USERS_DB)
        if self._export_path and self._export_path != out:
            self._export_path.unlink(missing_ok=True)
        self._export_path = out
        return out

    async def restore(self, blob: bytes) -> list[str]:
        """Replace the projects found in an export archive."""
        with tempfile.TemporaryDirectory(prefix="exuvia-import-") as scratch:
            try:
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
                    tar.extractall(scratch, filter="data")  # refuses links and paths outside
            except (tarfile.TarError, OSError, EOFError) as e:
                raise ValueError(f"not a exuvia hub export: {e}") from e
            found = sorted(
                p.name for p in Path(scratch).iterdir() if p.is_dir() and PROJECT_ID.match(p.name) and (p / "nodes").is_dir()
            )
            users = Path(scratch) / HUB_USERS_DB
            if not found:
                raise ValueError("the archive contains no projects")
            # The archive's user list and sharing replace this hub's (roles,
            # disabled users and who can open what included), unless a shared
            # database holds them.
            if users.is_file() and not database_url():
                if self.users is not None and "users" in _sqlite_tables(users):
                    self.users.replace_from(users)
                if self.sharing is not None:
                    self.sharing.replace_from(users)
            async with self._lock:
                try:
                    for pid in found:
                        await self._unload(pid)
                        # The target must be gone: moving onto a folder that
                        # survived would put the project inside it, at pid/pid.
                        await _remove_tree(self.root / pid)
                        shutil.move(str(Path(scratch) / pid), str(self.root / pid))
                        self._load_origin[pid] = "import"
                finally:
                    self._structural += 1
        return found


async def _remove_tree(path: Path) -> None:
    """Remove ``path`` completely, or raise ``StillInUse``. On Windows a file
    closed a moment ago (the run database, a file watcher's handle, a virus
    scanner reading a new file) can stay locked briefly, so removal is retried
    with a short backoff. A folder that survives every attempt is an error,
    never a silent half-removal that a later copy or move lands inside."""
    for delay in (*REMOVE_RETRY_DELAYS, None):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        if delay is not None:
            await asyncio.sleep(delay)
    raise StillInUse(f"could not remove {path.name}: a file in it is still in use; try again")


def _sqlite_tables(path: Path) -> set[str]:
    """The tables in a SQLite file (an archive's hub.db may hold only some)."""
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    except sqlite3.DatabaseError:
        return set()
    finally:
        con.close()


def _backup_db(src: Path, dst: Path) -> None:
    source = sqlite3.connect(src)
    dest = sqlite3.connect(dst)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()


def _shared_store(project_id: str) -> Store | None:
    """The project's records in the shared database, when ``EXUVIA_DB`` names
    one (None: each project keeps its own SQLite file)."""
    url = database_url()
    if not url:
        return None
    return Store(shared_database(url), scope=project_scope(project_id), owns_db=False)
