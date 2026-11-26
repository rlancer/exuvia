"""Read the Lobster market-data lake from an exuvia node.

    from exuvia.connectors import lobster

    @exuvia.node(schedule="*/30 9-16 * * 1-5")
    def spy_chain():
        return lobster.query("select * from options.option_contracts where symbol = 'SPY' limit 5000")

    @exuvia.node()
    def nvda_brief():
        return lobster.get("/api/research/NVDA")      # JSON → dict

    @exuvia.node()
    def all_puts():                                   # no row cap: DuckDB over the Iceberg tables
        return lobster.lake_query("select * from lake.options.option_contracts where as_of_date = '2026-11-13' and type = 'put'")

Env:
    LOBSTER_API    base URL (default https://api.lobster.mp)
    LOBSTER_TOKEN  bearer token. Leave it unset when the base URL is a gateway
                   that attaches credentials itself (a host that keeps secrets
                   out of the process running node code).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_API = "https://api.lobster.mp"
TIMEOUT_S = 120


class LobsterError(RuntimeError):
    """The Lobster API refused a request or could not run a query."""


def _base() -> str:
    return os.environ.get("LOBSTER_API", DEFAULT_API).rstrip("/")


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    # A named client: the API's edge refuses urllib's default signature.
    headers = {"Accept": "application/json", "User-Agent": "exuvia-lobster-connector"}
    token = os.environ.get("LOBSTER_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{_base()}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        try:
            detail = json.loads(detail).get("error", detail)
        except (ValueError, AttributeError):
            pass
        raise LobsterError(f"{method} {path} -> HTTP {e.code}: {detail}") from None


def query(sql: str, limit: int = 10000, *, allow_truncated: bool = False) -> Any:
    """Run read-only lake SQL and return the rows as a DataFrame (max 10,000 rows).

    The API stops at ``limit`` rows and says so (``truncated``). A cut-off
    result is an arbitrary subset of the matching rows, so anything computed
    from it is silently wrong: this raises ``LobsterError`` instead. Aggregate
    or filter in the SQL, or pass ``allow_truncated=True`` when the first
    ``limit`` rows of an ordered result are genuinely all that is needed.
    """
    import pandas as pd

    payload = _request("POST", "/api/query", {"sql": sql, "limit": limit})
    if isinstance(payload, dict) and payload.get("error"):
        raise LobsterError(str(payload["error"]))
    if isinstance(payload, dict) and payload.get("truncated") and not allow_truncated:
        cap = payload.get("limit", limit)
        raise LobsterError(
            f"the query matched more than {cap} rows and the result was cut off, so rows are missing. "
            "Aggregate or filter in SQL (GROUP BY, a tighter WHERE, fewer symbols or days per call), "
            "or pass allow_truncated=True if the first rows of an ORDER BY are all you need."
        )
    rows = payload.get("rows", payload.get("data", payload)) if isinstance(payload, dict) else payload
    columns = payload.get("columns") if isinstance(payload, dict) else None
    # Keep the column names even when the query matched nothing.
    return pd.DataFrame(rows, columns=columns or None)


def get(path: str, **params: Any) -> Any:
    """GET a read-only Lobster JSON endpoint, e.g. ``get("/api/news", symbol="NVDA")``."""
    if not path.startswith("/"):
        raise ValueError("path must start with '/'")
    clean = {k: v for k, v in params.items() if v is not None}
    if clean:
        path = f"{path}?{urllib.parse.urlencode(clean)}"
    return _request("GET", path)


# -- direct reads -----------------------------------------------------------------
#
# The lake is a set of Iceberg tables. ``query`` runs SQL on the server and
# returns at most 10,000 rows; ``lake()`` attaches the tables to DuckDB in this
# process instead, so a node can scan and join whole partitions locally.
#
# Env:
#     LOBSTER_ICEBERG_ENDPOINT   Iceberg REST catalog URL (a host's gateway, or
#                                the real catalog for local use)
#     LOBSTER_ICEBERG_WAREHOUSE  catalog warehouse name
#     LOBSTER_ICEBERG_TOKEN      catalog token; leave unset behind a gateway
#                                that attaches it

LAKE_ALIAS = "lake"
_SAFE_LITERAL = re.compile(r"^[A-Za-z0-9_.:/@-]+$")
_lake_con: Any = None


def _connect() -> Any:  # seam for tests
    import duckdb

    return duckdb.connect()


def lake(threads: int = 16) -> Any:
    """A DuckDB connection with the lake attached read-only as ``lake``.

    Tables are ``lake.options.<table>``; filter on the date column so DuckDB
    reads only the files for those days::

        con = lobster.lake()
        df = con.sql("SELECT ... FROM lake.options.option_contracts WHERE as_of_date = '2026-11-13'").df()

    The connection is opened once per process and reused.
    """
    global _lake_con
    if _lake_con is not None:
        return _lake_con
    endpoint = os.environ.get("LOBSTER_ICEBERG_ENDPOINT", "").strip()
    warehouse = os.environ.get("LOBSTER_ICEBERG_WAREHOUSE", "").strip()
    if not endpoint or not warehouse:
        raise LobsterError(
            "direct lake reads are not configured here (LOBSTER_ICEBERG_ENDPOINT and LOBSTER_ICEBERG_WAREHOUSE); "
            "use lobster.query instead"
        )
    if not _SAFE_LITERAL.match(endpoint) or not _SAFE_LITERAL.match(warehouse):
        raise LobsterError("LOBSTER_ICEBERG_ENDPOINT / LOBSTER_ICEBERG_WAREHOUSE contain unexpected characters")
    # Behind a gateway the catalog token is attached at egress; DuckDB still
    # wants one to send.
    token = os.environ.get("LOBSTER_ICEBERG_TOKEN") or "attached-by-gateway"
    con = _connect()
    for ext in ("httpfs", "iceberg"):
        try:
            con.execute(f"LOAD {ext}")
        except Exception:  # noqa: BLE001 - not installed here yet (whatever error the connection raises for it)
            con.execute(f"INSTALL {ext}")  # local use; the workflows image ships them
            con.execute(f"LOAD {ext}")
    # Iceberg scans wait on object-store latency, not CPU.
    con.execute(f"SET threads = {int(threads)}")
    con.execute("CREATE OR REPLACE SECRET lobster_lake (TYPE ICEBERG, TOKEN $token)", {"token": token})
    con.execute(
        f"ATTACH '{warehouse}' AS {LAKE_ALIAS} "
        f"(TYPE ICEBERG, ENDPOINT '{endpoint}', SECRET lobster_lake, READ_ONLY)"
    )
    _lake_con = con
    return con


def lake_query(sql: str) -> Any:
    """Run DuckDB SQL against the attached lake (``lake.options.<table>``) and return a DataFrame."""
    return lake().sql(sql).df()
