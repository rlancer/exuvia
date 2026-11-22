"""Execute a single node in an isolated subprocess.

    python -m exuvia.runner <spec.json>
    python -m exuvia.runner --warm        # pre-warmed, reads spec path on stdin

A warm runner reads a second line after the spec path: the project's secrets
as a JSON object, set in ``os.environ`` before the node is loaded. They come
over the pipe so they are never written to a file (see project_secrets.py).

The parent (``workspace.py``) streams this process's stdout/stderr as the run
log and reads the result from ``spec["result_path"]``. Running each node in
its own process gives crash isolation, clean log capture, killable timeouts,
and means a node can never corrupt the server's memory. Each process runs
exactly one node, so no module state leaks between runs; the warm mode only
moves the (often multi-second) pandas/duckdb import off the critical path.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any


def load_artifact(path: str | None, kind: str | None) -> Any:
    if not path or kind in (None, "none"):
        return None
    if kind == "table":
        import pandas as pd

        return pd.read_parquet(path)
    if kind == "json":
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if kind == "pickle":
        # Older exuvia pickled outputs it could not store as JSON. Loading a
        # pickle runs code, so they are never read; the workspace reports such
        # an output as stale, which re-runs its node before anything reads it.
        raise ValueError("this upstream output was saved with pickle, which exuvia no longer reads; re-run it")
    raise ValueError(f"unknown artifact kind {kind!r}")


# Table outputs use the current Parquet format, not pyarrow's
# compatibility defaults (v1 data pages, Snappy, no page index): every reader
# exuvia feeds (pyarrow, pandas, DuckDB) handles all of it.
PARQUET_OPTIONS: dict[str, Any] = {
    "version": "2.6",  # newest format version: nanosecond timestamps, all logical types
    "data_page_version": "2.0",
    "compression": "zstd",
    "write_page_index": True,  # column/offset indexes let readers skip pages
    "write_page_checksum": True,  # outputs travel to and from object storage
    "store_decimal_as_integer": True,  # spec-preferred encoding for small decimals
}


class UnsupportedOutput(TypeError):
    pass


def _json_default(obj: Any) -> Any:
    """JSON for the non-JSON types data code commonly returns; anything else
    is an error rather than a silent ``str()`` (or a pickle)."""
    import datetime as dt
    import decimal
    import uuid

    if type(obj).__module__ == "numpy" and hasattr(obj, "tolist"):
        return obj.tolist()  # numpy scalars and arrays
    if type(obj).__name__ in ("NAType", "NaTType"):
        return None  # pandas missing values
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()  # also pandas Timestamps
    if isinstance(obj, dt.timedelta):
        return obj.total_seconds()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=repr)
    if isinstance(obj, (Path, uuid.UUID)):
        return str(obj)
    raise UnsupportedOutput(type(obj).__name__)


def _to_json(value: Any) -> str:
    try:
        return json.dumps(value, default=_json_default)
    except (UnsupportedOutput, TypeError, ValueError) as e:
        raise TypeError(
            "A node must return a DataFrame (stored as Parquet), JSON-compatible data "
            "(dict with string keys, list, str, number, bool), or None. "
            f"This one returned {type(value).__name__}, which contains something JSON cannot hold: {e}"
        ) from None


def _table_schema(df: Any) -> list[dict[str, str]]:
    return [{"name": str(c), "type": str(t)} for c, t in df.dtypes.items()]


def save_artifact(value: Any, out_dir: Path) -> dict[str, Any]:
    """Serialize a node's return value; artifact id = content hash."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp)
    meta: dict[str, Any] = {"rows": None, "schema": None}
    try:
        import pandas as pd

        if value is None:
            tmp_path.unlink()
            return {"kind": "none", "artifact_id": "none", "artifact_path": None, **meta}
        if hasattr(value, "to_pandas") and not isinstance(value, pd.DataFrame):
            value = value.to_pandas()  # polars / pyarrow tables
        if isinstance(value, pd.Series):
            value = value.to_frame()
        if isinstance(value, pd.DataFrame):
            df = value
            if not isinstance(df.index, pd.RangeIndex) or df.index.name is not None:
                df = df.reset_index()
            df.columns = [str(c) for c in df.columns]
            df.to_parquet(tmp_path, index=False, engine="pyarrow", **PARQUET_OPTIONS)
            kind, ext = "table", "parquet"
            meta = {"rows": int(len(df)), "schema": _table_schema(df)}
        else:
            tmp_path.write_text(_to_json(value), encoding="utf-8")
            kind, ext = "json", "json"
            meta["schema"] = [{"name": "value", "type": type(value).__name__}]
        digest = hashlib.sha256(tmp_path.read_bytes()).hexdigest()[:16]
        final = out_dir / f"{digest}.{ext}"
        if final.exists():
            tmp_path.unlink()
        else:
            tmp_path.replace(final)
        return {"kind": kind, "artifact_id": digest, "artifact_path": str(final), **meta}
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


class _FromSource(importlib.machinery.SourceFileLoader):
    """Compiles a project module from its source on every import, never from
    ``__pycache__``. Python trusts a cached .pyc whose source has the same
    size and modification time (in whole seconds), so a node or helper edited
    twice within a second to the same size would otherwise run the old code.
    Project files are small, so compiling them each run costs little, and
    nothing is written into the project's folders."""

    def get_code(self, fullname: str) -> Any:
        return self.source_to_code(self.get_data(self.path), self.path)


_SKIP_PARTS = {"site-packages", "dist-packages", "node_modules", "__pycache__"}


def _import_project_from_source(root: Path) -> None:
    """Route imports from the project's folders (``lib/`` helpers and the
    like) through ``_FromSource``. Hidden folders and installed packages
    inside the project, such as a ``.venv``, keep their normal bytecode cache."""
    root = root.resolve()

    def ours(entry: str) -> bool:
        try:
            p = Path(entry or ".").resolve()
        except (OSError, ValueError):
            return False
        if not p.is_relative_to(root):
            return False
        return not any(part.startswith(".") or part in _SKIP_PARTS for part in p.relative_to(root).parts)

    finder = importlib.machinery.FileFinder.path_hook(
        (importlib.machinery.ExtensionFileLoader, importlib.machinery.EXTENSION_SUFFIXES),
        (_FromSource, importlib.machinery.SOURCE_SUFFIXES),
        (importlib.machinery.SourcelessFileLoader, importlib.machinery.BYTECODE_SUFFIXES),
    )

    def hook(entry: str) -> Any:
        if not ours(entry):
            raise ImportError("not a project folder")
        return finder(entry)

    sys.path_hooks.insert(0, hook)
    for entry in [e for e in sys.path_importer_cache if ours(e)]:
        del sys.path_importer_cache[entry]  # found before the hook (the warm imports scan the cwd)


def _load_python_node(root: Path, path: Path, name: str):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))  # lets nodes import shared helpers from the project
    _import_project_from_source(root)
    mod = f"exuvia_nodes.{name}"
    spec = importlib.util.spec_from_file_location(mod, path, loader=_FromSource(mod, str(path)))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for value in vars(module).values():
        if callable(value) and hasattr(value, "__exuvia__") and getattr(value, "__module__", None) == module.__name__:
            return value
    raise RuntimeError("No function decorated with @exuvia.node found in module.")


def execute(spec: dict[str, Any]) -> dict[str, Any]:
    import re

    import exuvia

    root = Path(spec["root"])
    os.chdir(root)
    if spec.get("site"):
        # The packages the project declares in its pyproject.toml (see
        # packages.py). Appended, so exuvia's own packages, already imported
        # by a warm runner, stay the ones in use; addsitedir also reads the
        # .pth files some packages install.
        import site

        site.addsitedir(spec["site"])
    params = spec.get("params") or {}
    exuvia._runtime = {"node": spec["node"], "params": params}  # what exuvia.param() reads
    inputs = {dep: load_artifact(i.get("path"), i.get("kind")) for dep, i in spec["inputs"].items()}
    if spec["kind"] == "python":
        fn = _load_python_node(root, root / spec["path"], spec["node"])
        value = fn(**inputs)
    else:
        import duckdb
        import pandas as pd

        con = duckdb.connect()
        for dep, val in inputs.items():
            if isinstance(val, pd.DataFrame):
                con.register(dep, val)
        sql = (root / spec["path"]).read_text(encoding="utf-8")
        # Declared parameters are bound as DuckDB named parameters: `$as_of`.
        # DuckDB rejects values the statement does not use, so bind only those.
        used = {name: params[name] for name in params if re.search(rf"\${name}\b", sql)}
        value = con.execute(sql, used).df() if used else con.execute(sql).df()
    return save_artifact(value, Path(spec["out_dir"]))


WARM_IMPORTS = ("pandas", "pyarrow", "duckdb", "numpy")


def main() -> None:
    """``runner <spec.json>`` runs immediately. ``runner --warm`` pre-imports
    the heavy data libraries, then blocks until the parent writes a spec path
    to stdin, runs that one node, and exits — a single-use, pre-warmed worker.
    """
    if sys.argv[1] == "--warm":
        for mod in WARM_IMPORTS:
            try:
                importlib.import_module(mod)
            except ImportError:
                pass
        line = sys.stdin.readline().strip()
        if not line:  # parent shut down the pool
            return
        spec_path = line
        secrets = sys.stdin.readline().strip()
        if secrets:
            os.environ.update(json.loads(secrets))
    else:
        spec_path = sys.argv[1]
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    try:
        result = {"ok": True, **execute(spec)}
    except BaseException as e:  # noqa: BLE001 — report everything to the parent
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        result = {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": tb}
    Path(spec["result_path"]).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
