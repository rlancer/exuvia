"""What a finished run looks like to an agent: status, the error and log tail on
failure, and the output's schema and first rows on success.

Shared by the built-in agent's ``run_node`` tool and the REST run routes
(``wait``), so an outside agent driving exuvia over HTTP sees exactly what the
built-in one sees.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .packages import missing_package
from .workspace import OK, Workspace

LOG_TAIL_LINES = 40
PRINTED_LINES = 30
_TRACEBACK = "Traceback (most recent call last):"
_CHAINED = ("During handling of the above exception", "The above exception was the direct cause")


def log_tail(log: str, lines: int = LOG_TAIL_LINES) -> dict[str, Any]:
    """``{"log_tail": the last lines of a run's log}``, plus ``printed``: the
    lines the node printed just before its last traceback, when that
    traceback is long enough to push them out of the tail (a failure deep in
    a database driver, say), so what the node said it was doing is kept."""
    every = log.splitlines()
    out: dict[str, Any] = {"log_tail": "\n".join(every[-lines:])}
    start = next((i for i in range(len(every) - 1, -1, -1) if every[i].startswith(_TRACEBACK)), -1)
    # A chained exception's tracebacks are one block: start at its first.
    while start > 0:
        above = next((i for i in range(start - 1, -1, -1) if every[i].strip()), -1)
        if above < 0 or not every[above].startswith(_CHAINED):
            break
        first = next((i for i in range(above - 1, -1, -1) if every[i].startswith(_TRACEBACK)), -1)
        if first < 0:
            break
        start = first
    if start > 0 and start - 1 < len(every) - lines:  # the line before it is not in the tail
        before = every[:start][-PRINTED_LINES:]
        if any(ln.strip() for ln in before):
            out["printed"] = "\n".join(before)
    return out


def _log_tail(ws: Workspace, run: dict[str, Any] | None) -> dict[str, Any]:
    full = ws.store.run(run["id"]) if run and run.get("id") else None
    return log_tail((full or {}).get("log") or "")


async def node_report(
    ws: Workspace, target: str, params: dict[str, Any], finals: dict[str, Any], *, head: int = 8
) -> tuple[bool, dict[str, Any]]:
    """Report on ``target`` after ``ws.run`` returned ``finals``. Returns
    (succeeded, report)."""
    run = finals.get(target) or ws.store.last_run(target, ws.node_params(target, params)[1])
    summary = {n: (r or {}).get("status") for n, r in finals.items()}
    if not run or run.get("status") not in OK:
        out = {
            "runs": summary,
            "run_id": (run or {}).get("id"),
            "error": (run or {}).get("error"),
            **_log_tail(ws, run),
        }
        missing = missing_package(out["error"])
        if missing:
            out["missing_package"] = missing
            out["hint"] = (
                f"`{missing['module']}` is not installed. If {missing['package']} is the package that provides it, "
                "ask for it with request_packages, then re-run this node once it is installed."
            )
        return False, out
    # Off the loop: reading an output may download it first.
    preview = await asyncio.to_thread(ws.preview, target, limit=head, params=params)
    # A cached run made nothing: the output's shape is the run that made it.
    made = run
    if run.get("status") == "cached":
        made = ws.store.last_success(target, ws.node_params(target, params)[1]) or run
    out: dict[str, Any] = {"runs": summary, "run_id": run.get("id"), "rows": made.get("rows"), "schema": made.get("schema")}
    if run.get("params"):
        out["run_params"] = run["params"]
    if preview.get("kind") == "multi":
        # Several outputs: each one's first rows (or value), by name.
        out["parts"] = {}
        for part in preview.get("parts") or []:
            one = await asyncio.to_thread(ws.preview, target, limit=head, params=params, part=part["name"])
            out["parts"][part["name"]] = _shown(one)
    else:
        out.update(_shown(preview))
    return True, out


def _shown(preview: dict[str, Any]) -> dict[str, Any]:
    if preview.get("kind") == "table":
        cols = [c["name"] for c in preview["columns"]]
        return {"head": [dict(zip(cols, r)) for r in preview["rows"]]}
    return {"value": preview.get("value")}


def graph_report(ws: Workspace, finals: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Report on a multi-node run: every node's status, and the error and log
    tail of each one that did not succeed."""
    summary = {n: (r or {}).get("status") for n, r in finals.items()}
    failed = [
        {"node": n, "run_id": (r or {}).get("id"), "error": (r or {}).get("error"), **_log_tail(ws, r)}
        for n, r in finals.items()
        if (r or {}).get("status") not in OK
    ]
    return not failed, {"runs": summary, "failed": failed}
