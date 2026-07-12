"""Reordering window and event-time handling: a watermark with a short grace window
for late arrivals.

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
from math import inf, isfinite

from behave_rv.events.event import Event


def usable_time(value) -> bool:
    """True iff ``value`` can drive the watermark and the timers: a finite real
    number. Anything else -- inf, -inf, NaN, a string, None, any non-number --
    is unusable and must be rejected with visibility rather than crash or
    corrupt ordering."""
    try:
        return isfinite(value)
    except TypeError:
        return False


class ReorderBuffer:
    def __init__(self, grace: float) -> None:
        self.grace = grace
        self._heap: list[tuple[float, int, Event]] = []
        self._seq = 0
        self._max_seen = -inf
        self._watermark = -inf  # event time up to which the stream has been released
        self.late: list[Event] = []
        # events with a non-finite event_time (inf/-inf/NaN): rejected at admission
        # so they can never poison the watermark or corrupt the heap ordering, and
        # recorded separately from `late` because malformed input is a different
        # signal than lateness.
        self.invalid: list[Event] = []

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
        if not usable_time(event.event_time):
            self.invalid.append(event)
            return
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

    @property
    def clock_front(self) -> float:
        """The highest admitted event time (the stream's clock front)."""
        return self._max_seen

    def advance_clock(self, tick: float) -> None:
        """Advance the clock as if a tick event at ``tick`` arrived, without
        admitting any event. Used by wall-clock deadline firing on live
        sources: the watermark then moves exactly as the admission rules say,
        so events arriving later that are older than the watermark are late
        and flagged -- the committed-plus-flagged rule, via the existing
        machinery rather than a second one."""
        if tick > self._max_seen:
            self._max_seen = tick

    def peek_oldest(self) -> float | None:
        """The earliest buffered event time, or None. On a live source the
        engine uses this to age the buffer by wall clock: a buffered event is
        released once grace wall-seconds have passed with no newer event."""
        return self._heap[0][0] if self._heap else None

    def releasable(self) -> list[Event]:
        """Events now safe to emit, in canonical order. Advances the watermark.

        The boundary is STRICT (`event_time < watermark`): an event *at* the
        watermark is not yet safe, because a same-timestamp sibling can still
        arrive and be admitted (admission is `time < watermark`). Releasing at the
        watermark would emit such ties in arrival order across separate releases,
        breaking canonical ordering. Holding them until the watermark strictly
        passes keeps every same-timestamp event in one batch, popped canonically.
        """
        self._watermark = self._max_seen - self.grace
        out: list[Event] = []
        while self._heap and self._heap[0][0] < self._watermark:
            out.append(heapq.heappop(self._heap)[-1])
        return out

    def flush(self) -> list[Event]:
        """Release everything still buffered, in event-time order (end of stream)."""
        out = [heapq.heappop(self._heap)[-1] for _ in range(len(self._heap))]
        self._watermark = inf
        return out
