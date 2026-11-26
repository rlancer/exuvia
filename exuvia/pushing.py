"""Push nodes: output uploaded from another machine instead of computed.

Some data can only be read from one person's machine: a Bloomberg
Terminal's Desktop API answers on that machine's ``localhost``, logged in as
that person, so no hub or executor can pull from it. A node marked
``@exuvia.node(push=True)`` stands for that data in the graph, and a script
on the machine that has it uploads each new version:

    exuvia push bbg_prices prices.parquet --url https://hub.example.com/p/research

or, from Python there, ``exuvia.push(url, "bbg_prices", df)``. The hub
records each push as a run of the node (trigger ``push``), so freshness, the
cascade downstream, run history and views work as for any other node.

The client half (``push``) needs only pandas, pyarrow and httpx, which come
with exuvia. ``save_pushed`` is the server half: it reads what arrived and
stores it the way a node's return value is stored, so a push of the same
data makes the same output and stops the cascade.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

FORMATS = {"parquet": "application/vnd.apache.parquet", "json": "application/json"}
# Content types a client might send for each format, besides the ones above.
_TYPES = {
    "application/vnd.apache.parquet": "parquet",
    "application/x-parquet": "parquet",
    "application/parquet": "parquet",
    "application/json": "json",
}
# Biggest upload the server takes, in MB (EXUVIA_PUSH_MAX_MB).
DEFAULT_MAX_MB = 1024


class PushError(RuntimeError):
    """The push did not happen, or the data it sent could not be read."""


def max_bytes() -> int:
    try:
        mb = float(os.environ.get("EXUVIA_PUSH_MAX_MB", "") or DEFAULT_MAX_MB)
    except ValueError:
        mb = DEFAULT_MAX_MB
    return int(mb * 1024 * 1024)


def format_of(fmt: str | None, content_type: str | None) -> str:
    """The format of an upload: ``fmt`` when given, else from its content
    type. ValueError when neither names parquet or JSON."""
    if fmt:
        if fmt not in FORMATS:
            raise ValueError(f"format must be parquet or json, not {fmt!r}")
        return fmt
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _TYPES:
        return _TYPES[ct]
    raise ValueError(
        "send the data as parquet (Content-Type: application/vnd.apache.parquet) or JSON "
        "(application/json), or say which with ?format=parquet|json"
    )


# -- the hub's side ---------------------------------------------------------------------
def save_pushed(path: Path, fmt: str, out_dir: Path) -> dict[str, Any]:
    """Read an uploaded file and store it as a node output, content-addressed
    exactly as a node's return value is (runner.save_artifact). Blocking."""
    from .runner import save_artifact

    if fmt == "parquet":
        import pandas as pd

        value: Any = pd.read_parquet(path)
    elif fmt == "json":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if value is None:
            raise ValueError("the JSON is null: nothing to store")
    else:
        raise ValueError(f"format must be parquet or json, not {fmt!r}")
    return save_artifact(value, out_dir)


# -- the pushing machine's side ---------------------------------------------------------
def encode(value: Any) -> tuple[bytes, str]:
    """``value`` as the bytes to upload and their format. A path is read by
    its suffix (.parquet as it is, .csv into parquet, .json as JSON); a
    DataFrame (pandas, polars, pyarrow) becomes parquet; anything else JSON."""
    from .runner import _as_frame, _frame_for_parquet, _parquet_bytes, _parts_of, _to_json

    if isinstance(value, (str, os.PathLike)):
        path = Path(value).expanduser()
        suffix = path.suffix.lower()
        if suffix not in (".parquet", ".pq", ".csv", ".json"):
            raise PushError(f"{path}: push a .parquet, .csv or .json file, or a DataFrame from Python")
        if not path.is_file():
            raise PushError(f"no file {path}")
        if suffix == ".csv":
            import pandas as pd

            return _parquet_bytes(_frame_for_parquet(pd.read_csv(path))), "parquet"
        data = path.read_bytes()
        if suffix == ".json":
            try:
                json.loads(data)
            except ValueError as e:
                raise PushError(f"{path} is not JSON: {e}") from e
            return data, "json"
        return data, "parquet"
    frame = _as_frame(value)
    if frame is not None:
        return _parquet_bytes(_frame_for_parquet(frame.copy())), "parquet"
    if _parts_of(value) is not None:
        raise PushError("a push is one table or one JSON value: push each table to its own push node")
    if value is None:
        raise PushError("nothing to push: the value is None")
    return _to_json(value).encode("utf-8"), "json"


def _token(token: str | None) -> str | None:
    if token:
        return token
    if os.environ.get("EXUVIA_API_TOKEN"):
        return os.environ["EXUVIA_API_TOKEN"]
    path = os.environ.get("EXUVIA_API_TOKEN_FILE")
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip() or None
        except OSError as e:
            raise PushError(f"EXUVIA_API_TOKEN_FILE: cannot read {path}: {e.strerror or e}") from e
    return None


def push(
    url: str,
    node: str,
    value: Any,
    *,
    params: dict[str, Any] | None = None,
    note: str | None = None,
    token: str | None = None,
    allow_http: bool = False,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Upload ``value`` as push node ``node``'s output to the exuvia at
    ``url``: a hub project (``<hub>/p/<project>``) or an ``exuvia serve``
    address. Returns the run the hub recorded.

    ``params``: the run parameters this output is for, when the node
    declares any (``{"as_of": "2026-11-20"}``). ``note``: a line for the
    run's log (where it came from, say). ``token``: the bearer token,
    by default ``EXUVIA_API_TOKEN`` (or the file ``EXUVIA_API_TOKEN_FILE``
    names): your own agent token from the workbench's account menu, or the
    server's. Plain http is refused unless the server is on this machine or
    ``allow_http``. Raises PushError when the push did not happen."""
    import httpx

    from .envcli import check_url
    from .nethttp import client_options, proxy_hint

    try:
        base = check_url(url, allow_http)
    except SystemExit as e:
        raise PushError(str(e)) from None
    data, fmt = encode(value)
    query: dict[str, str] = {"format": fmt}
    if params:
        query["params"] = json.dumps(params, default=str)
    if note:
        query["note"] = note[:500]
    headers = {"Content-Type": FORMATS[fmt], "X-Exuvia-Origin": "push"}
    tok = _token(token)
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    target = f"{base}/api/nodes/{quote(node, safe='')}/push"
    try:
        r = httpx.post(target, params=query, content=data, headers=headers, timeout=timeout, **client_options(base))
    except httpx.HTTPError as e:
        raise PushError(f"could not reach exuvia at {base} ({type(e).__name__}): is it running?{proxy_hint(base, e)}") from e
    if r.status_code == 401:
        raise PushError(
            f"{base} needs a token: set EXUVIA_API_TOKEN (or EXUVIA_API_TOKEN_FILE) to your own agent token "
            "from the workbench's account menu, or to the server's token"
        )
    if r.status_code >= 400:
        try:
            detail = str(r.json().get("detail", r.text))
        except ValueError:
            detail = r.text
        if r.status_code == 404 and not detail.startswith("no node"):
            detail = f"no exuvia project at {base}, or one too old to take pushes ({detail})"
        raise PushError(f"{base}: {detail}")
    return r.json()["run"]


def cancel_run(url: str, run_id: str, *, token: str | None = None, allow_http: bool = False) -> None:
    """Stop run ``run_id`` of the exuvia project at ``url`` (as for
    :func:`push`). Raises PushError when it could not be stopped."""
    import httpx

    from .envcli import check_url
    from .nethttp import client_options, proxy_hint

    try:
        base = check_url(url, allow_http)
    except SystemExit as e:
        raise PushError(str(e)) from None
    headers = {"X-Exuvia-Origin": "cli"}
    tok = _token(token)
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = httpx.post(
            f"{base}/api/runs/{quote(run_id, safe='')}/cancel", headers=headers, timeout=30.0, **client_options(base)
        )
    except httpx.HTTPError as e:
        raise PushError(f"could not reach exuvia at {base} ({type(e).__name__}): is it running?{proxy_hint(base, e)}") from e
    if r.status_code == 401:
        raise PushError(f"{base} needs a token: set EXUVIA_API_TOKEN (or EXUVIA_API_TOKEN_FILE)")
    if r.status_code >= 400:
        raise PushError(f"{base}: HTTP {r.status_code}: {r.text[:300]}")
    if not r.json().get("ok"):
        raise PushError(f"run {run_id} is not queued or running at {base}, so there is nothing to stop")
