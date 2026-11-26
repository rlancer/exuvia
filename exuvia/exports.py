"""Downloads: a table as CSV, Excel, Parquet or JSON, and code as a zip.

Every table export starts from an ``ArrowStream`` (a node's output from
``Workspace.preview_arrow``, or a SQL result from ``query_arrow`` and
``view_query_arrow``), so each format is written batch by batch from the same
typed columns. CSV and JSON stream as they are written. Parquet and Excel need
the whole file before its first byte is useful (Parquet's footer, the xlsx
zip), so they are written to a temporary file, served, then deleted.

The code archive is an allow-list of the project's own files (nodes, ``lib/``
helpers, views and the few root files that describe the project), never
``.env``, ``.exuvia/`` (runs, outputs, caches) or a virtual environment.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import tempfile
import zipfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import quote

from starlette.background import BackgroundTask
from starlette.responses import FileResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from .arrowstream import ArrowStream
    from .project import Project

# format -> (media type, file extension)
TABLE_FORMATS: dict[str, tuple[str, str]] = {
    "csv": ("text/csv; charset=utf-8", "csv"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "parquet": ("application/vnd.apache.parquet", "parquet"),
    "json": ("application/json", "json"),
}
# One sheet holds 1,048,576 rows; the header takes one.
EXCEL_MAX_ROWS = 1_048_575
EXCEL_MAX_CHARS = 32_767  # per cell
# The project's root files that belong in a copy of its code (when present).
ROOT_FILES = ("exuvia.json", "pyproject.toml", "uv.lock", "package.json", "AGENTS.md", "README.md", ".gitignore", ".python-version")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ExportError(ValueError):
    """An export that cannot be made; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def check_format(fmt: str) -> str:
    fmt = (fmt or "").lower()
    if fmt not in TABLE_FORMATS:
        raise ExportError(f"format must be one of {', '.join(TABLE_FORMATS)}, not {fmt!r}")
    return fmt


def slug(text: Any, fallback: str = "export") -> str:
    """A file-name-safe version of ``text``: letters, digits, ``.``, ``_``, ``-``."""
    out = _UNSAFE.sub("-", str(text)).strip("-.")
    return out[:120] or fallback


def file_stem(name: str, part: str | None = None, params: dict[str, Any] | None = None) -> str:
    """``prices``, ``fit.coefs``, ``prices-2026-11-20``: what a node's export is called.
    The run's parameter values tell one download of a node from another."""
    stem = f"{name}.{part}" if part else name
    values = [slug(v, "") for _, v in sorted((params or {}).items()) if v not in (None, "")]
    return "-".join([stem, *[v for v in values if v]])


def disposition(filename: str) -> str:
    """A ``Content-Disposition`` that saves the answer as ``filename``."""
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "").replace("?", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _headers(filename: str) -> dict[str, str]:
    return {"Content-Disposition": disposition(filename), "Cache-Control": "no-store"}


# -- tables ------------------------------------------------------------------


def table_response(stream: "ArrowStream", fmt: str, stem: str) -> Response:
    """The table ``stream`` reads, as a download in ``fmt``. Raises
    ExportError (413) when it has more rows than one Excel sheet holds."""
    fmt = check_format(fmt)
    media, ext = TABLE_FORMATS[fmt]
    filename = f"{slug(stem)}.{ext}"
    if fmt == "csv":
        return StreamingResponse(_csv(stream), media_type=media, headers=_headers(filename))
    if fmt == "json":
        return StreamingResponse(_json_rows(stream), media_type=media, headers=_headers(filename))
    path = _temp(ext)
    try:
        if fmt == "parquet":
            _parquet(stream, path)
        else:
            _xlsx(stream, path, sheet=stem)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return FileResponse(path, media_type=media, headers=_headers(filename), background=BackgroundTask(path.unlink, missing_ok=True))


def json_response(value: Any, stem: str) -> Response:
    """A JSON output (not a table) as a ``.json`` download."""
    body = json.dumps(value, indent=2, default=str, ensure_ascii=False).encode()
    return Response(body, media_type=TABLE_FORMATS["json"][0], headers=_headers(f"{slug(stem)}.json"))


def file_response(path: Path, media: str, filename: str) -> Response:
    """A stored file as a download, byte for byte."""
    return FileResponse(path, media_type=media, headers=_headers(filename))


def _temp(ext: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="exuvia-export-", suffix=f".{ext}")
    os.close(fd)
    return Path(name)


def _plain_schema(schema: Any) -> Any:
    """``schema`` without exuvia's metadata, with each column CSV cannot
    write as text (see ``_flatten``)."""
    import pyarrow as pa

    return pa.schema([pa.field(f.name, pa.string()) if _needs_text(f.type) else f.remove_metadata() for f in schema])


def _needs_text(t: Any) -> bool:
    """Whether a column has to become text first: every type but the plain
    ones Arrow's CSV writer handles (lists, structs, maps, intervals,
    durations, binary, ...)."""
    import pyarrow as pa

    if pa.types.is_dictionary(t):
        return _needs_text(t.value_type)
    plain = (
        pa.types.is_null,
        pa.types.is_boolean,
        pa.types.is_integer,
        pa.types.is_floating,
        pa.types.is_decimal,
        pa.types.is_string,
        pa.types.is_large_string,
        pa.types.is_date,
        pa.types.is_time,
        pa.types.is_timestamp,
    )
    return not any(check(t) for check in plain)


def _interval(v: Any) -> str:
    """A month-day-nanosecond interval as people write it: ``1 month 2 days 01:30:00``."""
    months, days, ns = v.months, v.days, v.nanoseconds
    out = [f"{n} {unit}{'' if abs(n) == 1 else 's'}" for n, unit in ((months, "month"), (days, "day")) if n]
    if ns or not out:
        sign = "-" if ns < 0 else ""
        secs, frac = divmod(abs(ns), 1_000_000_000)
        clock = f"{sign}{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
        out.append(clock + (f".{frac:09d}".rstrip("0") if frac else ""))
    return " ".join(out)


def _is_interval(v: Any) -> bool:
    return type(v).__name__ == "MonthDayNano"


def _text(v: Any) -> str:
    """One value of a column ``_needs_text`` picked: nested values as JSON."""
    if _is_interval(v):
        return _interval(v)
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(_clean(v), default=str, ensure_ascii=False)
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


def _flatten(batch: Any, schema: Any) -> Any:
    """``batch`` with each column ``_needs_text`` picked as text: CSV has no
    nested values, and neither has an Excel cell."""
    import pyarrow as pa

    cols = []
    for col, field in zip(batch.columns, schema, strict=True):
        if field.type == pa.string() and col.type != pa.string() and _needs_text(col.type):
            cols.append(pa.array([None if v is None else _text(v) for v in col.to_pylist()], pa.string()))
        else:
            cols.append(col)
    return pa.RecordBatch.from_arrays(cols, schema=schema)


def _drain(sink: io.BytesIO) -> bytes:
    data = sink.getvalue()
    sink.seek(0)
    sink.truncate()
    return data


def _csv(stream: "ArrowStream") -> Iterator[bytes]:
    import pyarrow.csv as pacsv

    schema = _plain_schema(stream.schema)
    sink = io.BytesIO()
    with pacsv.CSVWriter(sink, schema) as writer:
        for batch in stream.batches():
            writer.write_batch(_flatten(batch, schema))
            yield _drain(sink)
        if not sink.getvalue():  # no rows at all: still write the header
            writer.write_table(schema.empty_table())
    yield _drain(sink)


def _clean(v: Any) -> Any:
    """``v`` as JSON can hold it: NaN and infinity become null."""
    if _is_interval(v):
        return _interval(v)
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return v.total_seconds()
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _json_rows(stream: "ArrowStream") -> Iterator[bytes]:
    """An array of ``{column: value}`` objects, one batch at a time."""
    yield b"["
    first = True
    for batch in stream.batches():
        out = []
        for row in batch.to_pylist():
            out.append(("\n" if first else ",\n") + json.dumps(_clean(row), default=str, ensure_ascii=False))
            first = False
        yield "".join(out).encode()
    yield b"\n]\n"


def _parquet(stream: "ArrowStream", path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    # Parquet has no month-day-nanosecond interval: those columns go as text.
    schema = pa.schema(
        [pa.field(f.name, pa.string()) if pa.types.is_interval(f.type) else f.remove_metadata() for f in stream.schema]
    )
    with pq.ParquetWriter(path, schema) as writer:
        for batch in stream.batches():
            writer.write_batch(_flatten(batch, schema))


def _sheet_name(stem: str) -> str:
    name = re.sub(r"[\[\]:*?/\\]", "-", stem).strip("'") or "data"
    return name[:31]


def _xlsx(stream: "ArrowStream", path: Path, sheet: str) -> None:
    """One sheet: a bold, frozen header row, then typed cells (numbers stay
    numbers, dates stay dates). Text is always written as text, never as a
    formula or a link, whatever it starts with."""
    import xlsxwriter

    schema = _plain_schema(stream.schema)
    wb = xlsxwriter.Workbook(
        str(path),
        {
            "constant_memory": True,
            "remove_timezone": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
            "strings_to_numbers": False,
        },
    )
    try:
        ws = wb.add_worksheet(_sheet_name(sheet))
        bold = wb.add_format({"bold": True})
        fmt_date = wb.add_format({"num_format": "yyyy-mm-dd"})
        fmt_datetime = wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        fmt_time = wb.add_format({"num_format": "hh:mm:ss"})
        fmt_duration = wb.add_format({"num_format": "[h]:mm:ss"})
        for c, field in enumerate(schema):
            ws.set_column(c, c, min(max(len(field.name) + 2, 10), 50))
            ws.write_string(0, c, field.name, bold)
        ws.freeze_panes(1, 0)
        r = 0
        for batch in stream.batches():
            if r + batch.num_rows > EXCEL_MAX_ROWS:
                raise ExportError(
                    f"too many rows for Excel (one sheet holds {EXCEL_MAX_ROWS:,}): download it as CSV or Parquet",
                    status=413,
                )
            cols = [col.to_pylist() for col in _flatten(batch, schema).columns]
            for i in range(batch.num_rows):
                r += 1
                for c, values in enumerate(cols):
                    v = values[i]
                    if v is None:
                        continue
                    if isinstance(v, bool):
                        ws.write_boolean(r, c, v)
                    elif isinstance(v, int):
                        # Excel numbers are doubles: a bigger integer would lose digits.
                        if abs(v) < 2**53:
                            ws.write_number(r, c, v)
                        else:
                            ws.write_string(r, c, str(v))
                    elif isinstance(v, float):
                        if not (math.isnan(v) or math.isinf(v)):
                            ws.write_number(r, c, v)
                    elif isinstance(v, Decimal):
                        ws.write_number(r, c, float(v))
                    elif isinstance(v, datetime):
                        ws.write_datetime(r, c, v, fmt_datetime)
                    elif isinstance(v, date):
                        ws.write_datetime(r, c, v, fmt_date)
                    elif isinstance(v, time):
                        ws.write_datetime(r, c, v, fmt_time)
                    elif isinstance(v, timedelta):
                        ws.write_datetime(r, c, v, fmt_duration)
                    elif isinstance(v, bytes):
                        ws.write_string(r, c, v.hex()[:EXCEL_MAX_CHARS])
                    else:
                        ws.write_string(r, c, str(v)[:EXCEL_MAX_CHARS])
    finally:
        wb.close()


# -- code --------------------------------------------------------------------


def code_files(project: "Project", graph: Any, node: str | None = None, view: str | None = None) -> list[str]:
    """The project-relative paths a code archive holds: every node, helper,
    view and root project file; with ``node``, that node and the helpers it
    imports; with ``view``, that view's files. Raises KeyError for a node or
    view that is not there."""
    from .project import VIEW_DIR

    if node is not None:
        path = project.path_for(node)
        if path is None:
            raise KeyError(node)
        found = graph.nodes.get(node)
        return [path.relative_to(project.root).as_posix(), *(found.helpers if found else [])]
    if view is not None:
        if view not in project.view_names():
            raise KeyError(view)
        return [f"{VIEW_DIR}/{view}/{rel}" for rel in project.view_files(view)]
    out = [name for name in ROOT_FILES if (project.root / name).is_file()]
    out += [p.relative_to(project.root).as_posix() for p in project.node_files()]
    out += project.helper_files()
    out += [rel for _, rel in project.all_view_files()]
    return out


def code_archive(project: "Project", graph: Any, node: str | None = None, view: str | None = None) -> tuple[bytes, str]:
    """A zip of ``code_files`` under one folder named after the project (or
    the node or view), and the file name to save it as."""
    base = slug(project.name, "project")
    folder = base if node is None and view is None else f"{base}-{slug(node or view)}"
    root = project.root.resolve()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in dict.fromkeys(code_files(project, graph, node=node, view=view)):
            path = (project.root / rel).resolve()
            # A symlink in the project must not carry a file from elsewhere into the archive.
            if not path.is_file() or not path.is_relative_to(root):
                continue
            zf.write(path, f"{folder}/{rel}")
    return buf.getvalue(), f"{folder}.zip"


def zip_response(body: bytes, filename: str) -> Response:
    return Response(body, media_type="application/zip", headers=_headers(filename))
