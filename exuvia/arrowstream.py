"""Query results as an Arrow IPC stream.

The JSON answers of ``POST /api/query`` and ``GET /api/nodes/{name}/data``
are rows of text, capped at a few thousand. A client that asks for
``Accept: application/vnd.apache.arrow.stream`` gets the same result as an
Arrow IPC stream instead: typed columns, no row cap unless it sets one, and
written batch by batch so the server never holds the whole result.

What the JSON answer carries besides the rows (the DuckDB column types, and
for a node's output its run id, parameters and row count) travels in the
stream's schema metadata under the ``exuvia`` key, as JSON.
"""

from __future__ import annotations

import io
import json
from typing import Any, Iterator

MEDIA_TYPE = "application/vnd.apache.arrow.stream"
METADATA_KEY = b"exuvia"
BATCH_ROWS = 50_000


def wants_arrow(accept: str | None) -> bool:
    """Whether an ``Accept`` header asks for an Arrow stream."""
    return MEDIA_TYPE in (accept or "").lower()


def _reader(rel: Any) -> Any:
    """A pyarrow RecordBatchReader over a DuckDB relation."""
    return rel.to_arrow_reader(BATCH_ROWS)


class ArrowStream:
    """A DuckDB relation read as Arrow record batches; iterating it yields
    the bytes of an IPC stream.

    The query runs as far as its first batch on construction, so a statement
    that fails does so before any response has started. ``owner`` is whatever
    has to stay alive while the result is read (the DuckDB connection).
    """

    def __init__(self, rel: Any, meta: dict[str, Any] | None = None, owner: Any = None):
        import pyarrow as pa

        self._owner = owner
        columns = [{"name": c, "type": str(t)} for c, t in zip(rel.columns, rel.types)] if rel is not None else []
        tag = {METADATA_KEY: json.dumps({"columns": columns, **(meta or {})}, default=str).encode()}
        if rel is None:  # a statement with no result set
            self.schema = pa.schema([]).with_metadata(tag)
            self._batches: Iterator[Any] = iter(())
            self._first = None
            return
        reader = _reader(rel)
        self.schema = reader.schema.with_metadata(tag)
        self._batches = iter(reader)
        self._first = next(self._batches, None)

    def batches(self) -> Iterator[Any]:
        """The record batches themselves, for a writer other than Arrow IPC
        (``exports.py``). Like iterating the stream, this reads it once."""
        try:
            batch = self._first
            while batch is not None:
                yield batch
                batch = next(self._batches, None)
        finally:
            self._owner = None

    def __iter__(self) -> Iterator[bytes]:
        import pyarrow as pa

        sink = io.BytesIO()

        def written() -> bytes:
            data = sink.getvalue()
            sink.seek(0)
            sink.truncate()
            return data

        with pa.ipc.new_stream(sink, self.schema) as writer:
            yield written()  # the schema message
            batch = self._first
            while batch is not None:
                writer.write_batch(batch)
                yield written()
                batch = next(self._batches, None)
        yield written()  # end-of-stream marker
        self._owner = None
