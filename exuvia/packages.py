"""Packages a project declares for its nodes, in its own ``pyproject.toml``.

A project lists what its nodes import beyond what exuvia comes with in the
standard place, ``[project] dependencies`` (``uv add scipy`` in the project
folder writes it). Before a node runs, the executor makes sure those packages
are installed, with uv, into a folder of their own; the runner appends that
folder to ``sys.path``. So a project's packages travel with it: through git,
into a hub, and to a remote executor in its bundle.

The packages sit on top of exuvia's own environment rather than in a
separate one, which keeps the pre-warmed runners shared by every project:

* They are resolved with exuvia's installed versions as constraints, so a
  project that needs, say, ``pandas<2`` while exuvia runs pandas 3 fails with
  uv's explanation instead of importing a mix of the two. When uv's
  backtracking to older releases ends at some other error, the conflict
  with exuvia's versions is looked for and reported first.
* Packages exuvia already has are excluded from the install, so the folder
  holds only what the project adds.

uv runs in the project folder, so its own settings there apply, among them
private package indexes (``[[tool.uv.index]]``) and where each package comes
from (``[tool.uv.sources]``). Index credentials come from the environment
(``UV_INDEX_<NAME>_USERNAME`` / ``_PASSWORD``), which a project's ``.env`` can
set. ``uv.lock`` is not read.

Installs live in a store: a project's ``.exuvia/packages``, or one folder
shared by every project on a server (a hub's ``.exuvia/packages``, an
executor service's ``packages``). Each install folder is named after what it
holds (the packages and versions the list resolved to, the index each came
from, the Python and the platform), so projects that resolve to the same set
share one, and a new exuvia version that leaves the set as it was keeps it:

* ``.inputs/<input key>`` names the install for one ``pyproject.toml`` with
  one exuvia environment, so a run finds it without resolving anything.
* On a miss (the list or exuvia changed) the list is resolved again,
  preferring the versions of the install the project used before; when that
  gives the same set, the folder is reused and nothing is installed.
* ``.refs/`` records which install each project uses. Installs no project
  uses are removed, an hour after they were made at the earliest.

A shared store keeps uv's own cache beside the installs (``.uv-cache``), on
the server's volume, so an install that does run copies files instead of
downloading them again after a restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import tomllib
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Callable

from .pool import node_environ

PYPROJECT = "pyproject.toml"
COMPLETE = ".complete"
RESOLVED = ".resolved.txt"  # in an install: the resolution it holds
INPUTS = ".inputs"  # in a store: input key -> the install it resolved to
REFS = ".refs"  # in a store: which install each project uses
UV_CACHE = ".uv-cache"  # in a shared store: uv's cache
KEEP_S = 3600  # an install no project uses is kept this long after it is made
CONFLICT_HINT = "hint: the exact (==) versions are the ones exuvia itself runs with; a project's packages must work with them"
INSTALL_TIMEOUT_S = int(os.environ.get("EXUVIA_PACKAGES_TIMEOUT", "900"))

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
# uv runs at once for this many installs (pre-builds and runs alike), so a hub
# that starts with many projects to install does not start them all together.
_SLOTS = threading.Semaphore(max(1, int(os.environ.get("EXUVIA_PACKAGES_CONCURRENCY", "2"))))


class PackagesError(RuntimeError):
    pass


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared(root: Path) -> list[str]:
    """The project's ``[project] dependencies``; empty without a
    ``pyproject.toml`` or the list."""
    path = root / PYPROJECT
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise PackagesError(f"{PYPROJECT} does not parse: {e}") from None
    deps = (data.get("project") or {}).get("dependencies") or []
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        raise PackagesError(f"[project] dependencies in {PYPROJECT} must be a list of strings")
    return [d.strip() for d in deps if d.strip()]


@lru_cache(maxsize=1)
def _base() -> tuple[tuple[str, str], ...]:
    """``(name, version)`` of every distribution in exuvia's environment, in
    import order (the first of a name is the one Python imports)."""
    seen: dict[str, tuple[str, str]] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name and _norm(name) not in seen:
            seen[_norm(name)] = (name, dist.version)
    return tuple(sorted(seen.values(), key=lambda nv: _norm(nv[0])))


def _uv() -> str:
    try:
        from uv import find_uv_bin

        return find_uv_bin()
    except (ImportError, FileNotFoundError):
        pass
    found = shutil.which("uv")
    if not found:
        raise PackagesError("installing the packages in pyproject.toml needs uv; install it with `pip install uv`")
    return found


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _key(root: Path) -> str:
    h = hashlib.sha256((root / PYPROJECT).read_bytes())
    h.update(f"\0{sys.version_info[:3]}\0{sys.platform}\0{sys.executable}\0".encode())
    for name, version in _base():
        h.update(f"{_norm(name)}=={version}\n".encode())
    return h.hexdigest()[:16]


# Index settings from the environment (a server's registry for every project).
# They apply when packages are resolved and installed, but are hidden from
# `uv add` / `uv remove`: given an index, uv add writes it into the project's
# pyproject.toml and ties each added package to it.
_INDEX_ENV = ("UV_INDEX", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL")


def _cache_env(cache: Path | None) -> dict[str, str]:
    """Where uv keeps its cache: ``UV_CACHE_DIR`` when the server sets it,
    else ``cache`` (a shared store's, on the server's volume), else uv's
    default, unless that is not writable (``docker run --user`` leaves HOME
    pointing at a folder the user can't write)."""
    if os.environ.get("UV_CACHE_DIR") or os.environ.get("UV_NO_CACHE"):
        return {}
    if cache is not None:
        return {"UV_CACHE_DIR": str(cache)}
    home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    probe = home if home.exists() else home.parent
    if not os.access(probe, os.W_OK):
        return {"UV_CACHE_DIR": str(Path(tempfile.gettempdir()) / "uv-cache")}
    return {}


def _uv_run(args: list[str], cwd: Path, edit: bool = False, cache: Path | None = None) -> str:
    # A build backend runs code from the packages being installed, so uv gets
    # none of exuvia's own settings; index credentials stay, uv needs them.
    env = {**node_environ(index=False), "NO_COLOR": "1", "UV_NO_PROGRESS": "1", **_cache_env(cache)}
    if edit:
        for name in _INDEX_ENV:
            env.pop(name, None)
    try:
        proc = subprocess.run(
            [_uv(), *args], cwd=cwd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=INSTALL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise PackagesError(f"uv took longer than {INSTALL_TIMEOUT_S}s (EXUVIA_PACKAGES_TIMEOUT)") from None
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode:
        raise PackagesError(out or f"uv exited with code {proc.returncode}")
    return out


def _write_base_files(work: Path) -> tuple[Path, Path, Path]:
    """Constraints (exuvia's exact versions), excludes (every package exuvia
    has) and a one-line excludes file for exuvia itself."""
    base = _base()
    constraints, excludes, only_exuvia = work / "base.txt", work / "excludes.txt", work / "exuvia.txt"
    constraints.write_text("".join(f"{n}=={v}\n" for n, v in base), encoding="utf-8")
    excludes.write_text("".join(f"{n}\n" for n, _ in base), encoding="utf-8")
    only_exuvia.write_text("exuvia\n", encoding="utf-8")
    return constraints, excludes, only_exuvia


def _compile(root: Path, work: Path, prefer: str | None = None, cache: Path | None = None) -> Path:
    """Resolve the project's packages against exuvia's own versions into a
    pinned requirements file, preferring the versions in ``prefer`` (an
    earlier resolution) where they still fit. A conflict fails here, with
    uv's explanation. exuvia itself is left out: a project may list it for
    its editor, and its version may not be on any index."""
    constraints, _, only_exuvia = _write_base_files(work)
    resolved = work / "resolved.txt"
    if prefer:
        resolved.write_text(prefer, encoding="utf-8")  # uv prefers what the output file pins
    try:
        _uv_compile(root, resolved, only_exuvia, constraints, cache=cache)
    except PackagesError as e:
        text = str(e)
        if not _names_a_base_pin(text):
            text = _hidden_base_conflict(root, work, only_exuvia, constraints, text, cache) or text
        if _names_a_base_pin(text):
            text += f"\n\n{CONFLICT_HINT}"  # one of exuvia's own versions is in the way
        raise PackagesError(text) from None
    return resolved


def _uv_compile(root: Path, out: Path, excludes: Path, *constraints: Path, cache: Path | None = None) -> None:
    # --emit-index-annotation: the index each package comes from (without
    # credentials) is part of what an install holds; see _install_key.
    args = ["pip", "compile", PYPROJECT, "--python", sys.executable, "--emit-index-annotation"]
    for c in constraints:
        args += ["-c", str(c)]
    _uv_run([*args, "--excludes", str(excludes), "-o", str(out), "--quiet"], cwd=root, cache=cache)


def _names_a_base_pin(text: str) -> bool:
    return any(f"{name}=={version}".lower() in text.lower() for name, version in _base())


def _hidden_base_conflict(
    root: Path, work: Path, excludes: Path, constraints: Path, error: str, cache: Path | None = None
) -> str | None:
    """uv's explanation of a conflict with exuvia's versions that a failed
    resolution (``error``) did not mention, or None if there is none.

    Held to exuvia's versions, uv backtracks from a package's newest release
    to older ones, and can stop at an error in one of those (a Git URL
    dependency, a build that fails) instead of reporting why the newer
    releases did not fit. So resolve again without exuvia's versions: if
    that works and needs other versions of exuvia's own packages, resolve
    with exuvia's versions once more, holding the project's packages to the
    releases that resolution chose, and report why that fails."""
    free = work / "free.txt"
    try:
        _uv_compile(root, free, excludes, cache=cache)
    except PackagesError:
        return None  # it does not resolve on its own either: the error stands
    wanted = _pins(free.read_text(encoding="utf-8"))
    base = {_norm(n): v for n, v in _base()}
    if not any(name in base and version != base[name] for name, version in wanted.items()):
        return None
    listed = {_requirement_name(d) for d in declared(root)}
    newest = {name: wanted[name] for name in sorted(n for n in listed if n and n in wanted and n not in base)}
    if not newest:
        return None
    held = work / "newest.txt"
    held.write_text("".join(f"{n}=={v}\n" for n, v in newest.items()), encoding="utf-8")
    try:
        _uv_compile(root, work / "held.txt", excludes, constraints, held, cache=cache)
    except PackagesError as e:
        if not _names_a_base_pin(str(e)):
            return None
        releases = ", ".join(f"{n} {v}" for n, v in newest.items())
        return (f"{e}\n\nThat is with the newest releases ({releases}), which need other versions of "
                f"packages exuvia runs with. uv went on to older releases and stopped at another error:\n\n{error}")
    return None


_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;#]+)")


def _pins(text: str) -> dict[str, str]:
    """``normalized name -> version`` of the ``name==version`` lines of a
    requirements file."""
    return {_norm(m.group(1)): m.group(2) for line in text.splitlines() if (m := _PIN.match(line.strip()))}


def resolve(root: Path, cache: Path | None = None) -> dict[str, str]:
    """``normalized name -> version`` of every package the project's list
    resolves to (exuvia's own included), without installing anything."""
    work = Path(tempfile.mkdtemp(prefix="exuvia-packages-"))
    try:
        with _SLOTS:
            text = _compile(root, work, cache=cache).read_text(encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return _pins(text)


# -- the store: installs named after what they hold ----------------------------------
_COMMENT = re.compile(r"\s+#.*$")


def _install_key(resolved: str) -> str:
    """The name of the install that holds a resolution: the packages it adds
    to exuvia's (a requirement line each, with the index it comes from), the
    Python and platform they were installed for, and NumPy's major version,
    which compiled extensions are built against."""
    base = {_norm(n): v for n, v in _base()}
    lines: list[str | None] = []  # None: one of exuvia's packages, not installed
    for raw in resolved.splitlines():
        stripped = raw.strip()
        if stripped.startswith("# from "):
            if lines and lines[-1] is not None:
                lines[-1] += f" from {stripped[7:]}"
            continue
        line = _COMMENT.sub("", stripped)
        if not line or line.startswith(("#", "-")):
            continue
        name = _requirement_name(line)
        lines.append(line if name and name not in base else None)
    h = hashlib.sha256()
    h.update(f"{sys.implementation.cache_tag}{getattr(sys, 'abiflags', '')}\0{sysconfig.get_platform()}\0".encode())
    h.update(f"numpy {base.get('numpy', '').split('.')[0]}\0".encode())
    for line in sorted(x for x in lines if x is not None):
        h.update(f"{line}\n".encode())
    return h.hexdigest()[:16]


def _ref_path(store: Path, root: Path) -> Path:
    return store / REFS / f"{hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]}.json"


def _found(store: Path, input_key: str) -> Path | None:
    """The finished install recorded for ``input_key``, without resolving."""
    try:
        name = (store / INPUTS / input_key).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    folder = store / name
    return folder if name and (folder / COMPLETE).exists() else None


def _use(store: Path, root: Path, folder: Path, input_key: str) -> bool:
    """Record that the project at ``root`` uses ``folder``, unless it was
    removed meanwhile. Under the store's lock, so an install found here is
    not removed by _collect before the record keeps it."""
    with _lock(f"store\0{store}"):
        if not (folder / COMPLETE).exists():
            return False
        if _found(store, input_key) != folder:
            _write(store / INPUTS / input_key, folder.name)
        ref = _ref_path(store, root)
        want = {"root": str(root.resolve()), "install": folder.name}
        try:
            if json.loads(ref.read_text(encoding="utf-8")) == want:
                return True
        except (OSError, ValueError):
            pass
        _write(ref, json.dumps(want))
    _collect(store)
    return True


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _forget(store: Path, root: Path) -> None:
    """The project at ``root`` uses no install (it lists no packages)."""
    ref = _ref_path(store, root)
    if ref.exists():
        with _lock(f"store\0{store}"):
            ref.unlink(missing_ok=True)
        _collect(store)


def _previous(store: Path, root: Path) -> str | None:
    """The resolution in the install the project used last, if it is still there."""
    try:
        name = json.loads(_ref_path(store, root).read_text(encoding="utf-8"))["install"]
        return (store / name / RESOLVED).read_text(encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _collect(store: Path) -> None:
    """Remove installs no project uses (made more than KEEP_S ago), the
    records of projects that are gone, and installs cut short."""
    now = time.time()
    with _lock(f"store\0{store}"):
        live: set[str] = set()
        for ref in (store / REFS).glob("*.json") if (store / REFS).is_dir() else []:
            try:
                data = json.loads(ref.read_text(encoding="utf-8"))
                root, name = Path(data["root"]), data["install"]
            except (OSError, ValueError, KeyError, TypeError):
                ref.unlink(missing_ok=True)
                continue
            if (root / PYPROJECT).is_file():
                live.add(name)
            else:
                ref.unlink(missing_ok=True)  # the project was deleted or moved
        for folder in store.iterdir() if store.is_dir() else []:
            if not folder.is_dir() or folder.name in live:
                continue
            try:
                age = now - folder.stat().st_mtime
                if folder.name.startswith(".incoming-"):
                    if age > INSTALL_TIMEOUT_S + KEEP_S:
                        shutil.rmtree(folder, ignore_errors=True)  # left by a process that died
                    continue
                if folder.name.startswith("."):
                    continue
                done = folder / COMPLETE
                if done.exists() and now - done.stat().st_mtime < KEEP_S:
                    continue  # just made: a project may be about to record it
            except OSError:
                continue
            shutil.rmtree(folder, ignore_errors=True)
        for record in (store / INPUTS).iterdir() if (store / INPUTS).is_dir() else []:
            if record.name.startswith("."):
                continue  # being written
            try:
                if not (store / record.read_text(encoding="utf-8").strip() / COMPLETE).exists():
                    record.unlink(missing_ok=True)
            except OSError:
                pass


def installed(root: Path, store: Path) -> Path | None:
    """The finished install of the project's current list, found without
    resolving (None when there is none yet, or the list is empty)."""
    if not declared(root):
        return None
    key = _key(root)
    folder = _found(store, key)
    if folder is not None and _use(store, root, folder, key):
        return folder
    return None


def ensure(root: Path, store: Path, log: Callable[[str], None], shared: bool = False) -> Path | None:
    """The folder holding the project's declared packages, installed first
    if needed (the installer's output goes to ``log``), or None when it
    declares none. ``store`` holds the installs; ``shared`` says that it is
    a server's, used by many projects, which also keeps uv's cache there."""
    deps = declared(root)
    if not deps:
        _forget(store, root)
        return None
    cache = store / UV_CACHE if shared else None
    key = _key(root)
    folder = installed(root, store)
    if folder is not None:
        return folder
    with _lock(f"input\0{store}\0{key}"):
        folder = installed(root, store)
        if folder is not None:
            return folder
        store.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="exuvia-packages-"))
        try:
            with _SLOTS:
                resolved = _compile(root, work, prefer=_previous(store, root), cache=cache)
            text = resolved.read_text(encoding="utf-8")
            target = store / _install_key(text)
            with _lock(f"install\0{target}"):
                if not (target / COMPLETE).exists():
                    log(f"installing the packages in {PYPROJECT}: {', '.join(deps)}\n")
                    _install(root, store, work, resolved, target, log, cache)
                if not _use(store, root, target, key):
                    raise PackagesError("the install was removed while it was being recorded; run again")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return target


def _install(root: Path, store: Path, work: Path, resolved: Path, target: Path,
             log: Callable[[str], None], cache: Path | None) -> None:
    """Install a resolution, minus what exuvia already has, as ``target``."""
    _, excludes, _ = _write_base_files(work)
    incoming = Path(tempfile.mkdtemp(dir=store, prefix=".incoming-"))
    try:
        with _SLOTS:
            out = _uv_run(["pip", "install", "-r", PYPROJECT, "--python", sys.executable, "--target",
                           str(incoming), "-c", str(resolved), "--excludes", str(excludes)], cwd=root, cache=cache)
        if out:
            log(out + "\n")
        shutil.copyfile(resolved, incoming / RESOLVED)
        (incoming / COMPLETE).write_text("", encoding="utf-8")
        if target.exists() and not (target / COMPLETE).exists():
            shutil.rmtree(target, ignore_errors=True)  # an install that was cut short
        try:
            os.replace(incoming, target)
        except OSError:
            if not (target / COMPLETE).exists():  # not another process finishing first
                raise
    finally:
        shutil.rmtree(incoming, ignore_errors=True)


def installed_in(folder: Path | None) -> dict[str, tuple[str, str]]:
    """``normalized name -> (name, version)`` of the packages in an install folder."""
    if folder is None or not folder.is_dir():
        return {}
    out: dict[str, tuple[str, str]] = {}
    for dist in metadata.distributions(path=[str(folder)]):
        name = dist.metadata["Name"]
        if name:
            out[_norm(name)] = (name, dist.version)
    return out


# -- missing modules ------------------------------------------------------------
# Import names that differ from the name of the package that provides them.
# Anything else is assumed to be published under its import name.
MODULE_TO_PACKAGE = {
    "attr": "attrs", "bs4": "beautifulsoup4", "cv2": "opencv-python-headless", "Crypto": "pycryptodome",
    "dateutil": "python-dateutil", "docx": "python-docx", "dotenv": "python-dotenv", "fitz": "pymupdf",
    "google.cloud.bigquery": "google-cloud-bigquery", "google.cloud.storage": "google-cloud-storage",
    "jwt": "pyjwt", "Levenshtein": "levenshtein", "magic": "python-magic", "MySQLdb": "mysqlclient",
    "OpenSSL": "pyopenssl", "PIL": "pillow", "pptx": "python-pptx", "psycopg2": "psycopg2-binary",
    "serial": "pyserial", "skimage": "scikit-image", "sklearn": "scikit-learn", "slugify": "python-slugify",
    "snowflake.connector": "snowflake-connector-python", "statsmodels.api": "statsmodels",
    "websocket": "websocket-client", "yaml": "pyyaml", "zmq": "pyzmq",
}
_NO_MODULE = re.compile(r"No module named '([A-Za-z0-9_.]+)'")


def missing_package(error: str | None) -> dict[str, str] | None:
    """For a run that failed on an import of something not installed, the
    module and the package that most likely provides it."""
    m = _NO_MODULE.search(error or "")
    if not m:
        return None
    module = m.group(1)
    parts = module.split(".")
    for i in range(len(parts), 0, -1):  # google.cloud.bigquery before google
        if ".".join(parts[:i]) in MODULE_TO_PACKAGE:
            return {"module": module, "package": MODULE_TO_PACKAGE[".".join(parts[:i])]}
    if parts[0] in ("exuvia_nodes", "lib"):
        return None  # the project's own code, not a package
    return {"module": module, "package": parts[0].replace("_", "-").lower()}


# -- a project's packages, live -----------------------------------------------------
PYPROJECT_TEMPLATE = """\
[project]
name = "{name}"
version = "0"
requires-python = ">=3.11"
# Packages the nodes import beyond those exuvia comes with. `uv add scipy`
# in this folder adds one; exuvia installs them before the next run.
dependencies = []
"""
# What the built-in agent and MCP clients may do with packages:
# ask (default): their request waits for a person to approve it in the workbench;
# install: they add packages themselves; off: they cannot ask.
AGENT_MODES = ("ask", "install", "off")
_BARE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*$")
_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\s*(\[[^\]]*\])?")


def agent_mode() -> str:
    mode = os.environ.get("EXUVIA_AGENT_PACKAGES", "ask").strip().lower()
    return mode if mode in AGENT_MODES else "ask"


def dist_name(folder: str) -> str:
    """A valid project name for pyproject.toml from a folder name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", folder).strip("-._").lower() or "exuvia-project"


def _requirement_name(spec: str) -> str | None:
    m = _REQUIREMENT.match(spec.strip())
    return _norm(m.group(0).split("[")[0].strip()) if m else None


class Packages:
    """One project's packages as the workbench sees them: their install
    state, edits to ``pyproject.toml`` (through ``uv add`` / ``uv remove``, so
    uv writes the TOML), installs started as soon as the list changes rather
    than when a node next runs, and the requests the agent makes for a
    person to approve.

    ``local`` is false when nodes run on a remote executor: it installs the
    packages itself, so this side edits and checks the list but installs
    nothing. ``store`` is a server's store shared by its projects (a hub's);
    without one, the project keeps its installs in ``.exuvia/packages``.
    """

    def __init__(self, root: Path, publish: Callable[[dict], None] | None = None, local: bool = True,
                 store: Path | None = None):
        self.root = root
        self.shared = store is not None
        self.store = store if store is not None else root / ".exuvia" / "packages"
        self.uv_cache = self.store / UV_CACHE if self.shared else None
        self.local = local
        self._publish = publish or (lambda event: None)
        self._edit = threading.Lock()
        self._state_lock = threading.Lock()
        self._installing = False
        self._failed: dict[str, str] = {}  # key -> uv's output, until the list changes or a retry
        self._log = ""
        self._requests: list[dict] = []
        self._next_request = 1
        self._thread: threading.Thread | None = None
        # Called after the list is edited through this object (Workspace sets
        # it to reload, which records pyproject.toml as a code revision).
        self.on_edit: Callable[[], None] | None = None

    # -- installing ------------------------------------------------------------
    def _current(self) -> tuple[list[str], str | None]:
        deps = declared(self.root)
        return deps, (_key(self.root) if deps else None)

    def ensure_for_run(self, log: Callable[[str], None]) -> Path | None:
        """What a run calls before its node starts (see LocalExecutor). Waits
        for an install in progress; fails at once with the earlier error when
        the same list already failed to install."""
        deps, key = self._current()
        if key is None:
            return None
        if key in self._failed:
            raise PackagesError(self._failed[key])
        folder = installed(self.root, self.store)
        if folder is not None:
            return folder
        return self._install(key, log)

    def _install(self, key: str, log: Callable[[str], None] | None = None) -> Path | None:
        lines: list[str] = []

        def tee(text: str) -> None:
            lines.append(text)
            with self._state_lock:
                self._log = "".join(lines)
            if log:
                log(text)

        with self._state_lock:
            self._installing = True
        self._changed()
        try:
            folder = ensure(self.root, self.store, tee, shared=self.shared)
        except PackagesError as e:
            with self._state_lock:
                self._failed[key] = str(e)
                self._log = "".join(lines) + str(e) + "\n"
            raise
        finally:
            with self._state_lock:
                self._installing = False
            self._changed()
        return folder

    def prebuild(self) -> None:
        """Install the current list in the background unless it is installed,
        installing, or failed already. Called when the project loads and
        after every change to the list."""
        if not self.local:
            return
        legacy = self.root / ".exuvia" / "packages"
        if self.shared and legacy.is_dir():
            # Installs from before the project used the server's store.
            threading.Thread(target=shutil.rmtree, args=(legacy, True), daemon=True).start()
        try:
            deps, key = self._current()
        except PackagesError:
            return  # state() reports the broken file
        if key is None or key in self._failed or installed(self.root, self.store) is not None:
            return
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return  # it re-checks the list when it is done
            self._thread = threading.Thread(target=self._prebuild_loop, daemon=True, name="exuvia-packages")
            self._thread.start()

    def _prebuild_loop(self) -> None:
        while True:
            try:
                deps, key = self._current()
            except PackagesError:
                break
            if key is None or key in self._failed or installed(self.root, self.store) is not None:
                break
            try:
                self._install(key)
            except PackagesError:
                pass  # recorded in the state; the list may have changed meanwhile
            except Exception as e:  # noqa: BLE001 — a background thread must not die silently
                with self._state_lock:
                    self._failed[key] = f"{type(e).__name__}: {e}"
                break
        self._changed()

    def retry(self) -> dict:
        """Forget the failure of the current list and install it again."""
        with self._state_lock:
            self._failed.clear()
        self.prebuild()
        return self.state()

    # -- editing the list --------------------------------------------------------
    def _edit_list(self, args: list[str], pin: list[str] | None = None) -> None:
        """Run ``uv add`` / ``uv remove`` (``args``) on pyproject.toml, check
        that the result resolves against exuvia's versions, and pin the
        packages named in ``pin`` that were added without a version to the
        version that resolved. On any failure the file is put back as it was."""
        path = self.root / PYPROJECT
        with self._edit:
            before = path.read_bytes() if path.exists() else None
            if before is None:
                path.write_text(PYPROJECT_TEMPLATE.format(name=dist_name(self.root.name)), encoding="utf-8")
            try:
                _uv_run([*args, "--frozen"], cwd=self.root, edit=True, cache=self.uv_cache)
                versions = resolve(self.root, cache=self.uv_cache)
                base = {_norm(n) for n, _ in _base()}
                exact = [
                    f"{m.group(1)}{m.group(2) or ''}=={versions[_norm(m.group(1))]}"
                    for spec in pin or []
                    if (m := _BARE.match(spec)) and _norm(m.group(1)) in versions and _norm(m.group(1)) not in base
                ]
                if exact:
                    _uv_run(["add", *exact, "--frozen"], cwd=self.root, edit=True, cache=self.uv_cache)
            except BaseException:
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(before)
                raise
        self._edited()

    def _edited(self) -> None:
        if self.on_edit:
            self.on_edit()
        self.prebuild()
        self._changed()

    @staticmethod
    def _check_specs(specs: list[str]) -> list[str]:
        out = [s.strip() for s in specs if isinstance(s, str) and s.strip()]
        if not out:
            raise PackagesError("name at least one package")
        for s in out:
            if s.startswith("-") or _requirement_name(s) is None:
                raise PackagesError(f"not a package requirement: {s!r} (e.g. scipy, scipy>=1.14, acme-models==2.3)")
        return out

    def add(self, specs: list[str]) -> dict:
        """Add requirements such as ``scipy`` or ``acme-models>=2``. One given
        without a version is pinned to the exact version it resolves to."""
        specs = self._check_specs(specs)
        self._edit_list(["add", *specs], pin=specs)
        return self.state()

    def remove(self, names: list[str]) -> dict:
        names = self._check_specs(names)
        listed = {_requirement_name(d) for d in declared(self.root)}
        unknown = [n for n in names if _requirement_name(n) not in listed]
        if unknown:
            raise PackagesError(f"not in {PYPROJECT}: {', '.join(unknown)}")
        self._edit_list(["remove", *[_requirement_name(n) or n for n in names]])
        return self.state()

    def write_manifest(self, text: str) -> dict:
        """Replace pyproject.toml as a whole (an editor in the workbench)."""
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise PackagesError(f"{PYPROJECT} does not parse: {e}") from None
        if not isinstance((data.get("project") or {}).get("dependencies", []), list):
            raise PackagesError(f"[project] dependencies in {PYPROJECT} must be a list of strings")
        with self._edit:
            (self.root / PYPROJECT).write_text(text, encoding="utf-8")
        self._edited()
        return self.state()

    # -- requests from the agent ---------------------------------------------------
    def request(self, specs: list[str], reason: str = "", origin: str = "agent") -> dict:
        """The agent asks for packages. In ``ask`` mode the request waits for a
        person to approve it; in ``install`` mode it is added right away."""
        specs = self._check_specs(specs)
        mode = agent_mode()
        if mode == "off":
            raise PackagesError("installing packages from the agent is turned off on this server (EXUVIA_AGENT_PACKAGES=off)")
        if mode == "install":
            self.add(specs)
            return {"status": "added", "packages": specs}
        import time

        with self._state_lock:
            req = {"id": self._next_request, "packages": specs, "reason": reason.strip()[:500],
                   "origin": origin, "at": time.time()}
            self._next_request += 1
            self._requests.append(req)
        self._changed()
        return {"status": "requested", **req}

    def _pop_request(self, request_id: int) -> dict:
        with self._state_lock:
            for i, req in enumerate(self._requests):
                if req["id"] == request_id:
                    return self._requests.pop(i)
        raise KeyError(request_id)

    def approve(self, request_id: int) -> dict:
        req = self._pop_request(request_id)
        try:
            return self.add(req["packages"])
        except PackagesError:
            with self._state_lock:
                self._requests.insert(0, req)  # still waiting: the person can edit and retry
            self._changed()
            raise

    def dismiss(self, request_id: int) -> dict:
        self._pop_request(request_id)
        self._changed()
        return self.state()

    # -- state --------------------------------------------------------------------------
    def _changed(self) -> None:
        self._publish({"type": "packages"})

    def environment(self) -> dict[str, str]:
        """Every package a node can import, ``name -> version``: exuvia's own
        environment, then the project's install (when it is ready) on top."""
        by_norm = {_norm(n): (n, v) for n, v in _base()}
        try:
            _, key = self._current()
        except PackagesError:
            key = None
        folder = _found(self.store, key) if key else None
        by_norm.update(installed_in(folder))
        return {n: v for n, v in sorted(by_norm.values(), key=lambda nv: _norm(nv[0]))}

    def state(self) -> dict:
        path = self.root / PYPROJECT
        manifest = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        error = None
        try:
            deps, key = self._current()
        except PackagesError as e:
            deps, key, error = [], None, str(e)
        folder = _found(self.store, key) if key else None
        ready = folder is not None
        with self._state_lock:
            installing, log, requests = self._installing, self._log, list(self._requests)
            failed = self._failed.get(key) if key else None
        if error:
            status = "failed"
        elif not deps:
            status = "none"
        elif not self.local:
            status = "remote"
        elif ready:
            status = "ready"
        elif installing:
            status = "installing"
        elif failed:
            status, error = "failed", failed
        else:
            status = "pending"
        here = installed_in(folder) if ready else {}
        base = {_norm(n): v for n, v in _base()}
        packages = []
        for spec in deps:
            name = _requirement_name(spec) or spec
            if name in here:
                version, source = here[name][1], "project"
            elif name in base:
                version, source = base[name], "exuvia"
            else:
                version, source = None, None
            packages.append({"requirement": spec, "name": name, "version": version, "source": source})
        return {
            "status": status,
            "error": error,
            "packages": packages,
            "installed": len(here),
            "log": log,
            "requests": requests,
            "agent": agent_mode(),
            "manifest": manifest,
        }
