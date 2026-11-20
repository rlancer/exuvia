"""Static discovery of a exuvia project: node files -> typed graph.

Nothing here executes user code. Python nodes are parsed with ``ast``; SQL
nodes read a small ``-- key: value`` header and resolve dependencies by
matching referenced identifiers against the other node names.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Any, Callable

NODE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
NODE_SUFFIXES = {".py": "python", ".sql": "sql"}
PARAM_TYPES = ("date", "string", "number", "boolean")

# Shared helper modules: plain Python under ``lib/`` that nodes import
# (``from lib.stats import weighted_corr``). They are the only project files
# besides nodes that the API, the agent and outside tools may write.
HELPER_DIR = "lib"
HELPER_MAX_BYTES = 512 * 1024
_MODULE_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Views: custom UI over node outputs, one folder per view under ``views/``
# with a ``view.json`` manifest and source files. A view only reads the
# outputs of the nodes its manifest lists; nothing in it ever runs as compute.
VIEW_DIR = "views"
VIEW_MANIFEST = "view.json"
VIEW_RUNTIMES = ("react",)
VIEW_DEFAULT_ENTRY = "App.tsx"
# The files a view is made of: text the browser-side runtime compiles or serves.
VIEW_SUFFIXES = (".tsx", ".ts", ".jsx", ".js", ".css", ".json", ".html", ".md", ".svg")
VIEW_ENTRY_SUFFIXES = (".tsx", ".ts", ".jsx", ".js")
VIEW_FILE_MAX_BYTES = 512 * 1024
VIEW_MANIFEST_KEYS = ("title", "description", "runtime", "entry", "reads")
_VIEW_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")  # no leading dot: no `..`, no hidden files
_VIEW_SKIP = {"node_modules", "__pycache__"}

PYTHON_TEMPLATE = '''"""{description}"""
import exuvia


@exuvia.node()
def {name}({params}):
    import pandas as pd

    return pd.DataFrame()
'''

SQL_TEMPLATE = """-- {description}
-- schedule:
select 1 as x
"""


@dataclass
class Node:
    name: str
    kind: str  # "python" | "sql"
    path: str  # relative to project root
    deps: list[str] = field(default_factory=list)
    description: str = ""
    schedule: str | None = None
    cascade: bool = True
    chart: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)  # run parameters this node reads
    schedule_source: str | None = None  # "code" | "config" (exuvia.json `schedules`)
    helpers: list[str] = field(default_factory=list)  # lib/ files it imports, directly or not
    code_hash: str = ""  # the node file plus every helper it imports
    parse_error: str | None = None
    line: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class View:
    """A view folder as read from disk. ``files`` are paths relative to the
    folder; ``hash`` covers every one of them (name and content)."""

    name: str
    path: str  # views/<name>, relative to the project root
    title: str = ""
    description: str = ""
    runtime: str = "react"
    entry: str = VIEW_DEFAULT_ENTRY
    reads: list[str] = field(default_factory=list)  # nodes whose outputs it reads
    files: list[str] = field(default_factory=list)
    hash: str = ""
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Graph:
    nodes: dict[str, Node]
    views: dict[str, View] = field(default_factory=dict)

    def readers(self, name: str) -> list[str]:
        """Views that read node ``name``'s output."""
        return sorted(v.name for v in self.views.values() if name in v.reads)

    def upstream(self, name: str) -> list[str]:
        return [d for d in self.nodes[name].deps if d in self.nodes]

    def downstream(self, name: str) -> list[str]:
        return [n.name for n in self.nodes.values() if name in n.deps]

    def ancestors(self, name: str) -> set[str]:
        out: set[str] = set()
        stack = list(self.upstream(name))
        while stack:
            cur = stack.pop()
            if cur not in out:
                out.add(cur)
                stack.extend(self.upstream(cur))
        return out

    def descendants(self, name: str) -> set[str]:
        out: set[str] = set()
        stack = list(self.downstream(name))
        while stack:
            cur = stack.pop()
            if cur not in out:
                out.add(cur)
                stack.extend(self.downstream(cur))
        return out

    def effective_params(self, name: str) -> list[str]:
        """Parameters that can change this node's output: its own plus every
        ancestor's. A node's outputs and history are kept per value of these."""
        names = set(self.nodes[name].params)
        for a in self.ancestors(name):
            names.update(self.nodes[a].params)
        return sorted(names)

    def topo_order(self, subset: set[str] | None = None) -> list[str]:
        """Kahn's algorithm. Raises ValueError on a cycle."""
        names = set(self.nodes) if subset is None else set(subset)
        indeg = {n: sum(1 for d in self.upstream(n) if d in names) for n in names}
        ready = sorted(n for n, k in indeg.items() if k == 0)
        order: list[str] = []
        while ready:
            cur = ready.pop(0)
            order.append(cur)
            for child in sorted(self.downstream(cur)):
                if child in indeg:
                    indeg[child] -= 1
                    if indeg[child] == 0:
                        ready.append(child)
        if len(order) != len(names):
            cyclic = sorted(names - set(order))
            raise ValueError(f"cycle between: {', '.join(cyclic)}")
        return order

    def cycle_members(self) -> set[str]:
        try:
            self.topo_order()
            return set()
        except ValueError:
            return {n for n in self.nodes if n in self.ancestors(n)}


class Project:
    """A exuvia project rooted at a directory containing ``nodes/``."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.nodes_dir = self.root / "nodes"
        self.lib_dir = self.root / HELPER_DIR
        self.views_dir = self.root / VIEW_DIR
        self.state_dir = self.root / ".exuvia"
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        # Both exist so the file watcher can watch them.
        self.lib_dir.mkdir(parents=True, exist_ok=True)
        self.views_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config = self._load_config()

    @property
    def name(self) -> str:
        return self.config.get("name") or self.root.name

    def _load_config(self) -> dict[str, Any]:
        cfg = self.root / "exuvia.json"
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    def update_config(self, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Read-modify-write ``exuvia.json``: the settings the UI and agent can
        change (name, timezone, params, schedule overrides)."""
        cfg = self._load_config()
        mutate(cfg)
        text = json.dumps(cfg, indent=2) + "\n"
        (self.root / "exuvia.json").write_text(text, encoding="utf-8", newline="\n")
        self.config = cfg
        return cfg

    # -- settings --------------------------------------------------------
    @property
    def tz(self) -> tzinfo | None:
        """Timezone for cron schedules and "today" (``exuvia.json`` ``timezone``,
        an IANA name). None means the host's local time."""
        name = self.config.get("timezone")
        if not name:
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError):
            return None

    def today(self) -> str:
        return datetime.now(self.tz).date().isoformat()

    @property
    def params(self) -> dict[str, dict[str, Any]]:
        """Declared run parameters: ``{name: {type, default, description}}``."""
        out: dict[str, dict[str, Any]] = {}
        raw = self.config.get("params")
        if not isinstance(raw, dict):
            return out
        for name, spec in raw.items():
            if not NODE_NAME.match(str(name)):
                continue
            spec = spec if isinstance(spec, dict) else {"default": spec}
            ptype = spec.get("type") if spec.get("type") in PARAM_TYPES else "string"
            out[name] = {
                "type": ptype,
                "default": spec.get("default"),
                "description": str(spec.get("description") or ""),
            }
        return out

    def resolve_params(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """A concrete value for every declared parameter. A ``date`` whose value
        is ``"today"`` becomes today's date in the project timezone, so every
        run is pinned to a calendar day and can be replayed later."""
        specs = self.params
        overrides = overrides or {}
        unknown = sorted(set(overrides) - set(specs))
        if unknown:
            raise ValueError(f"unknown parameter(s): {', '.join(unknown)}")
        out: dict[str, Any] = {}
        for name, spec in specs.items():
            value = overrides.get(name)
            if value is None or value == "":
                value = spec["default"]
            out[name] = coerce_param(name, spec["type"], value, self.today)
        return out

    def instructions(self) -> str:
        """Project-specific notes for the agent: ``exuvia.json`` ``instructions``
        and/or an ``AGENTS.md`` next to it (data sources, conventions, helpers)."""
        parts = []
        text = self.config.get("instructions")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
        notes = self.root / "AGENTS.md"
        if notes.is_file():
            parts.append(notes.read_text(encoding="utf-8").strip())
        return "\n\n".join(p for p in parts if p)

    # -- discovery -------------------------------------------------------
    def node_files(self) -> list[Path]:
        return sorted(
            p
            for p in self.nodes_dir.iterdir()
            if p.is_file() and p.suffix in NODE_SUFFIXES and not p.name.startswith("_")
        )

    def load(self) -> Graph:
        self.config = self._load_config()  # picks up hand edits to exuvia.json
        raw: dict[str, Node] = {}
        helper_cache: dict[Path, tuple[str, set[str]]] = {}
        for path in self.node_files():
            n = parse_node(path, self.root)
            if n.kind == "python":
                # A helper edit changes what the node computes, so the node's
                # code hash covers every lib/ file it imports.
                text = path.read_text(encoding="utf-8")
                n.helpers = self.helper_deps(text, helper_cache)
                if n.helpers:
                    n.code_hash = _hash(
                        text + "".join(f"\0{rel}\0{helper_cache[self.root / rel][0]}" for rel in n.helpers)
                    )
            raw[n.name] = n
        # SQL deps can only be resolved once every node name is known.
        names = set(raw)
        for n in raw.values():
            if n.kind == "sql" and n.parse_error is None:
                n.deps = sql_deps(self.source(n.name), names - {n.name})
        graph = Graph(raw)
        for name in graph.cycle_members():
            graph.nodes[name].parse_error = "Dependency cycle: this node (indirectly) depends on itself."
        declared = self.params
        overrides = self.config.get("schedules")
        for n in graph.nodes.values():
            missing = [d for d in n.deps if d not in names]
            if missing and n.parse_error is None:
                n.parse_error = f"Unknown upstream node(s): {', '.join(missing)}"
            unknown = [p for p in n.params if p not in declared]
            if unknown and n.parse_error is None:
                n.parse_error = (
                    f"Unknown parameter(s): {', '.join(unknown)}. Declare them under `params` in exuvia.json."
                )
            if n.schedule:
                n.schedule_source = "code"
            # A schedule set from the UI lives in exuvia.json and wins over the
            # one in code; an empty string turns a code schedule off.
            if isinstance(overrides, dict) and n.name in overrides:
                n.schedule = str(overrides[n.name] or "").strip() or None
                n.schedule_source = "config"
                if n.schedule and n.parse_error is None:
                    try:
                        _validate_cron(n.schedule)
                    except ValueError as e:
                        n.parse_error = str(e)
        graph.views = {v.name: v for v in (self.parse_view(name, names) for name in self.view_names())}
        return graph

    def path_for(self, name: str) -> Path | None:
        for suffix in NODE_SUFFIXES:
            p = self.nodes_dir / f"{name}{suffix}"
            if p.exists():
                return p
        return None

    def source(self, name: str) -> str:
        p = self.path_for(name)
        if p is None:
            raise KeyError(name)
        return p.read_text(encoding="utf-8")

    # -- mutation (UI + agent) ------------------------------------------
    def write_node(self, name: str, source: str, kind: str | None = None) -> Path:
        if not NODE_NAME.match(name):
            raise ValueError("Node names must be snake_case identifiers (a-z, 0-9, _).")
        existing = self.path_for(name)
        if kind is None:
            kind = NODE_SUFFIXES[existing.suffix] if existing else "python"
        suffix = ".py" if kind == "python" else ".sql"
        target = self.nodes_dir / f"{name}{suffix}"
        if existing and existing != target:
            existing.unlink()  # kind changed: python <-> sql
        target.write_text(source, encoding="utf-8", newline="\n")
        return target

    def delete_node(self, name: str) -> None:
        p = self.path_for(name)
        if p is None:
            raise KeyError(name)
        p.unlink()

    # -- helper files (lib/**/*.py) ----------------------------------------
    def helper_path(self, rel: str) -> Path:
        """The file for a helper path such as ``lib/stats.py`` or
        ``lib/vol/surface.py``. Raises ValueError for anything that is not an
        importable ``.py`` module path under ``lib/`` (traversal, absolute
        paths, other extensions, node files, symlinks out of ``lib/``)."""
        parts = rel.split("/") if isinstance(rel, str) else []
        if len(parts) < 2 or parts[0] != HELPER_DIR or len(parts) > 8:
            raise ValueError(f"helper files live under {HELPER_DIR}/, e.g. {HELPER_DIR}/stats.py")
        *dirs, filename = parts[1:]
        stem, dot, ext = filename.rpartition(".")
        if not dot or ext != "py" or not all(_MODULE_PART.match(p) for p in [*dirs, stem]):
            raise ValueError(
                f"helper paths are importable Python modules: {HELPER_DIR}/<name>.py or "
                f"{HELPER_DIR}/<package>/<name>.py (letters, digits and _ only)"
            )
        path = self.root.joinpath(*parts)
        if not path.resolve().is_relative_to(self.lib_dir):
            raise ValueError(f"{rel} resolves outside {HELPER_DIR}/")
        return path

    def helper_files(self) -> list[str]:
        """Every helper module, as project-relative POSIX paths."""
        out = []
        for p in sorted(self.lib_dir.rglob("*.py")) if self.lib_dir.is_dir() else []:
            rel = p.relative_to(self.root).as_posix()
            try:
                self.helper_path(rel)
            except ValueError:
                continue
            if p.is_file():
                out.append(rel)
        return out

    def read_helper(self, rel: str) -> str:
        p = self.helper_path(rel)
        if not p.is_file():
            raise KeyError(rel)
        return p.read_text(encoding="utf-8")

    def write_helper(self, rel: str, source: str) -> Path:
        p = self.helper_path(rel)
        if len(source.encode("utf-8")) > HELPER_MAX_BYTES:
            raise ValueError(f"helper files are at most {HELPER_MAX_BYTES // 1024} KB")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source, encoding="utf-8", newline="\n")
        return p

    def delete_helper(self, rel: str) -> None:
        p = self.helper_path(rel)
        if not p.is_file():
            raise KeyError(rel)
        p.unlink()
        parent = p.parent  # drop package directories the delete left empty
        while parent != self.lib_dir and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    # -- views (views/<name>/...) ---------------------------------------------
    def view_names(self) -> list[str]:
        """Every folder under ``views/``. A badly named one is listed too (its
        view carries the error), the way a badly named node file is."""
        if not self.views_dir.is_dir():
            return []
        return sorted(
            p.name
            for p in self.views_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name not in _VIEW_SKIP
        )

    def view_dir(self, name: str) -> Path:
        if not isinstance(name, str) or not NODE_NAME.match(name):
            raise ValueError("View names must be snake_case identifiers (a-z, 0-9, _).")
        return self.views_dir / name

    def view_file_path(self, name: str, rel: str) -> Path:
        """The file for ``rel`` inside view ``name`` (``App.tsx``,
        ``components/Chart.tsx``). Raises ValueError for a bad view name and
        for anything but a plain relative path with an allowed extension:
        traversal, absolute paths, hidden files, symlinks out of the folder."""
        return _view_file(self.view_dir(name), rel)

    def view_files(self, name: str) -> list[str]:
        """The files view ``name`` is made of, relative to its folder. Files
        with other extensions or unsafe paths are not part of the view."""
        base = self.views_dir / name
        out: list[str] = []
        if not base.is_dir():
            return out
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if _VIEW_PART.match(d) and d not in _VIEW_SKIP]
            for f in filenames:
                rel = (Path(dirpath) / f).relative_to(base).as_posix()
                try:
                    _view_file(base, rel)
                except ValueError:
                    continue
                out.append(rel)
        return sorted(out)

    def all_view_files(self) -> list[tuple[str, str]]:
        """``(view name, project-relative path)`` for every file of every view."""
        return [(name, f"{VIEW_DIR}/{name}/{rel}") for name in self.view_names() for rel in self.view_files(name)]

    def parse_view(self, name: str, nodes: set[str]) -> View:
        """Read one view folder statically. A missing or broken manifest, a
        missing entry file or a read of a node that does not exist is an error
        on the view; the view is listed all the same."""
        base = self.views_dir / name
        view = View(name=name, path=f"{VIEW_DIR}/{name}", title=name, files=self.view_files(name))
        h = hashlib.sha256()
        for rel in view.files:
            try:
                data = (base / rel).read_bytes()
            except OSError:
                continue  # removed mid-scan; the next reload sees it gone
            h.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
        view.hash = h.hexdigest()[:16]
        errors: list[str] = []
        if not NODE_NAME.match(name):
            errors.append("Folder name must be a snake_case identifier.")
        manifest = base / VIEW_MANIFEST
        raw: Any = None
        if not manifest.is_file():
            errors.append(f"Missing {VIEW_MANIFEST}.")
        else:
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                errors.append(f"{VIEW_MANIFEST} is not valid JSON: {e}")
        if raw is not None:
            clean, problems = check_view_manifest(raw)
            errors += problems
            view.title = clean["title"] or name
            view.description = clean["description"]
            view.runtime = clean["runtime"]
            view.entry = clean["entry"]
            view.reads = clean["reads"]
        if not errors and view.entry not in view.files:
            errors.append(f"Entry file {view.entry} does not exist.")
        unknown = [r for r in view.reads if r not in nodes]
        if unknown:
            errors.append(f"Reads unknown node(s): {', '.join(unknown)}.")
        view.parse_error = " ".join(errors) or None
        return view

    def read_view_file(self, name: str, rel: str) -> str:
        p = self.view_file_path(name, rel)
        if not p.is_file():
            raise KeyError(rel)
        return p.read_text(encoding="utf-8")

    def write_view_file(self, name: str, rel: str, source: str) -> Path:
        p = self.view_file_path(name, rel)
        if len(source.encode("utf-8")) > VIEW_FILE_MAX_BYTES:
            raise ValueError(f"view files are at most {VIEW_FILE_MAX_BYTES // 1024} KB")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source, encoding="utf-8", newline="\n")
        return p

    def delete_view_file(self, name: str, rel: str) -> None:
        p = self.view_file_path(name, rel)
        if not p.is_file():
            raise KeyError(rel)
        p.unlink()
        base, parent = self.view_dir(name), p.parent  # drop directories the delete left empty
        while parent != base and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    def write_view_manifest(self, name: str, manifest: dict[str, Any]) -> Path:
        """Create or replace ``views/<name>/view.json``. Raises ValueError when
        the manifest is malformed; reads of nodes that do not exist yet are
        allowed (the view reports them until the nodes exist)."""
        base = self.view_dir(name)
        clean, problems = check_view_manifest(manifest)
        if problems:
            raise ValueError(" ".join(problems))
        clean["title"] = clean["title"] or name
        base.mkdir(parents=True, exist_ok=True)
        p = base / VIEW_MANIFEST
        p.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8", newline="\n")
        return p

    def delete_view(self, name: str) -> None:
        base = self.view_dir(name)
        if not base.is_dir() or base.is_symlink():
            raise KeyError(name)
        shutil.rmtree(base)

    def helper_deps(self, source: str, cache: dict[Path, tuple[str, set[str]]] | None = None) -> list[str]:
        """The ``lib/`` files a Python module imports, directly or through other
        helpers. Only static ``import`` / ``from … import`` statements count;
        ``cache`` maps a helper file to its text and imported module names."""
        cache = {} if cache is None else cache
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        found: set[Path] = set()
        queue = [p for m in _imported_modules(tree, None) for p in self._module_files(m)]
        while queue:
            path = queue.pop()
            if path in found:
                continue
            found.add(path)
            if path not in cache:
                text = path.read_text(encoding="utf-8", errors="replace")
                package = ".".join(path.parent.relative_to(self.root).parts)
                try:
                    cache[path] = (text, _imported_modules(ast.parse(text), package))
                except SyntaxError:
                    cache[path] = (text, set())
            queue.extend(p for m in cache[path][1] for p in self._module_files(m))
        return sorted(p.relative_to(self.root).as_posix() for p in found)

    def _module_files(self, module: str) -> list[Path]:
        """Files Python runs to import ``module`` from ``lib/``: each package's
        ``__init__.py`` on the way, then the module itself."""
        parts = module.split(".")
        if parts[0] != HELPER_DIR or not all(_MODULE_PART.match(p) for p in parts):
            return []
        out = []
        for i in range(1, len(parts) + 1):
            base = self.root.joinpath(*parts[:i])
            if (base / "__init__.py").is_file():
                out.append(base / "__init__.py")
            elif i == len(parts) and base.with_name(base.name + ".py").is_file():
                out.append(base.with_name(base.name + ".py"))
        safe = []
        for p in out:
            try:
                self.helper_path(p.relative_to(self.root).as_posix())
            except ValueError:
                continue
            safe.append(p)
        return safe

    def template(self, name: str, kind: str, deps: list[str], description: str) -> str:
        if kind == "sql":
            src = SQL_TEMPLATE.format(description=description or name)
            if deps:
                src = src.replace("select 1 as x", f"select * from {deps[0]}")
            return src
        return PYTHON_TEMPLATE.format(
            name=name, params=", ".join(deps), description=description or name
        )


# -- parsing --------------------------------------------------------------
def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_node(path: Path, root: Path) -> Node:
    kind = NODE_SUFFIXES[path.suffix]
    text = path.read_text(encoding="utf-8")
    node = Node(
        name=path.stem,
        kind=kind,
        path=path.relative_to(root).as_posix(),
        code_hash=_hash(text),
    )
    if not NODE_NAME.match(node.name):
        node.parse_error = "File name must be a snake_case identifier."
        return node
    try:
        if kind == "python":
            _parse_python(node, text)
        else:
            _parse_sql(node, text)
    except SyntaxError as e:
        node.parse_error = f"SyntaxError line {e.lineno}: {e.msg}"
        node.line = e.lineno or 1
    except ValueError as e:
        node.parse_error = str(e)
    return node


def _is_node_decorator(dec: ast.expr) -> ast.Call | bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    is_node = (isinstance(target, ast.Attribute) and target.attr == "node") or (
        isinstance(target, ast.Name) and target.id == "node"
    )
    if not is_node:
        return False
    return dec if isinstance(dec, ast.Call) else True


def _parse_python(node: Node, text: str) -> None:
    tree = ast.parse(text)
    fn: ast.FunctionDef | None = None
    call: ast.Call | bool = False
    for item in tree.body:
        if isinstance(item, ast.FunctionDef):
            for dec in item.decorator_list:
                found = _is_node_decorator(dec)
                if found:
                    fn, call = item, found
                    break
        if fn:
            break
    if fn is None:
        raise ValueError("No function decorated with @exuvia.node found.")
    node.line = fn.lineno
    node.deps = [a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs]
    node.description = (ast.get_docstring(fn) or ast.get_docstring(tree) or "").strip()
    if isinstance(call, ast.Call):
        for kw in call.keywords:
            if kw.arg is None:
                continue
            try:
                value = ast.literal_eval(kw.value)
            except ValueError as e:
                raise ValueError(f"@exuvia.node({kw.arg}=...) must be a literal value.") from e
            if kw.arg in {"schedule", "cascade", "chart", "tags"}:
                setattr(node, kw.arg, value)
            elif kw.arg == "params":
                node.params = _param_names(value)
    if node.schedule:
        _validate_cron(node.schedule)


def _imported_modules(tree: ast.AST, package: str | None) -> set[str]:
    """Absolute module names a module's import statements may load, anywhere
    in it (imports inside functions too). ``package`` resolves relative imports;
    None skips them (node files are not part of a package)."""
    out: set[str] = set()
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            out.update(alias.name for alias in item.names)
        elif isinstance(item, ast.ImportFrom):
            if item.level:
                base = package.split(".") if package else []
                if item.level > len(base):
                    continue
                prefix = ".".join(base[: len(base) - item.level + 1])
                module = f"{prefix}.{item.module}" if item.module else prefix
            else:
                module = item.module or ""
            if not module:
                continue
            out.add(module)
            # `from lib import stats` may name a submodule rather than an attribute.
            out.update(f"{module}.{alias.name}" for alias in item.names if alias.name != "*")
    return out


_SQL_HEADER = re.compile(r"^\s*--\s*([a-z_]+)\s*:\s*(.*?)\s*$")


def _parse_sql(node: Node, text: str) -> None:
    desc: list[str] = []
    for line in text.splitlines():
        if not line.strip().startswith("--"):
            if line.strip():
                break
            continue
        m = _SQL_HEADER.match(line)
        if m:
            key, val = m.group(1), m.group(2)
            if key == "schedule":
                node.schedule = val or None
            elif key == "cascade":
                node.cascade = val.lower() not in {"false", "no", "0"}
            elif key == "chart" and val:
                node.chart = json.loads(val)
            elif key == "tags" and val:
                node.tags = [t.strip() for t in val.split(",") if t.strip()]
            elif key == "params":
                node.params = _param_names([t.strip() for t in val.split(",") if t.strip()])
            else:
                desc.append(line.strip()[2:].strip())
        else:
            desc.append(line.strip()[2:].strip())
    node.description = " ".join(d for d in desc if d)
    if node.schedule:
        _validate_cron(node.schedule)


_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_SQL_STRING = re.compile(r"'(?:[^']|'')*'")
_IDENT = re.compile(r'"([^"]+)"|\b([A-Za-z_][A-Za-z0-9_]*)\b')


def sql_deps(text: str, names: set[str]) -> list[str]:
    body = _SQL_STRING.sub("''", _SQL_COMMENT.sub(" ", text))
    found: list[str] = []
    for m in _IDENT.finditer(body):
        ident = (m.group(1) or m.group(2) or "").lower()
        if ident in names and ident not in found:
            found.append(ident)
    return found


def _view_parts(rel: str) -> list[str]:
    """The parts of a view file path; ValueError unless it is a plain relative
    path with an allowed extension."""
    parts = rel.split("/") if isinstance(rel, str) else []
    if not parts or len(parts) > 8 or not all(_VIEW_PART.match(p) and p not in _VIEW_SKIP for p in parts):
        raise ValueError(
            "view file paths are relative to the view folder, e.g. App.tsx or components/Chart.tsx "
            "(letters, digits, `_`, `-` and `.`; no `..`, no leading `/`)"
        )
    if not parts[-1].endswith(VIEW_SUFFIXES):
        raise ValueError(f"view files are {', '.join(VIEW_SUFFIXES)}")
    return parts


def _view_file(base: Path, rel: str) -> Path:
    path = base.joinpath(*_view_parts(rel))
    if not path.resolve().is_relative_to(base):
        raise ValueError(f"{rel} resolves outside {base.parent.name}/{base.name}/")
    return path


def check_view_manifest(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """A view manifest with defaults filled in, and what is wrong with it.
    Malformed fields fall back to their defaults so a broken view can still
    be listed. ``title`` is empty when not given (the view name stands in)."""
    clean: dict[str, Any] = {
        "title": "",
        "description": "",
        "runtime": VIEW_RUNTIMES[0],
        "entry": VIEW_DEFAULT_ENTRY,
        "reads": [],
    }
    if not isinstance(raw, dict):
        return clean, [f"{VIEW_MANIFEST} must be a JSON object."]
    problems: list[str] = []
    unknown = sorted(str(k) for k in raw if k not in VIEW_MANIFEST_KEYS)
    if unknown:
        problems.append(f"Unknown key(s) in {VIEW_MANIFEST}: {', '.join(unknown)} (allowed: {', '.join(VIEW_MANIFEST_KEYS)}).")
    for key in ("title", "description"):
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            clean[key] = value.strip()
        else:
            problems.append(f"`{key}` must be a string.")
    runtime = raw.get("runtime")
    if runtime is not None:
        if runtime in VIEW_RUNTIMES:
            clean["runtime"] = runtime
        else:
            problems.append(f"Unknown runtime {runtime!r}; available: {', '.join(VIEW_RUNTIMES)}.")
    entry = raw.get("entry")
    if entry is not None:
        try:
            if not isinstance(entry, str) or not entry.endswith(VIEW_ENTRY_SUFFIXES):
                raise ValueError
            _view_parts(entry)
            clean["entry"] = entry
        except ValueError:
            problems.append(
                f"`entry` must be a file path in the view ending in {', '.join(VIEW_ENTRY_SUFFIXES)}, e.g. App.tsx."
            )
    reads = raw.get("reads")
    if reads is not None:
        if isinstance(reads, list) and all(isinstance(r, str) and NODE_NAME.match(r) for r in reads):
            clean["reads"] = list(dict.fromkeys(reads))
        else:
            problems.append('`reads` must be a list of node names, e.g. ["prices", "rolling_vol"].')
    return clean, problems


def _validate_cron(expr: str) -> None:
    from croniter import croniter

    if not croniter.is_valid(expr):
        raise ValueError(f"Invalid cron schedule: {expr!r}")


def _param_names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and NODE_NAME.match(v) for v in value):
        raise ValueError('`params` must be a list of parameter names, e.g. params=["as_of"].')
    return list(dict.fromkeys(value))


def coerce_param(name: str, ptype: str, value: Any, today: Callable[[], str]) -> Any:
    """Validate one parameter value and normalize it to its JSON form."""
    if value is None:
        return None
    try:
        if ptype == "date":
            if isinstance(value, str) and value.strip().lower() == "today":
                return today()
            if isinstance(value, datetime):
                return value.date().isoformat()
            if isinstance(value, date):
                return value.isoformat()
            return date.fromisoformat(str(value).strip()).isoformat()
        if ptype == "number":
            if isinstance(value, bool):
                raise ValueError("not a number")
            num = float(value)
            return int(num) if num.is_integer() else num
        if ptype == "boolean":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"true", "1", "yes"}:
                return True
            if text in {"false", "0", "no"}:
                return False
            raise ValueError("not a boolean")
        return str(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"parameter `{name}` expects a {ptype}, got {value!r}") from e
