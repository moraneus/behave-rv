"""Timer wheel: fires deadlines independently of incoming events. The absence is the violation.

A bounded-response property is violated by a timeout, not by an arriving event,
so the engine needs its own clock. In replay the clock is event time: deadlines
fire as the stream's event time advances past them. In live mode the same queue
is driven by wall clock to fire real-time deadlines when no event arrives.

Stale entries are tolerated: a monitor may resolve before its deadline, so the
engine re-validates each due timer against the live instance before firing.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from typing import Hashable


class TimerQueue:
    def __init__(self) -> None:
        self._heap: list[tuple[float, int, Hashable]] = []
        self._seq = 0  # tie-breaker so heap never compares the payloads

    def schedule(self, when: float, instance_id: Hashable) -> None:
        heapq.heappush(self._heap, (when, self._seq, instance_id))
        self._seq += 1

    def due(self, now: float) -> Iterator[tuple[float, Hashable]]:
        """Yield (deadline, instance_id) for every timer at or before ``now``."""
        while self._heap and self._heap[0][0] <= now:
            when, _, instance_id = heapq.heappop(self._heap)
            yield when, instance_id
