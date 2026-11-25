"""A node output as a manifest of content-addressed Parquet chunks.

Exploration code for "Partitioned outputs without DuckLake" in
../incremental-nodes-and-ducklake.md. Nothing in exuvia imports it.

A chunk is a Parquet file named after the SHA-256 of its bytes, like exuvia's
artifacts today. A manifest is a small JSON value:

    {"key": "date",
     "partitions": {"2026-11-20": "<digest>", ...},   # what the data is
     "files": [{"file": "<sha>.parquet", "partitions": ["2026-11-20", ...], "rows": n}, ...]}

- `partitions` maps each partition value to a digest of its rows: SHA-256 of
  the rows as text, sorted. It is not the chunk's file name, because two ways
  of writing the same rows (one day, or a backfill split in one pass) give
  different Parquet bytes. `logical_id` hashes only that map, so it changes
  when data changes and not when compaction rearranges files. Freshness and
  cascades would key on it.
- `files` is where the rows physically are. `physical_id` hashes it.
- Writes never change a file: a commit returns a new manifest, and old
  manifests stay readable (time travel, pinned readers).

Operations:
- `replace(m, rows_sql)`: split rows by the key, one chunk per partition, and
  replace those partitions. A daily run writes one; a backfill writes many.
  A partition rewritten with identical rows keeps its digest, so the logical
  id doesn't move and a cascade would stop.
- `compact(m, group)`: merge files into one per group of partitions (for
  example a year of days). Copy-on-write: the logical id is unchanged.
- Replacing a partition that lives in a compacted file rewrites that file
  without it.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import uuid
from typing import Callable

import duckdb


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class Store:
    """Chunks under ROOT/chunks/<sha>.parquet, written by one connection (the control plane)."""

    def __init__(self, root: str):
        self.root = root.replace("\\", "/").rstrip("/")
        self.dir = self.root + "/chunks"
        os.makedirs(self.dir, exist_ok=True)
        self.con = duckdb.connect()

    def path(self, name: str) -> str:
        return f"{self.dir}/{name}"

    def write(self, select_sql: str) -> tuple[str, int]:
        """Write one chunk from a SELECT; return (file name, rows). Same bytes, same name."""
        tmp = f"{self.dir}/.tmp-{uuid.uuid4().hex}.parquet"
        self.con.execute(f"COPY ({select_sql}) TO {_lit(tmp)} (FORMAT parquet, COMPRESSION zstd)")
        with open(tmp, "rb") as f:
            name = _sha(f.read()) + ".parquet"
        rows = self.con.execute(f"SELECT count(*) FROM read_parquet({_lit(tmp)})").fetchone()[0]
        if os.path.exists(self.path(name)):
            os.remove(tmp)
        else:
            os.replace(tmp, self.path(name))
        return name, rows

    def write_partitioned(self, table: str, key: str) -> dict[str, tuple[str, int]]:
        """One chunk per value of `key`, in one pass (a backfill): {value: (file name, rows)}.

        COPY ... PARTITION_BY writes a directory per value; each file is then
        renamed to its digest. The helper column holding the value is not
        written, so a chunk has the same columns as one from `write`.
        """
        tmp = f"{self.dir}/.tmp-{uuid.uuid4().hex}"
        self.con.execute(f"COPY (SELECT *, CAST({key} AS VARCHAR) AS __p FROM {table} ORDER BY ALL) TO {_lit(tmp)} "
                         f"(FORMAT parquet, COMPRESSION zstd, PARTITION_BY (__p))")
        counts = dict(self.con.execute(f"SELECT CAST({key} AS VARCHAR), count(*) FROM {table} GROUP BY 1").fetchall())
        out = {}
        for d in os.listdir(tmp):
            value = urllib.parse.unquote(d.split("=", 1)[1])
            (src,) = [f"{tmp}/{d}/{n}" for n in os.listdir(f"{tmp}/{d}")]
            with open(src, "rb") as f:
                name = _sha(f.read()) + ".parquet"
            if os.path.exists(self.path(name)):
                os.remove(src)
            else:
                os.replace(src, self.path(name))
            os.rmdir(f"{tmp}/{d}")
            out[value] = (name, counts[value])
        os.rmdir(tmp)
        return out

    def files(self, m: dict) -> list[str]:
        return [self.path(f["file"]) for f in m["files"]]


def empty(key: str) -> dict:
    return {"key": key, "partitions": {}, "files": []}


def logical_id(m: dict) -> str:
    return _sha(json.dumps([m["key"], sorted(m["partitions"].items())]).encode())[:16]


def physical_id(m: dict) -> str:
    return _sha(json.dumps(sorted(f["file"] for f in m["files"])).encode())[:16]


def _without(s: Store, m: dict, drop: set[str]) -> list[dict]:
    """m's files with the partitions in `drop` removed; a file holding some of them is rewritten."""
    out = []
    key = m["key"]
    for f in m["files"]:
        hit = drop.intersection(f["partitions"])
        if not hit:
            out.append(f)
            continue
        keep = [p for p in f["partitions"] if p not in drop]
        if keep:  # copy-on-write: the file minus the replaced partitions
            vals = ", ".join(_lit(p) for p in keep)
            name, rows = s.write(f"SELECT * FROM read_parquet({_lit(s.path(f['file']))}) "
                                 f"WHERE CAST({key} AS VARCHAR) IN ({vals}) ORDER BY ALL")
            out.append({"file": name, "partitions": keep, "rows": rows})
    return out


def replace(s: Store, m: dict, rows_sql: str) -> dict:
    """Commit the rows of `rows_sql`: every partition they contain is replaced by them."""
    key = m["key"]
    s.con.execute(f"CREATE OR REPLACE TEMP TABLE _new AS {rows_sql}")
    digests = dict(s.con.execute(
        f"SELECT CAST({key} AS VARCHAR), sha256(string_agg(CAST(_new AS VARCHAR), chr(10) "
        f"ORDER BY CAST(_new AS VARCHAR))) FROM _new GROUP BY 1").fetchall())
    changed = sorted(v for v, d in digests.items() if m["partitions"].get(v) != d)
    if not changed:
        return m  # identical rows: nothing written, logical id unchanged
    if len(changed) == 1:
        written = {changed[0]: s.write(f"SELECT * FROM _new WHERE CAST({key} AS VARCHAR) = {_lit(changed[0])} ORDER BY ALL")}
    else:
        if len(changed) < len(digests):
            vals = ", ".join(_lit(v) for v in changed)
            s.con.execute(f"DELETE FROM _new WHERE CAST({key} AS VARCHAR) NOT IN ({vals})")
        written = s.write_partitioned("_new", key)
    new_files = [{"file": written[v][0], "partitions": [v], "rows": written[v][1]} for v in changed]
    return {"key": key, "partitions": {**m["partitions"], **{v: digests[v] for v in changed}},
            "files": _without(s, m, set(changed)) + new_files}


def compact(s: Store, m: dict, group: Callable[[str], str]) -> dict:
    """One file per group of partitions (e.g. group=lambda d: d[:4] for a file per year)."""
    key = m["key"]
    groups: dict[str, list[dict]] = {}
    for f in m["files"]:
        groups.setdefault(group(min(f["partitions"])), []).append(f)
    files = []
    for g in sorted(groups):
        fs = groups[g]
        if len(fs) == 1:
            files.append(fs[0])
            continue
        paths = "[" + ", ".join(_lit(s.path(f["file"])) for f in fs) + "]"
        name, rows = s.write(f"SELECT * FROM read_parquet({paths}, union_by_name = true) ORDER BY ALL")
        files.append({"file": name, "partitions": sorted(p for f in fs for p in f["partitions"]), "rows": rows})
    return {"key": key, "partitions": dict(m["partitions"]), "files": files}


def prune(m: dict, keep: Callable[[str], bool]) -> dict:
    """The files holding any partition `keep` accepts: what exuvia would hand a reader that filters on the key."""
    return {**m, "files": [f for f in m["files"] if any(keep(p) for p in f["partitions"])]}


def select_sql(s: Store, m: dict) -> str:
    """The SELECT a reader runs: one read_parquet over the manifest's files."""
    if not m["files"]:
        raise ValueError("empty output")
    return "SELECT * FROM read_parquet([" + ", ".join(_lit(p) for p in s.files(m)) + "], union_by_name = true)"
