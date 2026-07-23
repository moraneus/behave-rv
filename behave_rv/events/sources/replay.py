"""Replay source: read events from a recorded file through the identical pipeline. Build early.

The recorded format is JSON Lines: one serialized :class:`Event` per line. The
same pipeline runs over live and replayed streams, so a policy can be tested
against last week's trace before it is pointed at live traffic.

Replay is event-time-driven, which leaves one gap a live run does not have: a
deadline that fired live on the WALL clock, in the silence after the last
event, has no recorded event to advance replay time past it -- the recorded
evidence of a violation would replay as ``pending``. The clock-horizon marker
closes that gap: a recording notes the moment it stopped, and on replay that
marker advances event time to the horizon exactly as the live wall clock did.
The marker is an ordinary :class:`Event` of the reserved type
:data:`HORIZON_EVENT_TYPE` with no bindings, so it flows through the identical
pipeline: no policy observes it, and its only effect is firing the deadlines
the advancing time has passed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from os import PathLike
from typing import Callable, Optional

from behave_rv.events.event import Event
from behave_rv.events.sources import EventSource
from behave_rv.events.watermark import usable_time

# Reserved event type for the clock-horizon marker. Application events must not
# use it: the engine treats it as a pure clock advance.
HORIZON_EVENT_TYPE = "behave_rv.clock"


def horizon_event(at: float) -> Event:
    """The clock-horizon marker: a pure clock advance to ``at``. It carries no
    bindings and no payload, so no monitor instance is created or touched; the
    engine's handling reduces to firing the deadlines that event time ``at``
    has passed."""
    return Event(type=HORIZON_EVENT_TYPE, event_time=at, bindings={},
                 payload={}, source="clock")


def record_events(path: str | PathLike[str], events: Iterable[Event],
                  horizon: Optional[float] = None) -> None:
    """Write ``events`` to ``path`` as JSON Lines, in iteration order.

    ``horizon`` appends a clock-horizon marker at that event time: pass the
    moment the observation window closed so that wall-fired deadline verdicts
    inside it replay as verdicts, not as ``pending``.
    """
    with open(path, "w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event.to_dict()))
            fh.write("\n")
        if horizon is not None and usable_time(horizon):
            fh.write(json.dumps(horizon_event(horizon).to_dict()))
            fh.write("\n")


class TraceRecorder:
    """Record a LIVE app's event stream to a replayable JSONL trace, as a tee.

    Where trace files come from, plainly: a test or script can build a list
    and call :func:`record_events`, but a running application does not have a
    finished list -- it has a stream. The recorder is a pass-through hook you
    compose into your emit chain, exactly like ``dashboard.tap``::

        recorder = TraceRecorder("traces/2026-07-13.jsonl", clock=clock)
        service = TicketService(lambda e: source.push(recorder(e)))
        ...
        recorder.close()          # or use it as a context manager

    Every event is appended (and flushed) as one JSON line in the same format
    :func:`record_events` writes and :class:`ReplaySource` reads, so the
    resulting file feeds replay runs, ``--trace`` liveness checks, and policy
    dry-runs against yesterday's traffic.

    ``clock`` should be the same callable the service emits event times with.
    On :meth:`close` the recorder appends a clock-horizon marker at the moment
    recording stopped -- ``clock()`` when provided, else the highest event
    time seen -- so deadline verdicts that fired on the live wall clock after
    the last event replay as verdicts instead of ``pending``.
    """

    def __init__(self, path: str | PathLike[str],
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._fh = open(path, "a", encoding="utf-8")
        self._clock = clock
        self._max_time = float("-inf")

    def __call__(self, event: Event) -> Event:
        self._fh.write(json.dumps(event.to_dict()))
        self._fh.write("\n")
        self._fh.flush()
        if usable_time(event.event_time) and event.event_time > self._max_time:
            self._max_time = event.event_time
        return event

    def close(self) -> None:
        if self._fh.closed:
            return
        at = self._clock() if self._clock is not None else self._max_time
        if usable_time(at) and at >= self._max_time:
            self._fh.write(json.dumps(horizon_event(at).to_dict()))
            self._fh.write("\n")
        self._fh.close()

    def __enter__(self) -> "TraceRecorder":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class ReplaySource(EventSource):
    def __init__(self, path: str | PathLike[str]) -> None:
        self._path = path

    def events(self) -> Iterator[Event]:
        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield Event.from_dict(json.loads(line))
