"""What a finished run looks like to an agent: status, the error and log tail on
failure, and the output's schema and first rows on success.

Shared by the built-in agent's ``run_node`` tool and the REST run routes
(``wait``), so an outside agent driving exuvia over HTTP sees exactly what the
built-in one sees.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .workspace import Workspace

LOG_TAIL_LINES = 40


def _log_tail(ws: Workspace, run: dict[str, Any] | None) -> str:
    full = ws.store.run(run["id"]) if run and run.get("id") else None
    return "\n".join(((full or {}).get("log") or "").splitlines()[-LOG_TAIL_LINES:])


async def node_report(
    ws: Workspace, target: str, params: dict[str, Any], finals: dict[str, Any], *, head: int = 8
) -> tuple[bool, dict[str, Any]]:
    """Report on ``target`` after ``ws.run`` returned ``finals``. Returns
    (succeeded, report)."""
    run = finals.get(target) or ws.store.last_run(target, ws.node_params(target, params)[1])
    summary = {n: (r or {}).get("status") for n, r in finals.items()}
    if not run or run.get("status") != "success":
        return False, {
            "runs": summary,
            "run_id": (run or {}).get("id"),
            "error": (run or {}).get("error"),
            "log_tail": _log_tail(ws, run),
        }
    # Off the loop: reading an output may download it first.
    preview = await asyncio.to_thread(ws.preview, target, limit=head, params=params)
    out: dict[str, Any] = {"runs": summary, "run_id": run.get("id"), "rows": run.get("rows"), "schema": run.get("schema")}
    if run.get("params"):
        out["run_params"] = run["params"]
    if preview.get("kind") == "table":
        cols = [c["name"] for c in preview["columns"]]
        out["head"] = [dict(zip(cols, r)) for r in preview["rows"]]
    else:
        out["value"] = preview.get("value")
    return True, out


def graph_report(ws: Workspace, finals: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Report on a multi-node run: every node's status, and the error and log
    tail of each one that did not succeed."""
    summary = {n: (r or {}).get("status") for n, r in finals.items()}
    failed = [
        {"node": n, "run_id": (r or {}).get("id"), "error": (r or {}).get("error"), "log_tail": _log_tail(ws, r)}
        for n, r in finals.items()
        if (r or {}).get("status") != "success"
    ]
    return not failed, {"runs": summary, "failed": failed}
