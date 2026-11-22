"""A pool of pre-warmed, single-use runner processes.

Importing pandas/pyarrow/duckdb can take seconds (much longer on Windows with
on-access antivirus). Each idle worker has already paid that cost and is
blocked on stdin; ``take()`` hands one out and immediately starts warming a
replacement. Workers are never reused, so node isolation is unchanged. If the
parent dies, the workers' stdin closes and they exit on their own.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from .project_secrets import node_environ


class WarmPool:
    def __init__(self, cwd: Path, size: int):
        self.cwd = cwd
        self.size = max(0, size)
        self._idle: list[subprocess.Popen[bytes]] = []
        self._lock = threading.Lock()
        self._closed = False

    def _spawn(self) -> subprocess.Popen[bytes]:
        # exuvia's own settings (tokens, provider keys, ...) never reach a
        # node; the project's secrets arrive with the run (runner.py).
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
                    proc.kill()
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
            p.kill()
