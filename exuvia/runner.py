"""Execute a single node in an isolated subprocess.

    python -m exuvia.runner <spec.json>
    python -m exuvia.runner --warm        # pre-warmed, reads spec path on stdin

The parent (``workspace.py``) streams this process's stdout/stderr as the run
log and reads the result from ``spec["result_path"]``. Running each node in
its own process gives crash isolation, clean log capture, killable timeouts,
and means a node can never corrupt the server's memory. Each process runs
exactly one node, so no module state leaks between runs; the warm mode only
moves the (often multi-second) pandas/duckdb import off the critical path.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path
from typing import Any


def load_artifact(path: str | None, kind: str | None, paths: list[str] | None = None) -> Any:
    if paths is not None and kind == "table":  # a partitioned output: its chunk files
        if not paths:
            import pandas as pd

            return pd.DataFrame()
        return read_files(paths).df()
    if not path or kind in (None, "none"):
        return None
    if kind == "table":
        import pandas as pd

        return pd.read_parquet(path)
    if kind == "json":
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if kind == "multi":
        return read_parts(path)
    if kind == "pickle":
        # Older exuvia pickled outputs it could not store as JSON. Loading a
        # pickle runs code, so they are never read; the workspace reports such
        # an output as stale, which re-runs its node before anything reads it.
        raise ValueError("this upstream output was saved with pickle, which exuvia no longer reads; re-run it")
    raise ValueError(f"unknown artifact kind {kind!r}")


def read_files(paths: list[str], con: Any = None) -> Any:
    """A partitioned output's chunk files (at least one) as one DuckDB relation."""
    import duckdb

    con = con or duckdb.connect()
    return con.read_parquet(list(paths), union_by_name=True)


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


# A node that returns several named outputs (a dict whose values include a
# DataFrame, or a dataclass / pydantic model with a DataFrame field) stores
# them as one zip of ``<part>.parquet`` / ``<part>.json`` members, kind
# ``multi``. One file per run keeps everything that moves outputs around
# (the remote executor, the artifact store, hub exports) unchanged. Members
# are stored uncompressed (Parquet is already compressed) with a fixed
# timestamp, so the same outputs make the same bytes and the same digest.
PART_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
PART_EXT = {"table": "parquet", "json": "json"}
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def part_members(path: str | Path) -> list[tuple[str, str]]:
    """``(part, kind)`` for each member of a multi-output zip, in order."""
    with zipfile.ZipFile(path) as z:
        return [_member_part(m) for m in z.namelist()]


def _member_part(member: str) -> tuple[str, str]:
    stem, _, ext = member.rpartition(".")
    kind = {v: k for k, v in PART_EXT.items()}.get(ext)
    if not kind or not PART_NAME.match(stem):
        raise ValueError(f"unexpected member {member!r} in a multi-output artifact")
    return stem, kind


def read_parts(path: str | Path) -> dict[str, Any]:
    """A multi-output artifact as ``{part: DataFrame | JSON value}``."""
    import io

    out: dict[str, Any] = {}
    with zipfile.ZipFile(path) as z:
        for member in z.namelist():
            part, kind = _member_part(member)
            data = z.read(member)
            if kind == "table":
                import pandas as pd

                out[part] = pd.read_parquet(io.BytesIO(data))
            else:
                out[part] = json.loads(data)
    return out


def _as_frame(value: Any) -> Any:
    """``value`` as a pandas DataFrame when it is table-like (pandas, polars,
    pyarrow), else None."""
    import pandas as pd

    if isinstance(value, pd.DataFrame):
        return value
    if isinstance(value, pd.Series):
        return value.to_frame()
    if hasattr(value, "to_pandas") and not isinstance(value, type):
        out = value.to_pandas()  # polars / pyarrow tables
        return out.to_frame() if isinstance(out, pd.Series) else out
    return None


def _is_dataclass_instance(value: Any) -> bool:
    return dataclasses.is_dataclass(value) and not isinstance(value, type)


def _is_pydantic_instance(value: Any) -> bool:
    return not isinstance(value, type) and hasattr(value, "model_dump") and hasattr(type(value), "model_fields")


def _fields(value: Any) -> dict[str, Any] | None:
    """A dataclass's or pydantic model's fields (one level), else None."""
    if _is_dataclass_instance(value):
        return {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
    if _is_pydantic_instance(value):
        return {name: getattr(value, name) for name in type(value).model_fields}
    return None


def _parts_of(value: Any) -> dict[str, Any] | None:
    """The named outputs of a return value with several, else None: a dict,
    dataclass or pydantic model with at least one table-like value."""
    if isinstance(value, dict):
        items = value
    else:
        items = _fields(value)
        if items is None:
            return None
    if not any(_is_table(v) for v in items.values()):
        return None
    bad = [repr(k) for k in items if not (isinstance(k, str) and PART_NAME.match(k))]
    if bad:
        raise TypeError(
            "A node that returns several outputs names each one with a lowercase identifier "
            f"(letters, digits and _, not starting with a digit), so SQL can read it as <node>.<name>. Not: {', '.join(bad)}"
        )
    return dict(items)


def _is_table(value: Any) -> bool:
    """Whether ``_as_frame`` would make a DataFrame of ``value``, without converting it."""
    import pandas as pd

    return isinstance(value, (pd.DataFrame, pd.Series)) or (hasattr(value, "to_pandas") and not isinstance(value, type))


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
    if _is_pydantic_instance(obj):
        return obj.model_dump(mode="json")
    if _is_dataclass_instance(obj):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    raise UnsupportedOutput(type(obj).__name__)


def _to_json(value: Any) -> str:
    try:
        return json.dumps(value, default=_json_default)
    except (UnsupportedOutput, TypeError, ValueError) as e:
        raise TypeError(
            "A node must return a DataFrame (stored as Parquet), JSON-compatible data "
            "(dict with string keys, list, str, number, bool, dataclass, pydantic model), "
            "several named outputs (a dict, dataclass or pydantic model with DataFrame values), or None. "
            f"This one returned {type(value).__name__}, which contains something JSON cannot hold: {e}"
        ) from None


def _table_schema(df: Any) -> list[dict[str, str]]:
    return [{"name": str(c), "type": str(t)} for c, t in df.dtypes.items()]


def _frame_for_parquet(df: Any) -> Any:
    import pandas as pd

    if not isinstance(df.index, pd.RangeIndex) or df.index.name is not None:
        df = df.reset_index()
    df.columns = [str(c) for c in df.columns]
    return df


def _parquet_bytes(df: Any) -> bytes:
    import io

    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow", **PARQUET_OPTIONS)
    return buf.getvalue()


def _write_parts(parts: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    """Write a multi-output zip; returns one schema entry per part."""
    schema: list[dict[str, Any]] = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for name, value in parts.items():
            frame = _as_frame(value)
            if frame is not None:
                frame = _frame_for_parquet(frame)
                data = _parquet_bytes(frame)
                kind = "table"
                schema.append({"name": name, "type": "table", "rows": int(len(frame)), "columns": _table_schema(frame)})
            else:
                data = _to_json(value).encode("utf-8")
                kind = "json"
                schema.append({"name": name, "type": "json", "rows": None, "columns": None})
            info = zipfile.ZipInfo(f"{name}.{PART_EXT[kind]}", date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return schema


def save_artifact(value: Any, out_dir: Path) -> dict[str, Any]:
    """Serialize a node's return value; artifact id = content hash."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp)
    meta: dict[str, Any] = {"rows": None, "schema": None}
    try:
        if value is None:
            tmp_path.unlink()
            return {"kind": "none", "artifact_id": "none", "artifact_path": None, **meta}
        parts = _parts_of(value)
        frame = None if parts is not None else _as_frame(value)
        if parts is not None:
            meta["schema"] = _write_parts(parts, tmp_path)
            kind, ext = "multi", "zip"
        elif frame is not None:
            df = _frame_for_parquet(frame)
            df.to_parquet(tmp_path, index=False, engine="pyarrow", **PARQUET_OPTIONS)
            kind, ext = "table", "parquet"
            meta = {"rows": int(len(df)), "schema": _table_schema(df)}
        else:
            tmp_path.write_text(_to_json(value), encoding="utf-8")
            kind, ext = "json", "json"
            meta["schema"] = [{"name": "value", "type": type(value).__name__}]
        return {"kind": kind, **_keep(tmp_path, out_dir, ext), **meta}
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _keep(tmp_path: Path, out_dir: Path, ext: str) -> dict[str, Any]:
    """Name a written output by its content: ``<sha256[:16]>.<ext>``."""
    with open(tmp_path, "rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()[:16]
    final = out_dir / f"{digest}.{ext}"
    if final.exists():
        tmp_path.unlink()
    else:
        tmp_path.replace(final)
    return {"artifact_id": digest, "artifact_path": str(final)}


# The same choices as PARQUET_OPTIONS, in DuckDB's COPY: zstd, format v2
# data pages and encodings, page indexes. DuckDB has no page checksums.
DUCKDB_PARQUET = "format parquet, compression zstd, parquet_version v2, write_page_index true"


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_inputs(con: Any, inputs: dict[str, Any], scratch: Path) -> None:
    """Each upstream table as a view named after its node, read from its
    Parquet file(s) where they are: no copy in memory, and DuckDB's own types.
    A node with several outputs is a schema holding a view per table part.
    JSON outputs, and upstreams with no output, are not readable from SQL."""
    for dep, i in inputs.items():
        name = '"' + dep.replace('"', '""') + '"'
        paths, kind = i.get("paths"), i.get("kind")
        if paths is not None and kind == "table":  # a partitioned output: its chunk files
            if not paths:
                raise ValueError(
                    f"upstream {dep} is a partitioned output with no partitions yet, so it has no columns SQL can read"
                )
            files = ", ".join(_sql_literal(p) for p in paths)
            con.execute(f"create view {name} as select * from read_parquet([{files}], union_by_name = true)")
        elif not i.get("path"):
            continue
        elif kind == "table":
            con.execute(f"create view {name} as select * from read_parquet({_sql_literal(i['path'])})")
        elif kind == "multi":
            # DuckDB cannot read inside a zip: each table part is extracted
            # next to the run, then is <dep>.<part>.
            con.execute(f"create schema if not exists {name}")
            with zipfile.ZipFile(i["path"]) as z:
                for member in z.namelist():
                    part, part_kind = _member_part(member)
                    if part_kind != "table":
                        continue
                    local = scratch / dep / member
                    local.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(member) as src, open(local, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    con.execute(
                        f'create view {name}."{part}" as select * from read_parquet({_sql_literal(local.as_posix())})'
                    )


def _without_trailing_terminators(sql: str) -> str:
    """``sql`` without the semicolons and comments after its last statement.
    DuckDB keeps them in the text of the relation the last statement makes,
    and fails on them when it reads that relation again."""
    import duckdb

    tokens = duckdb.tokenize(sql)
    end = len(tokens)
    while end:
        pos, kind = tokens[end - 1]
        stop = tokens[end][0] if end < len(tokens) else len(sql)
        if kind == duckdb.token_type.comment or set(sql[pos:stop].strip()) == {";"}:
            end -= 1
        else:
            break
    return sql[: tokens[end][0]] if end < len(tokens) else sql


def _run_sql(sql: str, params: dict[str, Any], inputs: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Run a SQL node in DuckDB and write its result straight to Parquet with
    COPY. The rows never become a DataFrame, so every column keeps its DuckDB
    type (DATE, DECIMAL, nullable integers, time zones, lists, structs).

    Parameters bind as in ``con.execute``: DuckDB named parameters, used by
    the last statement. The statements before it run first; the last one's
    rows are the output."""
    import duckdb

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="exuvia-sql-") as scratch:
        con = duckdb.connect()
        try:
            _sql_inputs(con, inputs, Path(scratch))
            sql = _without_trailing_terminators(sql)
            result = con.sql(sql, params=params) if params else con.sql(sql)
            if result is None:
                raise TypeError("A SQL node ends with a query: the rows it returns are the node's output.")
            con.register("__exuvia_result", result)
            fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
            os.close(fd)
            tmp_path = Path(tmp)
            try:
                (rows,) = con.execute(
                    f"copy (select * from __exuvia_result) to {_sql_literal(tmp_path.as_posix())} ({DUCKDB_PARQUET})"
                ).fetchone()
                described = con.execute(f"describe select * from read_parquet({_sql_literal(tmp_path.as_posix())})")
                schema = [{"name": r[0], "type": r[1]} for r in described.fetchall()]
                con.close()
                return {"kind": "table", **_keep(tmp_path, out_dir, "parquet"), "rows": int(rows), "schema": schema}
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()
        finally:
            con.close()


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
    # The project's environment variables (envvars.py). Set before the node's
    # module is imported, so module-level code reads them too.
    os.environ.update(spec.get("env") or {})
    params = spec.get("params") or {}
    exuvia._runtime = {"node": spec["node"], "params": params}  # what exuvia.param() reads
    partition_by = spec.get("partition_by")
    have: list[str] = list(spec.get("partitions") or [])
    if partition_by:
        exuvia._runtime["partitions"] = have  # what exuvia.partitions() reads
    if spec["kind"] != "python":
        # A SQL node runs entirely in DuckDB: it always returns one table,
        # which is what a partitioned node must return too.
        sql = (root / spec["path"]).read_text(encoding="utf-8")
        # Declared parameters are bound as DuckDB named parameters: `$as_of`.
        # DuckDB rejects values the statement does not use, so bind only those.
        bound = dict(params)
        if partition_by:  # the newest partition already stored (null when there is none)
            bound["last_partition"] = have[-1] if have else None
        used = {name: bound[name] for name in bound if re.search(rf"\${name}\b", sql)}
        return _run_sql(sql, used, spec["inputs"], Path(spec["out_dir"]))
    inputs = {dep: load_artifact(i.get("path"), i.get("kind"), i.get("paths")) for dep, i in spec["inputs"].items()}
    fn = _load_python_node(root, root / spec["path"], spec["node"])
    value = fn(**_typed_inputs(fn, inputs))
    if partition_by:
        _check_partitioned(value, partition_by)
    return save_artifact(value, Path(spec["out_dir"]))


def _check_partitioned(value: Any, column: str) -> None:
    """A partitioned node returns one table holding its partition column, or None."""
    if value is None:
        return
    if _parts_of(value) is not None or _as_frame(value) is None:
        raise TypeError(
            f"A node partitioned by `{column}` returns one table (a DataFrame) whose `{column}` column says "
            f"which partition each row belongs to, or None for no new rows. This one returned {type(value).__name__}."
        )


def _typed_inputs(fn: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    """Build the parameters annotated with a dataclass or a pydantic model
    from their upstream's output: a JSON object, or the parts of a node with
    several outputs. Other parameters get the output as it is."""
    import typing

    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # an annotation that does not resolve: leave every input untyped
        hints = {}
    out = dict(inputs)
    for dep, value in inputs.items():
        cls = hints.get(dep)
        if not isinstance(cls, type) or not isinstance(value, dict):
            continue
        try:
            if hasattr(cls, "model_validate") and hasattr(cls, "model_fields"):
                out[dep] = cls.model_validate(value)
            elif dataclasses.is_dataclass(cls):
                out[dep] = cls(**value)
        except Exception as e:
            raise TypeError(f"could not build {cls.__name__} for parameter {dep!r} from {dep}'s output: {e}") from e
    return out


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
