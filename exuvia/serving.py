"""Running the workbench and the hub under uvicorn so that Ctrl+C stops them
promptly and quietly.

uvicorn's graceful shutdown waits for every open response to finish, and the
workbench keeps one open for as long as a tab is: its event stream
(``/api/events``). So shutting down first tells those streams to end
(:func:`closing`, which the stream waits on beside its queue), then waits a
few seconds at most for anything else still being sent.

A second SIGINT within a second of the first is the same Ctrl+C arriving
twice (``uvx`` forwards the terminal's SIGINT to the server, which the
terminal has already sent it), not a request to force quit. A second Ctrl+C
after that does force quit, as in uvicorn, but without printing the
tracebacks of the requests and the lifespan it cuts short; nor does a
request the grace period cuts short print one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Generator
from types import FrameType
from typing import Any

import uvicorn

# How long shutdown waits for responses still being sent once the event
# streams have ended (a download, an agent's turn being streamed).
GRACE_SECONDS = 5
# A SIGINT this soon after the first is the same keypress, delivered twice.
SAME_SIGNAL_SECONDS = 1.0


def closing(app: Any) -> asyncio.Event | None:
    """Set once the server has begun to shut down: a response that would
    otherwise stay open (an event stream) ends when it is. None for an app
    not run by :func:`run` (the tests' client)."""
    return getattr(app.state, "closing", None)


def _not_cancelled(record: logging.LogRecord) -> bool:
    """Drop the traceback of a request shutdown cut short (uvicorn logs one
    for each response still open after :data:`GRACE_SECONDS`, below its one
    line saying how many it cancelled)."""
    exc = record.exc_info[1] if record.exc_info else None
    return not isinstance(exc, asyncio.CancelledError)


class _Server(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, app: Any) -> None:
        super().__init__(config)
        self._app = app
        self._first_signal: float | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        now = time.monotonic()
        if self._first_signal is None:
            self._first_signal = now
        elif sig == signal.SIGINT and now - self._first_signal < SAME_SIGNAL_SECONDS:
            return  # the same Ctrl+C, forwarded
        super().handle_exit(sig, frame)
        if self.force_exit:
            # Asked to quit now: what is cut short is expected, not an error.
            logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        # uvicorn raises the captured signals again once it has stopped, so
        # that a caller sees the KeyboardInterrupt; here nothing follows
        # (the command returns), and raising it inside the event loop is
        # what turned the tasks still open into tracebacks.
        with super().capture_signals():
            try:
                yield
            finally:
                self._captured_signals.clear()

    async def shutdown(self, sockets: list[Any] | None = None) -> None:
        event = closing(self._app)
        if event is not None:
            event.set()
        logging.getLogger("uvicorn.error").addFilter(_not_cancelled)
        await super().shutdown(sockets)


def run(app: Any, host: str, port: int) -> None:
    """Serve ``app`` until Ctrl+C (or SIGTERM)."""
    app.state.closing = asyncio.Event()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=GRACE_SECONDS)
    _Server(config, app).run()
