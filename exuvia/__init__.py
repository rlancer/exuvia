"""exuvia — an AI-first DAG workbench.

A exuvia project is a folder of node files under ``nodes/``. Each file is one
node; its upstream dependencies are the function's parameter names (Python) or
the node names referenced as tables (SQL). The graph is read statically, so a
node with a syntax error still shows up in the graph.

    import exuvia

    @exuvia.node(schedule="0 17 * * 1-5")
    def daily_returns(prices):
        '''Log returns per ticker.'''
        ...

Run parameters (declared in ``exuvia.json``) make a run reproducible: a node
that lists ``params=["as_of"]`` reads the value with ``exuvia.param("as_of")``,
and its outputs and history are kept per parameter value.
"""

from __future__ import annotations

import time as _time
from typing import Any, Callable

__version__ = "0.2.0"

# When this process imported exuvia (monotonic). The server reports the time
# since then as `uptime_s` on /healthz, so a host that starts it can tell its
# own start-up time apart from the container's or VM's.
_STARTED = _time.monotonic()

# Set by the runner inside a node process: {"node": str, "params": {name: value}}.
# ``params`` holds only the parameters the running node declared.
_runtime: dict[str, Any] | None = None
_MISSING: Any = object()


def node(
    fn: Callable[..., Any] | None = None,
    *,
    schedule: str | None = None,
    cascade: bool = True,
    chart: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    params: list[str] | None = None,
) -> Any:
    """Mark a function as a exuvia node.

    Args:
        schedule: cron expression (project timezone). The node is re-run on
            that cadence, even if its inputs did not change.
        cascade: when an upstream node produces new output, re-run this node
            automatically. Set False to only refresh on demand/schedule.
        chart: a Vega-Lite spec (without ``data``) the UI renders against the
            node's output table.
        tags: free-form labels shown in the UI.
        params: names of the run parameters this node reads with
            ``exuvia.param``. A node's output is stored per value of the
            parameters it (or anything upstream of it) declares.

    The decorator is a no-op at runtime; exuvia reads these keyword arguments
    from the source with ``ast`` so they must be literals.
    """

    def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
        f.__exuvia__ = {  # type: ignore[attr-defined]
            "schedule": schedule,
            "cascade": cascade,
            "chart": chart,
            "tags": tags or [],
            "params": params or [],
        }
        return f

    return wrap(fn) if fn is not None else wrap


def param(name: str, default: Any = _MISSING) -> Any:
    """The value of run parameter ``name`` inside a running node.

    The node must declare it (``@exuvia.node(params=["as_of"])``, or
    ``-- params: as_of`` in SQL) so exuvia knows which outputs depend on it.
    ``date`` parameters arrive as ISO ``YYYY-MM-DD`` strings. Outside a node
    run (a REPL, a unit test) the ``default`` is returned.
    """
    rt = _runtime
    if rt is None:
        if default is _MISSING:
            raise RuntimeError("exuvia.param() has no value outside a node run; pass a default.")
        return default
    if name not in rt["params"]:
        raise KeyError(
            f"node `{rt['node']}` reads parameter `{name}` without declaring it. "
            f'Add params=["{name}"] to @exuvia.node (SQL: `-- params: {name}`).'
        )
    value = rt["params"][name]
    return default if value is None and default is not _MISSING else value
