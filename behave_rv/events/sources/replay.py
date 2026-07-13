"""Replay source: read events from a recorded file through the identical pipeline. Build early.

The recorded format is JSON Lines: one serialized :class:`Event` per line. The
same pipeline runs over live and replayed streams, so a policy can be tested
against last week's trace before it is pointed at live traffic.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from os import PathLike

from behave_rv.events.event import Event
from behave_rv.events.sources import EventSource


def record_events(path: str | PathLike[str], events: Iterable[Event]) -> None:
    """Write ``events`` to ``path`` as JSON Lines, in iteration order."""
    with open(path, "w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event.to_dict()))
            fh.write("\n")


class TraceRecorder:
    """Record a LIVE app's event stream to a replayable JSONL trace, as a tee.

    Where trace files come from, plainly: a test or script can build a list
    and call :func:`record_events`, but a running application does not have a
    finished list -- it has a stream. The recorder is a pass-through hook you
    compose into your emit chain, exactly like ``dashboard.tap``::

        recorder = TraceRecorder("traces/2026-07-13.jsonl")
        service = TicketService(lambda e: source.push(recorder(e)))
        ...
        recorder.close()

    Every event is appended (and flushed) as one JSON line in the same format
    :func:`record_events` writes and :class:`ReplaySource` reads, so the
    resulting file feeds replay runs, ``--trace`` liveness checks, and policy
    dry-runs against yesterday's traffic.
    """

    def __init__(self, path: str | PathLike[str]) -> None:
        self._fh = open(path, "a", encoding="utf-8")

    def __call__(self, event: Event) -> Event:
        self._fh.write(json.dumps(event.to_dict()))
        self._fh.write("\n")
        self._fh.flush()
        return event

    def close(self) -> None:
        self._fh.close()


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
