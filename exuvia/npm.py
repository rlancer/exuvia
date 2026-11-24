"""npm packages a project's views import, listed in its ``package.json``.

A project lists the npm packages its views import beyond the ones the views
runtime ships (react, react-dom, apache-arrow, vega-embed and exuvia) in the
standard place, ``dependencies`` in a ``package.json`` at the project root::

    {"dependencies": {"recharts": "2.15.0", "date-fns": "^4.1.0"}}

Nothing is installed and nothing runs on the server: a view still runs in a
frame that cannot reach the network. The server fetches each package the
view imports as browser-ready ES modules from an esm.sh CDN (``EXUVIA_NPM_CDN``,
https://esm.sh by default), bundled with its own dependencies and with the
runtime's packages left as bare imports, so a package and the view share one
React. The page posts those modules into the frame with the view's files,
and the frame's loader compiles them like the view's own (ui/src/views/loader.ts).

Fetched modules are kept in a cache (a project's ``.exuvia/npm``, or one
folder shared by a hub's projects). A module whose address names an exact
version is immutable and never fetched again; anything else (a range such as
``^4.1.0``) is fetched again after an hour, and the cached copy is used when
the CDN cannot be reached.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

PACKAGE_JSON = "package.json"
CDN = os.environ.get("EXUVIA_NPM_CDN", "https://esm.sh").rstrip("/")
# Packages the views runtime ships (BUILTINS in ui/src/views/runtime.tsx). A
# view always gets these, and a package that imports one gets the same copy.
RUNTIME = ("react", "react-dom", "apache-arrow", "vega-embed", "exuvia")
_EXTERNAL = ",".join(p for p in RUNTIME if p != "exuvia")
# esm.sh: bundle the package's own dependencies into it, leave the runtime's
# packages as bare imports, and build for a current browser.
QUERY = f"bundle&external={_EXTERNAL}&target=es2022"
TIMEOUT_S = 30
REFRESH_S = 3600  # a module named by a version range is fetched again after this
MAX_MODULES = 300  # per package a view imports
MAX_BYTES = 40 * 1024 * 1024
# Subpaths fetched as files rather than as modules (`leaflet/dist/leaflet.css`).
ASSETS = (".css",)

_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")
_RANGE = re.compile(r"^[0-9A-Za-z.^~*<>=|+ -]+$")
_EXACT = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?$")
# The version esm.sh resolved a request to: `/* esm.sh - recharts@2.15.0 */`.
_STUB = re.compile(r"^/\* esm\.sh - ((?:@[^/@\s]+/)?[^@\s]+)@([^\s/*]+)")
# Imports in a view's own source, and in the modules esm.sh serves.
_IMPORT = re.compile(r"""(?:\bfrom|\bimport|\brequire)\s*\(?\s*["']([^"'\s]+)["']""")
# What an import a module makes may name: a path on the CDN.
_PATH = re.compile(r"""^/[^\s"'<>(){};\\`]*$""")
# A path or query that names an exact version: its content never changes.
_IMMUTABLE = re.compile(r"@\d+\.\d+\.\d+(?:[/?]|$)")


class NpmError(RuntimeError):
    pass


def package_of(spec: str) -> str:
    """The package a bare import names: ``recharts`` for ``recharts``,
    ``lodash-es`` for ``lodash-es/debounce``, ``@tanstack/react-table``."""
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


def bare_imports(sources: dict[str, str]) -> set[str]:
    """The bare specifiers (``recharts``, ``leaflet/dist/leaflet.css``) the
    code files among ``sources`` (path -> text) import."""
    out: set[str] = set()
    for path, text in sources.items():
        if not path.endswith((".tsx", ".ts", ".jsx", ".js")):
            continue
        for spec in _IMPORT.findall(text):
            if not spec.startswith((".", "/")) and ":" not in spec:
                out.add(spec)
    return out


def split_spec(spec: str) -> tuple[str, str | None]:
    """``recharts@^2`` -> (``recharts``, ``^2``); ``@scope/x`` -> (``@scope/x``, None)."""
    spec = spec.strip()
    at = spec.find("@", 1)
    return (spec, None) if at < 0 else (spec[:at], spec[at + 1:].strip() or None)


def check_name(name: str) -> str:
    if not _NAME.match(name):
        raise NpmError(f"not an npm package name: {name!r} (e.g. recharts, @tanstack/react-table)")
    if name in RUNTIME:
        raise NpmError(f"{name} comes with exuvia's views runtime: views import it without adding it")
    return name


def check_range(version: str) -> str:
    if not _RANGE.match(version) or version.strip() != version:
        raise NpmError(
            f"not a version or range esm.sh can serve: {version!r} (e.g. 2.15.0, ^2.15.0, ~4.1, latest); "
            "git, file and URL dependencies are not supported"
        )
    return version


def declared(root: Path) -> dict[str, str]:
    """``dependencies`` of the project's ``package.json``; empty without one."""
    path = root / PACKAGE_JSON
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise NpmError(f"{PACKAGE_JSON} does not parse: {e}") from None
    deps = data.get("dependencies") if isinstance(data, dict) else None
    if deps is None:
        return {}
    if not isinstance(deps, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in deps.items()):
        raise NpmError(f"dependencies in {PACKAGE_JSON} must be an object of package name to version")
    return dict(deps)


def _fetch(url: str) -> tuple[str, dict[str, str]]:
    """GET ``url``: its text and headers. Raises NpmError with what the CDN said."""
    req = urllib.request.Request(url, headers={"User-Agent": "exuvia", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.read().decode("utf-8", errors="replace"), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace").strip()[:500]
        raise NpmError(f"{url}: {e.code} {body or e.reason}") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise NpmError(f"could not reach {CDN}: {reason}") from None


def resolve_module(base: str, spec: str) -> str | None:
    """The CDN path an import in the module at ``base`` names, or None for a
    bare import (one of the runtime's packages, left for the frame)."""
    if spec.startswith(CDN + "/"):
        spec = spec[len(CDN):]
    elif spec.startswith(("./", "../")):
        spec = urllib.parse.urljoin(f"{CDN}{base}", spec)[len(CDN):]
    return spec if spec.startswith("/") and _PATH.match(spec) and len(spec) < 1000 else None


class Npm:
    """One project's view packages: the list in ``package.json``, edits to it,
    and the modules a view imports, fetched through the cache in ``store``."""

    def __init__(self, root: Path, store: Path | None = None, fetch: Callable[[str], tuple[str, dict[str, str]]] | None = None):
        self.root = root
        self.store = store if store is not None else root / ".exuvia" / "npm"
        self._fetch = fetch or _fetch
        self._edit = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Called after the list is edited through this object (Workspace sets
        # it to reload, which records package.json as a code revision).
        self.on_edit: Callable[[], None] | None = None

    # -- the cache ---------------------------------------------------------------
    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, path: str) -> str:
        """The text of ``path`` on the CDN (``/recharts@2.15.0?bundle...``),
        from the cache when it is there and still good."""
        url = CDN + path
        file = self.store / hashlib.sha256(url.encode()).hexdigest()
        with self._lock(url):
            try:
                age = time.time() - file.stat().st_mtime
                if _IMMUTABLE.search(path) or age < REFRESH_S:
                    return file.read_text(encoding="utf-8")
            except OSError:
                pass
            try:
                text, _ = self._fetch(url)
            except NpmError:
                if file.is_file():
                    return file.read_text(encoding="utf-8")  # the CDN is down: what we had
                raise
            self.store.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.store, prefix=".incoming-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, file)
            return text

    def resolved(self, name: str, version: str) -> str:
        """The exact version ``name@version`` resolves to on the CDN (which
        checks the package exists)."""
        path = f"/{name}@{urllib.parse.quote(version, safe='')}?{QUERY}"
        m = _STUB.match(self.get(path))
        if not m:
            raise NpmError(f"{name}@{version}: {CDN} did not answer with a module")
        return m.group(2)

    # -- what a view gets --------------------------------------------------------
    def modules(self, specs: set[str]) -> dict[str, Any]:
        """For the bare imports ``specs`` of a view: ``entries`` (import ->
        module id), ``modules`` (module id -> source, every module those
        entries import on the CDN), ``errors`` (import -> why it could not be
        fetched) and ``declared`` (the packages ``package.json`` lists). An
        import of a package not listed is left out, for the frame to report."""
        deps = declared(self.root)
        out: dict[str, Any] = {"declared": sorted(deps), "entries": {}, "modules": {}, "errors": {}}
        for spec in sorted(specs):
            name = package_of(spec)
            if name in RUNTIME or name not in deps:
                continue
            sub = spec[len(name):]
            base = f"/{name}@{urllib.parse.quote(deps[name], safe='')}{sub}"
            entry = base if sub.endswith(ASSETS) else f"{base}?{QUERY}"
            found: dict[str, str] = {}
            try:
                self._walk(entry, found)
            except NpmError as e:
                out["errors"][spec] = f"{spec} could not be loaded from {CDN}: {e}"
                continue
            out["entries"][spec] = entry
            out["modules"].update(found)
        return out

    def _walk(self, entry: str, found: dict[str, str]) -> None:
        todo, size = [entry], 0
        while todo:
            path = todo.pop()
            if path in found:
                continue
            if len(found) >= MAX_MODULES:
                raise NpmError(f"it is made of more than {MAX_MODULES} modules")
            text = self.get(path)
            size += len(text)
            if size > MAX_BYTES:
                raise NpmError(f"it is larger than {MAX_BYTES // (1024 * 1024)} MB")
            found[path] = text
            if path.endswith(ASSETS):
                continue
            for spec in _IMPORT.findall(text):
                dep = resolve_module(path, spec)
                if dep is not None and dep not in found:
                    todo.append(dep)

    # -- editing the list ----------------------------------------------------------
    def _write(self, deps: dict[str, str]) -> None:
        path = self.root / PACKAGE_JSON
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                data = loaded if isinstance(loaded, dict) else {}
            except (ValueError, UnicodeDecodeError):
                data = {}
        if not data:
            data = {"private": True}
        data["dependencies"] = dict(sorted(deps.items()))
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _edited(self) -> None:
        if self.on_edit:
            self.on_edit()

    def add(self, specs: list[str]) -> dict[str, Any]:
        """Add packages such as ``recharts``, ``recharts@^2`` or
        ``@tanstack/react-table@8.21.3``. Each is checked on the CDN first;
        one given without a version is pinned to the exact version it
        resolves to, so the project shows the same thing tomorrow."""
        wanted = [s.strip() for s in specs if isinstance(s, str) and s.strip()]
        if not wanted:
            raise NpmError("name at least one package")
        pinned: dict[str, str] = {}
        for spec in wanted:
            name, version = split_spec(spec)
            check_name(name)
            exact = self.resolved(name, check_range(version or "latest"))
            pinned[name] = version if version and version != "latest" else exact
        with self._edit:
            deps = declared(self.root)
            deps.update(pinned)
            self._write(deps)
        self._edited()
        return self.state()

    def remove(self, names: list[str]) -> dict[str, Any]:
        wanted = [split_spec(n)[0] for n in names if isinstance(n, str) and n.strip()]
        if not wanted:
            raise NpmError("name at least one package")
        with self._edit:
            deps = declared(self.root)
            unknown = [n for n in wanted if n not in deps]
            if unknown:
                raise NpmError(f"not in {PACKAGE_JSON}: {', '.join(unknown)}")
            for n in wanted:
                deps.pop(n)
            self._write(deps)
        self._edited()
        return self.state()

    # -- state ---------------------------------------------------------------------
    def signature(self) -> str:
        """Changes when the list does: part of each view's hash, so a view
        rebuilds when a package it may import is added or changed."""
        try:
            deps = declared(self.root)
        except NpmError as e:
            return hashlib.sha256(str(e).encode()).hexdigest()[:12]
        return hashlib.sha256(json.dumps(deps, sort_keys=True).encode()).hexdigest()[:12] if deps else ""

    def state(self) -> dict[str, Any]:
        error = None
        try:
            deps = declared(self.root)
        except NpmError as e:
            deps, error = {}, str(e)
        return {
            "packages": [{"name": n, "version": v, "exact": bool(_EXACT.match(v))} for n, v in sorted(deps.items())],
            "error": error,
            "builtins": list(RUNTIME),
            "cdn": CDN,
        }
