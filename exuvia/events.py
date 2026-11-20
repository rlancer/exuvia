"""Tiny in-process pub/sub used to push live updates to the UI over SSE."""

from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Bumped on every state change (not log lines), so a host can tell
        # cheaply whether anything needs persisting since it last looked.
        self.version = 0

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    def touch(self) -> None:
        """Count a change worth persisting that no client needs to hear about."""
        self.version += 1

    def publish(self, event: dict[str, Any]) -> None:
        """Safe to call from any thread."""
        if event.get("type") not in ("log", "agent_event"):
            self.version += 1
        if self._loop is None or self._loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._fanout(event)
        else:
            self._loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow client; it will resync on the next graph event
