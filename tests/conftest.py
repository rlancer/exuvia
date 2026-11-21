"""Test-wide settings.

No pre-warmed runner processes in tests. A Workspace keeps EXUVIA_MAX_PARALLEL
(4) runners warm and starts a replacement after every node run; each one
imports pandas, pyarrow and duckdb. Most tests run a node or two and throw
the Workspace away, so nearly all of those imports were wasted, and with one
pytest worker per core they took most of the CPU and about half the suite's
time. With an empty pool every run starts its runner on demand, through the
same `exuvia.runner --warm` path (exuvia/pool.py), so the runner code under
test is unchanged. The container job in CI (ci.yml) runs the image with the
real pool.
"""

from exuvia import pool

_init = pool.WarmPool.__init__


def _no_warm_workers(self, cwd, size):
    _init(self, cwd, 0)


pool.WarmPool.__init__ = _no_warm_workers
