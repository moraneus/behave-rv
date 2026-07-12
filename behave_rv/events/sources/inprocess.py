"""In-process emitter source: the application calls our library directly. Lowest latency.

The application pushes events with :meth:`emit` (or the :meth:`emit_event`
convenience); the engine pulls them with :meth:`events`. ``source`` is stamped
``"inprocess"`` for provenance.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from typing import Any

from behave_rv.events.event import Event
from behave_rv.events.sources import EventSource

SOURCE_NAME = "inprocess"


class InProcessSource(EventSource):
    def __init__(self) -> None:
        self._queue: deque[Event] = deque()

    def emit(self, event: Event) -> None:
        """Push an already-built event onto the stream."""
        self._queue.append(event)

    def emit_event(
        self,
        type: str,
        event_time: float,
        bindings: dict[str, str],
        payload: dict[str, Any],
    ) -> None:
        """Build and push an event, stamping this source's provenance."""
        self.emit(
            Event(
                type=type,
                event_time=event_time,
                bindings=bindings,
                payload=payload,
                source=SOURCE_NAME,
            )
        )

    def events(self) -> Iterator[Event]:
        """Drain and yield currently-queued events, in emission order."""
        while self._queue:
            yield self._queue.popleft()
