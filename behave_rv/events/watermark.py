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

    def push(self, event: Event) -> None:
        if event.event_time < self._watermark:
            # the stream has already advanced past this event's time
            self.late.append(event)
            return
        heapq.heappush(self._heap, (event.event_time, self._seq, event))
        self._seq += 1
        self._max_seen = max(self._max_seen, event.event_time)

    def releasable(self) -> list[Event]:
        """Events now safe to emit, in event-time order. Advances the watermark."""
        self._watermark = self._max_seen - self.grace
        out: list[Event] = []
        while self._heap and self._heap[0][0] <= self._watermark:
            out.append(heapq.heappop(self._heap)[2])
        return out

    def flush(self) -> list[Event]:
        """Release everything still buffered, in event-time order (end of stream)."""
        out = [heapq.heappop(self._heap)[2] for _ in range(len(self._heap))]
        self._watermark = inf
        return out
