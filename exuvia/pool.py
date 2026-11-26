"""A pool of pre-warmed, single-use runner processes.

Importing pandas/pyarrow/duckdb can take seconds (much longer on Windows with
on-access antivirus). Each idle worker has already paid that cost and is
blocked on stdin; ``take()`` hands one out and immediately starts warming a
replacement. Workers are never reused, so node isolation is unchanged. If the
parent dies, the workers' stdin closes and they exit on their own.

Workers start without exuvia's own settings (``node_environ``): node code,
which the agent may have written, has no use for the API or executor token,
the database URL or package index credentials, so it never sees them.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Mapping

# exuvia's own settings, which node code never needs: its tokens, database
# URL, failure webhook and Portkey gateway (EXUVIA_*, PORTKEY_*), and a package
# index's credentials, which only the installer uses. Each `_FILE` form too.
OWN_PREFIXES = ("EXUVIA_", "PORTKEY_")
INDEX_CREDENTIAL = re.compile(r"^UV_INDEX_[A-Z0-9_]+_(USERNAME|PASSWORD)(_FILE)?$")


def node_environ(base: Mapping[str, str] | None = None, *, index: bool = True) -> dict[str, str]:
    """``base`` (default: this process's environment) without exuvia's own
    settings: what a node's process starts from. ``index=False`` keeps package
    index credentials, for the installer (packages.py)."""
    env = os.environ if base is None else base
    return {
        k: v for k, v in env.items()
        if not k.startswith(OWN_PREFIXES) and not (index and INDEX_CREDENTIAL.match(k))
    }


def kill(proc: "subprocess.Popen[bytes]") -> None:
    """Kill a runner and every process it started. A node that shells out
    (``subprocess.run``, a database client, ``curl``) leaves a child holding
    the runner's output pipe: killing the runner alone leaves the run
    waiting on that pipe for as long as the child lives, which is exactly
    when a timeout or a cancel is needed. Each runner leads a process group
    of its own (``_spawn``), so the group goes at once. On Windows only the
    runner is killed."""
    if sys.platform != "win32":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass  # no such group any more, or not one of ours: the runner itself, below
    proc.kill()


class WarmPool:
    def __init__(self, cwd: Path, size: int):
        self.cwd = cwd
        self.size = max(0, size)
        self._idle: list[subprocess.Popen[bytes]] = []
        self._lock = threading.Lock()
        self._closed = False

    def _spawn(self) -> subprocess.Popen[bytes]:
        env = {**node_environ(), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        return subprocess.Popen(
            [sys.executable, "-m", "exuvia.runner", "--warm"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.cwd,
            env=env,
            creationflags=flags,
            # Its own session, so `kill` can end it with whatever it started.
            # Ctrl-C in the terminal then reaches exuvia alone, which kills
            # its runners as it closes (LocalExecutor.close).
            start_new_session=sys.platform != "win32",
        )

    def _refill(self) -> None:
        while True:
            with self._lock:
                self._idle = [p for p in self._idle if p.poll() is None]
                if self._closed or len(self._idle) >= self.size:
                    return
            proc = self._spawn()
            with self._lock:
                if self._closed:
                    kill(proc)
                    return
                self._idle.append(proc)

    def start(self) -> None:
        threading.Thread(target=self._refill, daemon=True).start()

    def take(self) -> subprocess.Popen[bytes]:
        """A worker waiting for a spec path on stdin (warm if one is ready)."""
        with self._lock:
            while self._idle:
                proc = self._idle.pop(0)
                if proc.poll() is None:
                    break
            else:
                proc = None
        if self.size:
            threading.Thread(target=self._refill, daemon=True).start()
        return proc or self._spawn()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for p in idle:
            kill(p)
