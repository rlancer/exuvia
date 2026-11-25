"""Partitioned node outputs.

A node declared with ``partition_by="date"`` keeps one output that each run
adds to: the rows a run returns replace the partitions (the values of the
partition column) they hold, and every other partition is kept. A daily run
returns today's rows, a backfill ten years of them in one call, a
correction the day it fixes.

The output is a *manifest* of content-addressed Parquet chunks:

* ``partitions`` maps each partition value (as text) to a digest of its
  rows and its row count. The digest is a SHA-256 over the SHA-256 of each
  row as text, sorted, computed in SQL: two ways of writing the same rows
  (one day alone, or many in one pass) give different Parquet bytes but the
  same digest.
* ``files`` lists the chunk files, ``.exuvia/artifacts/<node>/<sha>.parquet``
  named like every other output, with the partitions each holds.

The *logical id* hashes the partition-to-digest map. It is the run's
``artifact_id``, so freshness, cascades and caches key on it: rewriting a
partition with identical rows writes nothing and leaves it alone, and
compaction, which only moves rows between files, never changes it.

Compaction is required, not an optimization: every file costs a reader about
1.5 ms a query and the sandbox about 0.8 ms to set up (see
docs/design/incremental-nodes-and-ducklake.md). Once an output has more than
``COMPACT_AFTER`` files beyond one per period, every period holding several
files is merged into one, copy-on-write. Dates group by year; other values
form a single group. A correction to a day inside a merged file rewrites
that file without the day.

The control plane is the only writer. Every version of a manifest stays in
the records database (``store.py``), so an older run's output stays
readable; chunks are never deleted (exuvia never deletes outputs).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# How many files an output may have beyond one per period before compacting.
COMPACT_AFTER = int(os.environ.get("EXUVIA_COMPACT_AFTER", "30"))
_DATE_LIKE = re.compile(r"^\d{4}-\d{2}")


class PartitionError(ValueError):
    """A partitioned run returned something that can't be committed."""


@dataclass
class Manifest:
    """One version of a partitioned output. ``partitions``: value ->
    (digest, rows); ``files``: [{file, partitions, rows}], ``file`` relative
    to the project root."""

    key: str
    partitions: dict[str, tuple[str, int]] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    version: int | None = None

    @property
    def logical_id(self) -> str:
        return logical_id(self.key, self.partitions)

    @property
    def rows(self) -> int:
        return sum(rows for _, rows in self.partitions.values())


def logical_id(key: str, partitions: dict[str, tuple[str, int]]) -> str:
    canon = json.dumps([key, sorted((v, d) for v, (d, _) in partitions.items())], separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def period(value: str) -> str:
    """The group a partition is compacted with: its year for a date-like value."""
    return value[:4] if _DATE_LIKE.match(value) else ""


@dataclass
class Commit:
    manifest: Manifest
    written: list[str]  # files new in this version (to upload), relative to the project root


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sortable(value: str) -> tuple[int, Any]:
    try:
        return (0, float(value))
    except ValueError:
        return (1, value)


def sort_values(values: Any) -> list[str]:
    """Partition values in order: numbers by value, the rest as text (ISO dates sort as dates)."""
    return sorted(values, key=_sortable)


def in_range(value: str, lo: str | None, hi: str | None) -> bool:
    """Whether ``value`` lies in [lo, hi], compared as numbers when both are, as text otherwise."""
    for bound, ok in ((lo, lambda a, b: a >= b), (hi, lambda a, b: a <= b)):
        if bound is None or bound == "":
            continue
        a, b = _sortable(value), _sortable(bound)
        if not (ok(a[1], b[1]) if a[0] == b[0] else ok(value, bound)):
            return False
    return True


class Writer:
    """Writes chunks for one node: Parquet files named by the SHA-256 of
    their bytes under ``out_dir``. ``rel`` makes a written path relative to
    the project root; ``local`` gives a recorded file's local path,
    downloading it from the artifact store when needed."""

    def __init__(self, out_dir: Path, rel: Callable[[Path], str], local: Callable[[str], str]):
        import duckdb

        self.out_dir = out_dir
        self.rel = rel
        self.local = local
        out_dir.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
        self.created: set[str] = set()  # files this writer made that did not exist before

    def _keep(self, tmp: Path) -> str:
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()[:16]
        final = self.out_dir / f"{digest}.parquet"
        rel = self.rel(final)
        if final.exists():
            tmp.unlink()
        else:
            tmp.replace(final)
            self.created.add(rel)
        return rel

    def discard_unused(self, keep: set[str]) -> None:
        """Delete the files this writer made that ``keep`` doesn't use (a
        day's chunk folded into its year in the same commit): nothing else
        can refer to them."""
        for rel in self.created - keep:
            with contextlib.suppress(OSError):
                Path(self.local(rel)).unlink()
        self.created &= keep

    def write(self, select_sql: str) -> str:
        fd, name = tempfile.mkstemp(dir=self.out_dir, suffix=".tmp")
        os.close(fd)
        tmp = Path(name)
        try:
            self.con.execute(f"copy ({select_sql}) to {_lit(str(tmp))} (format parquet, compression zstd)")
            return self._keep(tmp)
        finally:
            if tmp.exists():
                tmp.unlink()

    def write_partitioned(self, table: str, key: str, values: list[str]) -> dict[str, str]:
        """One chunk per value, in one pass over ``table``: {value: file}.
        COPY ... PARTITION_BY names a folder after each value, which a value
        such as ``..`` would break, so the rows are split by the value's
        index in ``values`` instead; that column isn't written."""
        self.con.execute("create or replace temp table _idx (v varchar, i integer)")
        self.con.executemany("insert into _idx values (?, ?)", [(v, i) for i, v in enumerate(values)])
        tmp = Path(tempfile.mkdtemp(dir=self.out_dir, prefix=".split-"))
        try:
            self.con.execute(
                f"copy (select {table}.*, _idx.i as __exuvia_i from {table} join _idx "
                f"on cast({table}.{_ident(key)} as varchar) = _idx.v) to {_lit(str(tmp / 'o'))} "
                "(format parquet, compression zstd, partition_by (__exuvia_i))"
            )
            out: dict[str, str] = {}
            for sub in (tmp / "o").iterdir():
                i = int(sub.name.split("=", 1)[1])
                (src,) = list(sub.iterdir())
                out[values[i]] = self._keep(src)
            return out
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def schema(self, files: list[str]) -> list[dict[str, str]]:
        if not files:
            return []
        paths = "[" + ", ".join(_lit(self.local(f)) for f in files) + "]"
        rows = self.con.execute(f"describe select * from read_parquet({paths}, union_by_name = true)").fetchall()
        return [{"name": r[0], "type": r[1]} for r in rows]


def commit(base: Manifest, staged: str | None, w: Writer, *, full_refresh: bool = False) -> Commit | None:
    """Commit the rows of the Parquet file ``staged`` (None: no rows) on top
    of ``base``: each partition they hold replaces that partition; with
    ``full_refresh`` every partition they don't hold is dropped too. Returns
    None when nothing changed: identical rows write nothing."""
    key = base.key
    con = w.con
    digests: dict[str, tuple[str, int]] = {}
    if staged is not None:
        con.execute("create or replace temp table _new as select * from read_parquet(?)", [staged])
        (count,) = con.execute("select count(*) from _new").fetchone() or (0,)
        if not count:
            staged = None  # no rows: nothing to replace, whatever the columns
    if staged is not None:
        cols = [r[0] for r in con.execute("describe _new").fetchall()]
        if key not in cols:
            raise PartitionError(
                f"the node is partitioned by `{key}`, but the table it returned has no `{key}` column "
                f"(it has {', '.join(cols) or 'none'})"
            )
        q = _ident(key)
        (nulls,) = con.execute(f"select count(*) from _new where {q} is null").fetchone() or (0,)
        if nulls:
            some = "1 row" if nulls == 1 else f"{nulls} rows"
            raise PartitionError(f"{some} returned {'has' if nulls == 1 else 'have'} no `{key}`: every row needs a partition value")
        # Each row as text, hashed; a partition's digest hashes its rows' hashes in order.
        rows = con.execute(
            f"select v, sha256(string_agg(h, '' order by h)), count(*) from "
            f"(select cast({q} as varchar) v, sha256(cast(_new as varchar)) h from _new) group by v"
        ).fetchall()
        digests = {v: (d, int(n)) for v, d, n in rows}
    changed = sort_values(v for v, (d, _) in digests.items() if (base.partitions.get(v) or (None,))[0] != d)
    removed = sort_values(set(base.partitions) - set(digests)) if full_refresh else []
    if not changed and not removed:
        return None
    if len(changed) == 1:
        (v,) = changed
        new = {v: w.write(f"select * from _new where cast({_ident(key)} as varchar) = {_lit(v)}")}
    elif changed:
        new = w.write_partitioned("_new", key, changed)
    else:
        new = {}
    files = _without(base, set(changed) | set(removed), w)
    files += [{"file": new[v], "partitions": [v], "rows": digests[v][1]} for v in changed]
    partitions = {v: p for v, p in base.partitions.items() if v not in removed}
    partitions.update({v: digests[v] for v in changed})
    manifest = compact(Manifest(key, partitions, files), w)
    used = {f["file"] for f in manifest.files}
    w.discard_unused(used)
    before = {f["file"] for f in base.files}
    return Commit(manifest, [f["file"] for f in manifest.files if f["file"] not in before])


def _without(base: Manifest, drop: set[str], w: Writer) -> list[dict[str, Any]]:
    """``base``'s files without the partitions in ``drop``: a file holding
    some of them and others is rewritten with only the others (copy-on-write)."""
    out = []
    for f in base.files:
        hit = drop.intersection(f["partitions"])
        if not hit:
            out.append(f)
            continue
        keep = [p for p in f["partitions"] if p not in drop]
        if keep:
            gone = ", ".join(_lit(p) for p in sort_values(hit))
            name = w.write(
                f"select * from read_parquet({_lit(w.local(f['file']))}) "
                f"where cast({_ident(base.key)} as varchar) not in ({gone}) order by {_ident(base.key)}"
            )
            out.append({"file": name, "partitions": keep, "rows": sum(base.partitions[p][1] for p in keep)})
    return out


def compact(m: Manifest, w: Writer, *, force: bool = False) -> Manifest:
    """Merge the files of every period that has several into one, once the
    output has more than ``COMPACT_AFTER`` files beyond one per period."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in m.files:
        groups.setdefault(period(min(f["partitions"], key=_sortable)), []).append(f)
    if not force and len(m.files) <= len(groups) + COMPACT_AFTER:
        return m
    files: list[dict[str, Any]] = []
    for g in sorted(groups):
        fs = groups[g]
        if len(fs) == 1:
            files.append(fs[0])
            continue
        paths = "[" + ", ".join(_lit(w.local(f["file"])) for f in fs) + "]"
        name = w.write(f"select * from read_parquet({paths}, union_by_name = true) order by {_ident(m.key)}")
        parts = sort_values(p for f in fs for p in f["partitions"])
        files.append({"file": name, "partitions": parts, "rows": sum(f["rows"] for f in fs)})
    return Manifest(m.key, dict(m.partitions), files)


def prune(files: list[dict[str, Any]], lo: str | None, hi: str | None) -> list[dict[str, Any]]:
    """The files holding a partition in [lo, hi], ordered by their first
    partition, so that reading them in turn gives the oldest rows first."""
    files = sorted(files, key=lambda f: _sortable(f["partitions"][0]) if f["partitions"] else (2, ""))
    if not lo and not hi:
        return files
    return [f for f in files if any(in_range(p, lo, hi) for p in f["partitions"])]
