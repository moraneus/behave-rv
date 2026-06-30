"""Verdict sinks: structured JSON logs by default, optional metrics and alert hooks. No model in this path.

A sink receives a :class:`~behave_rv.verdict.record.Verdict` and disposes of it.
The default is one JSON object per line, which is greppable and replayable.
"""

from __future__ import annotations

import json
from typing import Protocol, TextIO

from behave_rv.verdict.record import Verdict


class Sink(Protocol):
    def emit(self, verdict: Verdict) -> None:
        ...


class JsonSink:
    """Write each verdict as a single JSON line to a text stream."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, verdict: Verdict) -> None:
        self._stream.write(json.dumps(verdict.to_dict()))
        self._stream.write("\n")
