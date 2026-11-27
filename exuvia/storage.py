"""What a project keeps on disk, and how (GET /api/storage).

One report for the workbench's Storage page: the size of the project's
state folder (``.exuvia/``) by what is in it, every stored output by node
(current and kept from earlier runs, since exuvia never deletes outputs),
the Parquet files' compression from their footers, each partitioned
output's files against its compaction limit, and the records database.

Read from the disk and the records alone: no row of an output is read.
Footers are read once per file and remembered, since an output file is
named by its content and never changes. A file that is only in the remote
artifact store (``artifacts.py``) and not cached here has no size to give:
it is counted under ``remote.missing``.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import partitioned
from .artifacts import artifact_store
from .db import SQLiteDatabase

if TYPE_CHECKING:
    from .workspace import Workspace

# What each entry of .exuvia/ holds, in words; anything else is "Other".
STATE_LABELS = {
    "artifacts": "Node outputs",
    "parts": "Extracted parts (cache)",
    "packages": "Python packages",
    "npm": "View modules (cache)",
    "staging": "Runs being committed",
    "exuvia.db": "Records database",
}
# Folders that are not the project's own files (beside .exuvia/ and any dot folder).
NOT_CODE = {"node_modules", "__pycache__"}

_footers: dict[tuple[str, int], dict[str, Any]] = {}
_footers_lock = threading.Lock()
_FOOTERS_KEPT = 50_000


def _footer(path: Path, size: int) -> dict[str, Any] | None:
    """Rows, row groups, uncompressed bytes and bytes by codec of one
    Parquet file, from its footer; None when it can't be read."""
    key = (str(path), size)
    with _footers_lock:
        hit = _footers.get(key)
    if hit is not None:
        return hit
    try:
        import pyarrow.parquet as pq

        meta = pq.ParquetFile(path).metadata
    except Exception:  # noqa: BLE001 - a file that isn't Parquet after all, or is cut short: no footer to count
        return None
    raw = 0
    codecs: dict[str, int] = {}
    for g in range(meta.num_row_groups):
        rg = meta.row_group(g)
        for c in range(rg.num_columns):
            chunk = rg.column(c)
            raw += chunk.total_uncompressed_size
            codecs[chunk.compression] = codecs.get(chunk.compression, 0) + chunk.total_compressed_size
    out = {"rows": meta.num_rows, "row_groups": meta.num_row_groups, "uncompressed_bytes": raw, "codecs": codecs}
    with _footers_lock:
        if len(_footers) >= _FOOTERS_KEPT:
            _footers.clear()
        _footers[key] = out
    return out


def _walk(path: Path, skip: set[str] | None = None) -> tuple[int, int]:
    """(bytes, files) under ``path``, not following links; entries named in
    ``skip`` and dot entries are left out when ``skip`` is given."""
    total = files = 0
    stack = [path]
    while stack:
        here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for e in entries:
            if skip is not None and (e.name.startswith(".") or e.name in skip):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
                    files += 1
            except OSError:
                continue
    return total, files


def _state(state_dir: Path) -> list[dict[str, Any]]:
    """Each entry of .exuvia/ by what it holds, largest first. The database's
    -wal and -shm files count with it."""
    sizes: dict[str, list[int]] = {}
    try:
        entries = list(os.scandir(state_dir))
    except OSError:
        entries = []
    for e in entries:
        name = e.name.split("-")[0] if e.name.startswith("exuvia.db") else e.name
        key = name if name in STATE_LABELS else "other"
        try:
            if e.is_dir(follow_symlinks=False):
                b, n = _walk(Path(e.path))
            elif e.is_file(follow_symlinks=False):
                b, n = e.stat(follow_symlinks=False).st_size, 1
            else:
                continue
        except OSError:
            continue
        acc = sizes.setdefault(key, [0, 0])
        acc[0] += b
        acc[1] += n
    out = [
        {"key": k, "label": STATE_LABELS.get(k, "Other"), "bytes": b, "files": n}
        for k, (b, n) in sizes.items()
    ]
    return sorted((p for p in out if p["files"]), key=lambda p: (-p["bytes"], p["key"]))


def _kind(name: str) -> str:
    ext = name.rpartition(".")[2]
    return {"parquet": "parquet", "json": "json", "zip": "multi"}.get(ext, "other")


def report(ws: "Workspace") -> dict[str, Any]:
    """The Storage page's figures for ``ws``'s project (see the module).
    Blocking: call it off the event loop."""
    root = ws.project.root
    state_dir = ws.project.state_dir
    store = ws.store
    g = ws.graph

    # -- what the records say is stored, and what is current -----------------
    recorded: dict[str, str] = {}  # file (relative to the root) -> node
    current: set[str] = set()
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for r in store.output_records():
        if r["artifact_path"]:
            recorded[r["artifact_path"]] = r["node"]
        latest[(r["node"], r["params_key"] or "")] = r
    chunks = store.chunk_records()
    for c in chunks:
        recorded[c["file"]] = c["node"]
    by_output: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in chunks:
        by_output.setdefault((c["node"], c["params_key"] or ""), []).append(c)

    outputs_partitioned: list[dict[str, Any]] = []
    for (node, key), r in latest.items():
        if r["version"] is None:
            if r["artifact_path"]:
                current.add(r["artifact_path"])
            continue
        version = int(r["version"] or 0)
        mine = by_output.get((node, key), [])
        live = [c for c in mine if version and c["since"] <= version and (c["until"] is None or c["until"] > version)]
        current.update(c["file"] for c in live)
        periods = set()
        merged = 0
        parts = 0
        for c in live:
            vals = _json_list(c["partitions"])
            parts += len(vals)
            merged += len(vals) > 1
            if vals:
                periods.add(partitioned.period(min(vals, key=partitioned._sortable)))
        superseded = {c["file"] for c in mine} - {c["file"] for c in live}
        outputs_partitioned.append({
            "node": node,
            "params_key": key,
            "version": version,
            "partitions": parts,
            "files": len(live),
            "merged_files": merged,
            "periods": len(periods),
            "limit": len(periods) + partitioned.COMPACT_AFTER,
            "superseded_files": len(superseded),
        })
    outputs_partitioned.sort(key=lambda p: (p["node"], p["params_key"]))

    # -- what is on disk -------------------------------------------------------
    stored = store.stored_paths() if artifact_store() is not None else set()
    nodes: dict[str, dict[str, Any]] = {}

    def row(name: str) -> dict[str, Any]:
        n = nodes.get(name)
        if n is None:
            node = g.nodes.get(name)
            n = nodes[name] = {
                "name": name,
                "exists": node is not None,
                "partitioned": bool(node and node.partition_by),
                "runs": 0,
                "files": 0,
                "bytes": 0,
                "current_files": 0,
                "current_bytes": 0,
                "parquet_bytes": 0,
                "uncompressed_bytes": 0,
                "compressed_bytes": 0,
                "rows": 0,
                "kinds": {},
            }
        return n

    totals = {k: {"files": 0, "bytes": 0} for k in ("parquet", "json", "multi", "other")}
    parquet = {
        "files": 0, "bytes": 0, "uncompressed_bytes": 0, "compressed_bytes": 0, "rows": 0, "row_groups": 0, "codecs": {},
    }
    seen: set[str] = set()
    art = state_dir / "artifacts"
    try:
        node_dirs = [d for d in os.scandir(art) if d.is_dir(follow_symlinks=False)]
    except OSError:
        node_dirs = []
    for d in node_dirs:
        try:
            files = [f for f in os.scandir(d.path) if f.is_file(follow_symlinks=False)]
        except OSError:
            continue
        for f in files:
            rel = f"{ws._rel(f.path)}"
            size = f.stat(follow_symlinks=False).st_size
            name = recorded.get(rel, d.name)
            n = row(name)
            kind = _kind(f.name)
            seen.add(rel)
            n["files"] += 1
            n["bytes"] += size
            n["kinds"][kind] = n["kinds"].get(kind, 0) + 1
            totals[kind]["files"] += 1
            totals[kind]["bytes"] += size
            is_current = rel in current
            if is_current:
                n["current_files"] += 1
                n["current_bytes"] += size
            if kind != "parquet":
                continue
            ft = _footer(Path(f.path), size)
            n["parquet_bytes"] += size
            parquet["files"] += 1
            parquet["bytes"] += size
            if ft is None:
                continue
            packed = sum(ft["codecs"].values())
            n["uncompressed_bytes"] += ft["uncompressed_bytes"]
            n["compressed_bytes"] += packed
            if is_current:
                n["rows"] += ft["rows"]
            parquet["uncompressed_bytes"] += ft["uncompressed_bytes"]
            parquet["compressed_bytes"] += packed
            parquet["rows"] += ft["rows"]
            parquet["row_groups"] += ft["row_groups"]
            for codec, b in ft["codecs"].items():
                parquet["codecs"][codec] = parquet["codecs"].get(codec, 0) + b

    runs = store.run_counts()
    for name, count in runs.items():
        if name in g.nodes or name in nodes:
            row(name)["runs"] = count
    for name in g.nodes:
        row(name)

    missing = [p for p in recorded if p not in seen]
    state = _state(state_dir)
    code_bytes, code_files = _walk(root, NOT_CODE)

    db = store.db
    db_kind = "sqlite" if isinstance(db, SQLiteDatabase) else getattr(db, "dialect", "database")
    db_file = getattr(db, "file", None)
    in_project = bool(db_file) and Path(db_file).resolve().parent == state_dir.resolve()

    return {
        "generated_at": time.time(),
        "total_bytes": sum(p["bytes"] for p in state) + code_bytes,
        "code": {"bytes": code_bytes, "files": code_files},
        "state": state,
        "outputs": {
            "files": sum(t["files"] for t in totals.values()),
            "bytes": sum(t["bytes"] for t in totals.values()),
            "current_files": sum(n["current_files"] for n in nodes.values()),
            "current_bytes": sum(n["current_bytes"] for n in nodes.values()),
            "by_kind": totals,
        },
        "parquet": {
            **parquet,
            "codecs": [{"codec": c, "bytes": b} for c, b in sorted(parquet["codecs"].items(), key=lambda x: -x[1])],
        },
        "nodes": sorted(nodes.values(), key=lambda n: (-n["bytes"], n["name"])),
        "partitioned": outputs_partitioned,
        "compact_after": partitioned.COMPACT_AFTER,
        "remote": {
            "configured": artifact_store() is not None,
            "uploaded": len(stored),
            "missing": len(missing),
        },
        "database": {
            "kind": db_kind,
            "in_project": in_project,
            "bytes": next((p["bytes"] for p in state if p["key"] == "exuvia.db"), None) if in_project else None,
            "rows": store.counts(),
        },
    }


def _json_list(text: str | None) -> list[str]:
    import json

    try:
        out = json.loads(text or "[]")
    except ValueError:
        return []
    return [str(v) for v in out] if isinstance(out, list) else []
