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
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version
from typing import Any, Callable

try:  # the installed package's version, which comes from its git tag
    __version__ = _version("exuvia")
except PackageNotFoundError:  # imported from a source tree that is not installed
    __version__ = "0.0.0"

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
    retries: int = 0,
    retry_delay: float = 30,
    overlap: str = "queue",
    timeout: float | None = None,
    push: bool = False,
    live: bool | float = False,
    partition_by: str | None = None,
    key: str | list[str] | None = None,
    on_duplicate: str = "error",
    merge: bool = False,
    runs_on: str | None = None,
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
        retries: how many more times to try a run that fails (0 to 10).
            Every attempt is in the same run, with one log.
        retry_delay: seconds before the first retry (default 30); each
            further retry waits twice as long as the one before.
        overlap: what a scheduled fire does while the node is still
            running: ``"queue"`` (default) runs it once that run ends,
            ``"skip"`` drops it.
        timeout: seconds an attempt may take before it is stopped and fails
            (1 to 86400); by default the server's ``EXUVIA_RUN_TIMEOUT``
            (1800). Set it on a node that reads from something that can
            hang, so a stuck attempt fails fast (and is retried, with
            ``retries``) instead of holding the node's queue.
        push: the node's output is uploaded from another machine
            (``exuvia push``, or :func:`exuvia.push` in a script there) and
            never computed by exuvia: for data only that machine has or can
            read, such as a licensed terminal's or another process's results. A push node takes no upstream
            nodes and has no schedule; its function is not run.
        runs_on: the label of the runners that run this node, instead of the
            project's executor: ``exuvia runner --label <label>`` on a
            person's own machine (a desktop logged in to a licensed data
            terminal, say), signed in as someone who can edit the project.
            It runs there only once that person has approved its code, and
            its output is sent back here. A ``runs_on`` node takes no
            upstream nodes and can't be live.
        live: re-run the node whenever a view that depends on it is opened
            (or its parameters change), so the view shows current data: it
            renders the last output at once, says it is refreshing, and
            updates when the new output lands. ``True`` re-runs every time;
            a number of seconds re-runs only when the output is older than
            that (``live=300``: at most every five minutes).
        partition_by: a column of the table the node returns. The node keeps
            one output that every run adds to: the rows a run returns
            replace the partitions (values of that column) they hold, and
            every other partition is kept. A daily run returns today's rows,
            a backfill years of them, a correction the day it fixes; ``None``
            or no rows changes nothing. :func:`exuvia.partitions` says what
            is stored already. Its own ``params`` don't split the output, so
            a backfill over ``as_of`` fills one history. A full refresh (run
            with ``full_refresh``) starts from an empty output.
        key: the column, or columns, that identify a row of the table the
            node returns, e.g. ``["date", "ticker"]``. Before an output is
            stored exuvia checks that no row has a null in them and no two
            rows share a key; a run that breaks that fails and the previous
            output stays. For a partitioned node the key holds across the
            whole output, not only one run's rows.
        on_duplicate: ``"error"`` (default) fails a run whose rows repeat a
            key; ``"last"`` keeps the last row for each key, in the order
            returned, for sources known to send a row twice.
        merge: for a partitioned node with a ``key``: the rows a run returns
            update or insert, by key, into the partitions they hold, instead
            of replacing them, and the stored rows they don't name stay. For
            sources that send part of a day at a time. A merge never removes
            a row: a full refresh rebuilds the output from what the run
            returns.

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
            "retries": retries,
            "retry_delay": retry_delay,
            "overlap": overlap,
            "push": push,
            "live": live,
            "partition_by": partition_by,
            "key": key,
            "on_duplicate": on_duplicate,
            "merge": merge,
            "runs_on": runs_on,
        }
        return f

    return wrap(fn) if fn is not None else wrap


def push(url: str, node: str, value: Any, **kw: Any) -> dict[str, Any]:
    """Upload ``value`` as push node ``node``'s output to the exuvia at ``url``
    (``<hub>/p/<project>``, or an ``exuvia serve`` address). ``value`` is a
    DataFrame (pandas, polars or pyarrow), a path to a .parquet, .csv or
    .json file, or a JSON-able value. Returns the run it recorded; see
    ``exuvia.pushing.push`` for the keyword arguments."""
    from .pushing import push as _push

    return _push(url, node, value, **kw)


def partitions() -> list[str]:
    """Inside a partitioned node's run (``@exuvia.node(partition_by=...)``):
    the partition values its output already holds, as text, in order (ISO
    dates sort as dates, numbers by value). Empty on the first run and on a
    full refresh, when the node should return everything. Outside a node
    run (a REPL, a unit test) it is empty."""
    rt = _runtime
    if rt is None:
        return []
    if "partitions" not in rt:
        raise RuntimeError(
            f"node `{rt['node']}` is not partitioned: exuvia.partitions() is for @exuvia.node(partition_by=...)."
        )
    return list(rt["partitions"])


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
