"""Unique keys on node outputs.

A node declares ``key=["date", "ticker"]`` (SQL: ``-- key: date, ticker``)
and exuvia checks every output it would store, in DuckDB, before the run
counts as a success: no row may have a null in a key column, and no two rows
may share a key. A run that breaks that fails, and the node's previous output
stays, so a duplicate never reaches a downstream node or an analysis.

``on_duplicate="last"`` keeps, for each key, the last row in the order the
run returned them, for sources known to send a row again. Nulls still fail.

For a partitioned node the key holds across the whole output, not only the
rows one run returns: a row whose partition changed (a trade re-dated from
the 3rd to the 4th) must be returned with its old partition too, or the old
copy would stay behind. That is always an error, whatever ``on_duplicate``
says: exuvia can't tell which copy is right.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

ON_DUPLICATE = ("error", "last")
_ROW = "__exuvia_row"  # the row's position in the file it came from


class KeyViolation(ValueError):
    """An output that breaks its node's key."""


def key_columns(value: Any) -> list[str]:
    """``key``: one column name or a list of them."""
    if value is None or value == "" or value == []:
        return []
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, (list, tuple)) or not all(isinstance(n, str) for n in names):
        raise ValueError(f'`key` names the columns that identify a row, e.g. ["date", "ticker"], not {value!r}.')
    out = [n.strip() for n in names]
    if any(not n or len(n) > 128 or "\0" in n for n in out):
        raise ValueError(f'`key` names the columns that identify a row, e.g. ["date", "ticker"], not {value!r}.')
    if len(set(out)) != len(out):
        raise ValueError(f"`key` names each column once, not {value!r}.")
    return out


def on_duplicate_value(value: Any) -> str:
    if value not in ON_DUPLICATE:
        raise ValueError(f'`on_duplicate` is "error" (the default) or "last", not {value!r}.')
    return value


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _key_text(values: tuple[Any, ...]) -> str:
    return "(" + ", ".join(str(v) for v in values) + ")" if len(values) > 1 else str(values[0])


def check(con: Any, table: str, key: list[str], *, what: str = "the table the node returned") -> int:
    """Check the rows of ``table`` (a table or view on ``con``) against
    ``key``: every key column present and never null. Returns how many rows
    share their key with an earlier row (0 when the key holds)."""
    cols = [r[0] for r in con.execute(f"describe {table}").fetchall()]
    missing = [k for k in key if k not in cols]
    if missing:
        raise KeyViolation(
            f"the node's key is ({', '.join(key)}), but {what} has no "
            f"{', '.join(f'`{m}`' for m in missing)} column (it has {', '.join(cols) or 'none'})"
        )
    for k in key:
        (nulls,) = con.execute(f"select count(*) from {table} where {_ident(k)} is null").fetchone() or (0,)
        if nulls:
            some = "1 row" if nulls == 1 else f"{nulls} rows"
            raise KeyViolation(
                f"{some} in {what} {'has' if nulls == 1 else 'have'} no `{k}`: "
                f"every row needs a value in each key column ({', '.join(key)})"
            )
    cols_sql = ", ".join(_ident(k) for k in key)
    (extra,) = con.execute(
        f"select count(*) - count(distinct ({cols_sql})) from {table}"
    ).fetchone() or (0,)
    return int(extra)


def duplicates_error(con: Any, table: str, key: list[str], extra: int, *, what: str = "the table the node returned") -> KeyViolation:
    """The error for ``extra`` rows of ``table`` that repeat a key, with a few examples."""
    cols_sql = ", ".join(_ident(k) for k in key)
    rows = con.execute(
        f"select {cols_sql}, count(*) n from {table} group by all having count(*) > 1 order by n desc, {cols_sql} limit 3"
    ).fetchall()
    eg = ", ".join(f"{_key_text(tuple(r[:-1]))} ×{r[-1]}" for r in rows)
    some = "1 row repeats" if extra == 1 else f"{extra} rows repeat"
    return KeyViolation(
        f"{some} a key ({', '.join(key)}) in {what}, e.g. {eg}. Remove the duplicates in the node, "
        'or declare on_duplicate="last" to keep the last row for each key'
    )


def keep_last_sql(source: str, key: list[str]) -> str:
    """``source`` (a read_parquet with ``file_row_number``, aliased to
    ``_ROW``) with one row per key: the last one, in the original order."""
    cols_sql = ", ".join(_ident(k) for k in key)
    return (
        f"select * exclude ({_ROW}, __exuvia_n) from ("
        f"select *, row_number() over (partition by {cols_sql} order by {_ROW} desc) __exuvia_n from {source}"
        f") where __exuvia_n = 1 order by {_ROW}"
    )


def read_numbered(path: str) -> str:
    """A read of the Parquet file ``path`` with each row's position as ``_ROW``."""
    return f"(select * exclude (file_row_number), file_row_number {_ROW} from read_parquet({_lit(path)}, file_row_number = true))"


def enforce_file(path: str, key: list[str], on_duplicate: str) -> tuple[str, str, int, int] | None:
    """Check the Parquet output at ``path`` against ``key``. Raises
    KeyViolation when it breaks it. None when it holds; with
    ``on_duplicate="last"`` and repeated keys, writes the output with one row
    per key next to ``path`` and returns (its path, its artifact id, its
    rows, how many rows were dropped). Blocking."""
    import duckdb

    con = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
    try:
        con.execute(f"create temp view _out as select * from read_parquet({_lit(path)})")
        extra = check(con, "_out", key)
        if not extra:
            return None
        if on_duplicate != "last":
            raise duplicates_error(con, "_out", key, extra)
        out_dir = Path(path).parent
        fd, name = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
        os.close(fd)
        tmp = Path(name)
        try:
            con.execute(
                f"copy ({keep_last_sql(read_numbered(path), key)}) to {_lit(str(tmp))} (format parquet, compression zstd)"
            )
            (rows,) = con.execute(f"select count(*) from read_parquet({_lit(str(tmp))})").fetchone() or (0,)
            digest = hashlib.sha256(tmp.read_bytes()).hexdigest()[:16]
            final = out_dir / f"{digest}.parquet"
            if final.exists():
                tmp.unlink()
            else:
                tmp.replace(final)
            return str(final), digest, int(rows), extra
        finally:
            if tmp.exists():
                tmp.unlink()
    finally:
        con.close()


def _where_key(key: list[str], values: tuple[Any, ...]) -> tuple[str, list[Any]]:
    return " and ".join(f"{_ident(k)} = ?" for k in key), list(values)


def check_output(con: Any, paths: list[str], key: list[str], partition_by: str) -> None:
    """Raise KeyViolation when two rows of a partitioned output (its files
    ``paths``) share a key, naming the partitions each example sits in. A
    scan of the key columns: about 0.8 s for 5 million rows."""
    if not paths:
        return
    files = "[" + ", ".join(_lit(p) for p in paths) + "]"
    cols_sql = ", ".join(_ident(k) for k in key)
    rows = con.execute(
        f"select {cols_sql}, count(*) n from read_parquet({files}, union_by_name = true) "
        f"group by all having count(*) > 1 order by n desc, {cols_sql} limit 3"
    ).fetchall()
    if not rows:
        return
    examples = []
    for r in rows:
        where, args = _where_key(key, tuple(r[:-1]))
        (parts,) = con.execute(
            f"select list(distinct cast({_ident(partition_by)} as varchar) order by cast({_ident(partition_by)} as varchar)) "
            f"from read_parquet({files}, union_by_name = true) where {where}", args
        ).fetchone()
        examples.append(f"{_key_text(tuple(r[:-1]))} ×{r[-1]} in {', '.join(parts)}")
    raise KeyViolation(
        f"the output would hold a key ({', '.join(key)}) more than once, e.g. {'; '.join(examples)}. "
        f"A row whose `{partition_by}` changed must be returned with its old `{partition_by}` too, so "
        "that partition is replaced without it; or run a full refresh"
    )


def check_against(con: Any, new: str, paths: list[str], gone: str, key: list[str], partition_by: str) -> None:
    """Raise KeyViolation when a row of table ``new`` (one run's rows, which
    hold the key among themselves) has the key of a stored row that stays:
    one in the files ``paths`` whose partition isn't in table ``gone`` (the
    partitions the run replaces). Only a key without the partition column
    can repeat across partitions, so only such a key needs this."""
    if not paths:
        return
    files = "[" + ", ".join(_lit(p) for p in paths) + "]"
    cols_sql = ", ".join(_ident(k) for k in key)
    part = _ident(partition_by)
    rows = con.execute(
        f"select {', '.join('n.' + _ident(k) for k in key)}, cast(n.{part} as varchar), "
        f"list(distinct cast(o.{part} as varchar) order by cast(o.{part} as varchar)) "
        f"from {new} n join (select {cols_sql}, {part} from read_parquet({files}, union_by_name = true) "
        f"where cast({part} as varchar) not in (select v from {gone})) o using ({cols_sql}) "
        f"group by all order by all limit 3"
    ).fetchall()
    if not rows:
        return
    eg = "; ".join(f"{_key_text(tuple(r[:-2]))} returned in {r[-2]} and stored in {', '.join(r[-1])}" for r in rows)
    raise KeyViolation(
        f"the output would hold a key ({', '.join(key)}) more than once, e.g. {eg}. "
        f"A row whose `{partition_by}` changed must be returned with its old `{partition_by}` too, so "
        "that partition is replaced without it; or run a full refresh"
    )
