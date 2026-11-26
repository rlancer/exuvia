"""What a table output's Parquet files say about themselves, read from their
footers alone: rows, row groups, sizes and compression, and for each column
its nulls and its smallest and largest value as the writer recorded them.
No row is read, so it costs the same for ten rows as for ten million.

A value the footers cannot answer (a row group written without statistics,
a nested column's nulls, a binary column's bounds) is None, never a guess."""

from __future__ import annotations

import os
from typing import Any

from .outputs import jsonable


def _leaves(t: Any) -> int:
    """How many Parquet leaf columns an Arrow type is stored as."""
    import pyarrow as pa

    if pa.types.is_struct(t):
        return sum(_leaves(t.field(i).type) for i in range(t.num_fields))
    if pa.types.is_map(t):
        return _leaves(t.key_type) + _leaves(t.item_type)
    if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_fixed_size_list(t):
        return _leaves(t.value_type)
    return 1


class _Column:
    """One column's figures, added up over row groups and files."""

    def __init__(self) -> None:
        self.flat = True
        self.values = 0  # rows seen in the row groups that held this column
        self.nulls: int | None = 0
        self.bounded = True  # every row group with a value recorded its bounds
        self.lo: Any = None
        self.hi: Any = None
        self.bytes = 0
        self.raw_bytes = 0

    def chunk(self, chunk: Any, rows: int) -> None:
        self.bytes += chunk.total_compressed_size
        self.raw_bytes += chunk.total_uncompressed_size
        self.values += rows
        if not self.flat:
            return
        s = chunk.statistics if chunk.is_stats_set else None
        if s is None or not s.has_null_count:
            self.nulls = None
        elif self.nulls is not None:
            self.nulls += s.null_count
        if s is not None and s.has_null_count and s.null_count >= rows:
            return  # every value null: no bounds to have
        if s is None or not s.has_min_max or isinstance(s.min, bytes):
            self.bounded = False
            return
        try:
            self.lo = s.min if self.lo is None or s.min < self.lo else self.lo
            self.hi = s.max if self.hi is None or s.max > self.hi else self.hi
        except TypeError:  # files that disagree on the column's type
            self.bounded = False

    def stats(self, rows: int) -> dict[str, Any]:
        # A file without the column holds only nulls in it (read with union_by_name).
        nulls = None if not self.flat or self.nulls is None else self.nulls + (rows - self.values)
        lo, hi = jsonable([[self.lo, self.hi]])[0] if self.flat and self.bounded else (None, None)
        return {"nulls": nulls, "min": lo, "max": hi, "bytes": self.bytes, "uncompressed_bytes": self.raw_bytes}


def parquet_stats(files: list[str]) -> dict[str, Any]:
    """The footers of ``files`` (the Parquet files of one output), summed:
    ``rows``, ``files``, ``row_groups``, ``bytes`` (on disk),
    ``uncompressed_bytes`` (the column data before compression),
    ``compression`` and ``created_by`` (each distinct value), and per column
    ``nulls``, ``min``, ``max``, ``bytes`` and ``uncompressed_bytes``, by name."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = groups = size = 0
    codecs: list[str] = []
    writers: list[str] = []
    cols: dict[str, _Column] = {}
    for path in files:
        size += os.path.getsize(path)
        pf = pq.ParquetFile(path)
        meta = pf.metadata
        if meta.created_by and meta.created_by not in writers:
            writers.append(meta.created_by)
        # Each top-level column and the leaves it is stored as, in order.
        # A nested column (a list, a struct, a map) has its nulls and bounds
        # on its leaves, not on its rows: only its size is answered.
        owners: list[tuple[str, bool]] = []
        for field in pf.schema_arrow:
            owners += [(field.name, not pa.types.is_nested(field.type))] * _leaves(field.type)
            cols.setdefault(field.name, _Column())
        for g in range(meta.num_row_groups):
            rg = meta.row_group(g)
            for i, (name, flat) in enumerate(owners):
                col = cols[name]
                chunk = rg.column(i)
                if chunk.compression not in codecs:
                    codecs.append(chunk.compression)
                col.flat = col.flat and flat
                # A column of several leaves is counted once per row group.
                if i == 0 or owners[i - 1][0] != name:
                    col.chunk(chunk, rg.num_rows)
                else:
                    col.bytes += chunk.total_compressed_size
                    col.raw_bytes += chunk.total_uncompressed_size
        rows += meta.num_rows
        groups += meta.num_row_groups
    return {
        "rows": rows,
        "files": len(files),
        "row_groups": groups,
        "bytes": size,
        "uncompressed_bytes": sum(c.raw_bytes for c in cols.values()),
        "compression": codecs,
        "created_by": writers,
        "columns": {name: c.stats(rows) for name, c in cols.items()},
    }
