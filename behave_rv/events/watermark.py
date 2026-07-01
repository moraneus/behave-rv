"""Reordering window and event-time handling: a watermark with a short grace window for late arrivals.

Distributed and telemetry sources deliver events out of order. The engine needs
events in event-time order so that ordering and precedence are decided by event
time, not arrival order. A :class:`ReorderBuffer` holds incoming events in a small
window and releases them in event-time order once the watermark -- the highest
event time seen minus the grace -- has passed them.

An event that arrives after the watermark has already moved past its event time is
``late``: it cannot be placed correctly in the ordered stream, so it is flagged
rather than silently slotted in at the wrong point (bias toward surfacing).
"""

from __future__ import annotations

import heapq
from math import inf

from behave_rv.events.event import Event


class ReorderBuffer:
    def __init__(self, grace: float) -> None:
        self.grace = grace
        self._heap: list[tuple[float, int, Event]] = []
        self._seq = 0
        self._max_seen = -inf
        self._watermark = -inf  # event time up to which the stream has been released
        self.late: list[Event] = []

    @staticmethod
    def _tiebreak(event: Event) -> tuple:
        # Ties (equal event_time) are broken by a canonical, content-derived key,
        # NOT by arrival order -- otherwise the verdict for same-timestamp events
        # would depend on the order they arrived, breaking the reordering contract.
        return (
            event.type,
            repr(sorted(event.bindings.items())),
            repr(sorted(event.payload.items())),
            event.source,
        )

    def push(self, event: Event) -> None:
        if event.event_time < self._watermark:
            # the stream has already advanced past this event's time
            self.late.append(event)
            return
        # heap key: (event_time, canonical content, seq, event). event_time drives
        # the watermark; content breaks ties deterministically; seq is only a final
        # fallback for byte-identical events (whose order cannot affect any verdict)
        # and keeps Event objects from ever being compared.
        heapq.heappush(self._heap, (event.event_time, self._tiebreak(event), self._seq, event))
        self._seq += 1
        self._max_seen = max(self._max_seen, event.event_time)

    def releasable(self) -> list[Event]:
        """Events now safe to emit, in event-time order. Advances the watermark."""
        self._watermark = self._max_seen - self.grace
        out: list[Event] = []
        while self._heap and self._heap[0][0] <= self._watermark:
            out.append(heapq.heappop(self._heap)[-1])
        return out

    def flush(self) -> list[Event]:
        """Release everything still buffered, in event-time order (end of stream)."""
        out = [heapq.heappop(self._heap)[-1] for _ in range(len(self._heap))]
        self._watermark = inf
        return out
