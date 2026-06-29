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
