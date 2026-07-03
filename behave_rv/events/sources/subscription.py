"""A subscription source that stays open while a service runs.

The service pushes events from its own code as they occur; the engine's loop
blocks waiting for the next event instead of exiting when none is immediately
available. An explicit :meth:`close` ends the stream, at which point the engine
flushes its reorder buffer (so buffered events are released, armed deadlines
whose horizon has passed resolve to ``violated``) and emits end-of-stream
pendings.

Thread-safety contract, stated plainly: the engine loop is single-threaded by
design and stays so. The queue is the boundary -- :meth:`push` and :meth:`close`
are safe to call from another thread (the service's thread), while consumption
via :meth:`events` happens on exactly one thread (the engine's). This source
does not make the engine multi-threaded; it makes feeding it thread-safe.
"""

from __future__ import annotations

import queue
from collections.abc import Iterator

from behave_rv.events.event import Event
from behave_rv.events.sources import EventSource

_CLOSED = object()


class QueueSource(EventSource):
    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._closed = False

    def push(self, event: Event) -> None:
        """Feed one event. Safe to call from any thread. Raises after close()."""
        if self._closed:
            raise RuntimeError("QueueSource is closed")
        self._queue.put(event)

    def close(self) -> None:
        """End the stream. The engine then flushes its reorder buffer, resolves
        what the horizon allows, emits pendings, and returns. Idempotent."""
        if not self._closed:
            self._closed = True
            self._queue.put(_CLOSED)

    def events(self) -> Iterator[Event]:
        """Yield events as they are pushed, blocking while the stream is quiet.
        Ends when close() is called. Single-consumer."""
        while True:
            item = self._queue.get()   # blocks: a quiet service means we wait
            if item is _CLOSED:
                return
            yield item
