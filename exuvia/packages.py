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
  uv's explanation instead of importing a mix of the two.
* Packages exuvia already has are excluded from the install, so the folder
  holds only what the project adds.

uv runs in the project folder, so its own settings there apply, among them
private package indexes (``[[tool.uv.index]]``) and where each package comes
from (``[tool.uv.sources]``). Index credentials come from the environment
(``UV_INDEX_<NAME>_USERNAME`` / ``_PASSWORD``), which a project's ``.env`` can
set. ``uv.lock`` is not read.

The install folder is named after a hash of the ``pyproject.toml`` and of
exuvia's environment, and is reused until either changes.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Callable

PYPROJECT = "pyproject.toml"
COMPLETE = ".complete"
CONFLICT_HINT = "hint: the exact (==) versions are the ones exuvia itself runs with; a project's packages must work with them"
INSTALL_TIMEOUT_S = int(os.environ.get("EXUVIA_PACKAGES_TIMEOUT", "900"))

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


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


def _uv_run(args: list[str], cwd: Path) -> str:
    env = {**os.environ, "NO_COLOR": "1", "UV_NO_PROGRESS": "1"}
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


def ensure(root: Path, cache: Path, log: Callable[[str], None], prune: bool = False) -> Path | None:
    """The folder holding the project's declared packages, installed first
    if needed (the installer's output goes to ``log``), or None when it
    declares none. ``prune`` removes other installs from ``cache`` (a
    project's own cache, not one shared by many projects)."""
    deps = declared(root)
    if not deps:
        return None
    key = _key(root)
    target = cache / key
    if (target / COMPLETE).exists():
        return target
    with _lock(key):
        if (target / COMPLETE).exists():
            return target
        cache.mkdir(parents=True, exist_ok=True)
        log(f"installing the packages in {PYPROJECT}: {', '.join(deps)}\n")
        base = _base()
        work = Path(tempfile.mkdtemp(prefix="exuvia-packages-"))
        incoming = Path(tempfile.mkdtemp(dir=cache, prefix=".incoming-"))
        try:
            constraints, excludes, resolved = work / "base.txt", work / "excludes.txt", work / "resolved.txt"
            constraints.write_text("".join(f"{n}=={v}\n" for n, v in base), encoding="utf-8")
            excludes.write_text("".join(f"{n}\n" for n, _ in base), encoding="utf-8")
            only_exuvia = work / "exuvia.txt"
            only_exuvia.write_text("exuvia\n", encoding="utf-8")
            py = ["--python", sys.executable]
            # 1. Resolve against exuvia's own versions: a conflict fails here,
            #    with uv's explanation. exuvia itself is left out (a project may
            #    list it for its editor; its version may not be on any index).
            try:
                _uv_run(["pip", "compile", PYPROJECT, *py, "-c", str(constraints), "--excludes", str(only_exuvia),
                         "-o", str(resolved), "--quiet"], cwd=root)
            except PackagesError as e:
                raise PackagesError(f"{e}\n\n{CONFLICT_HINT}") from None
            # 2. Install that resolution minus what exuvia already has.
            out = _uv_run(["pip", "install", "-r", PYPROJECT, *py, "--target", str(incoming),
                           "-c", str(resolved), "--excludes", str(excludes)], cwd=root)
            if out:
                log(out + "\n")
            (incoming / COMPLETE).write_text("", encoding="utf-8")
            if target.exists() and not (target / COMPLETE).exists():
                shutil.rmtree(target, ignore_errors=True)  # an install that was cut short
            try:
                os.replace(incoming, target)
            except OSError:
                if not (target / COMPLETE).exists():  # not another process finishing first
                    raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(incoming, ignore_errors=True)
        if prune:
            for old in cache.iterdir():
                if old.name != key and not old.name.startswith("."):
                    shutil.rmtree(old, ignore_errors=True)
        return target
