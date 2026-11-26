"""HTTP helpers the workbench's API (server.py) and the hub's data API
(dataapi.py) share: request parsing for run parameters and partition
ranges, and the two ways a data answer goes out (FastJSON, an Arrow
stream)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .arrowstream import MEDIA_TYPE as ARROW_STREAM, ArrowStream
from .workspace import Workspace


class FastJSON(JSONResponse):
    """JSON written straight from an answer whose values are already JSON
    (``Workspace._jsonable``), skipping FastAPI's ``jsonable_encoder``, which
    walks every value again and took over half of a large answer's time.
    The bytes are the same as a plain JSONResponse's for such an answer."""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=str).encode()


def arrow_response(stream: ArrowStream) -> StreamingResponse:
    return StreamingResponse(stream, media_type=ARROW_STREAM, headers={"Cache-Control": "no-store"})


def query_params(raw: str | None) -> dict[str, Any] | None:
    """GET routes take run parameters as a JSON object in ``?params=``."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(400, "`params` must be a JSON object") from e
    if not isinstance(value, dict):
        raise HTTPException(400, "`params` must be a JSON object")
    return value


def resolve_params(ws: Workspace, params: dict[str, Any] | None) -> dict[str, Any]:
    """The run parameters ``params`` name, with defaults; 400 when they are bad."""
    try:
        return ws.resolve_params(params)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def partition_range(raw: str | None) -> tuple[str | None, str | None] | None:
    """``?partitions=`` on a partitioned output's data and export routes:
    ``lo..hi`` (either end may be left out) or a single value."""
    if raw is None or raw == "":
        return None
    lo, sep, hi = raw.partition("..")
    return (lo or None, hi or None) if sep else (raw, raw)


def need_partitioned(ws: Workspace, name: str, partitions: Any, run_id: str | None) -> None:
    """``?partitions=`` only means something for a partitioned node: say so
    rather than answer with every row."""
    node = ws.graph.nodes.get(name)
    if partitions is not None and run_id is None and node is not None and not node.partition_by:
        raise HTTPException(400, f"{name} is not partitioned: leave out partitions")
