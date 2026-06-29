"""Event sources are pluggable adapters that yield :class:`~behave_rv.events.event.Event`.

Adoption priority order:

1. :mod:`behave_rv.events.sources.inprocess` -- in-process emitter (build first).
2. :mod:`behave_rv.events.sources.otel`      -- OpenTelemetry spans (build second).
3. :mod:`behave_rv.events.sources.logs`      -- structured JSON logs.
4. :mod:`behave_rv.events.sources.replay`    -- read events from a recorded file.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from behave_rv.events.event import Event


class EventSource(ABC):
    """Yields normalized :class:`Event` objects. The same pipeline runs over
    every source, identically for live and replay."""

    @abstractmethod
    def events(self) -> Iterator[Event]:
        raise NotImplementedError
