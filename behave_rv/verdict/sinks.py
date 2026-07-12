"""Verdict sinks: structured JSON logs by default, optional metrics and alert hooks.
No model in this path.

A sink receives a :class:`~behave_rv.verdict.record.Verdict` and disposes of it.
The default is one JSON object per line, which is greppable and replayable.
"""

from __future__ import annotations

import json
import sys
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


class PrintSink:
    """Print each violation as its fully rendered explanation the moment it is
    decided; other verdicts as one compact line. Pass the compiled policies so
    the authored scenario can be rendered."""

    def __init__(self, policies, stream: TextIO | None = None,
                 compact_ok: bool = True) -> None:
        self._by_id = {p.policy_id: p for p in policies}
        self._stream = stream if stream is not None else sys.stdout
        self._compact_ok = compact_ok

    def emit(self, verdict: Verdict) -> None:
        policy = self._by_id.get(verdict.policy_id)
        if verdict.verdict == "violated" and policy is not None \
                and policy.authored_scenario is not None:
            from behave_rv.verdict.explain import explain_verdict
            print(explain_verdict(verdict, policy.authored_scenario,
                                  policy.failing_step_index),
                  file=self._stream)
            print(file=self._stream)
            return
        if self._compact_ok:
            from behave_rv.verdict.explain import safe_value
            entity = ", ".join(f"{k}={safe_value(v)}" for k, v in verdict.entity_key.items())
            print(f"[{entity}] {verdict.verdict}: {verdict.policy_id} @ t={verdict.at}",
                  file=self._stream)


class JsonFileSink:
    """Append each verdict as a JSON line to a file, flushed per verdict so an
    external tail sees violations as they happen."""

    def __init__(self, path) -> None:
        self._fh = open(path, "a", encoding="utf-8")

    def emit(self, verdict: Verdict) -> None:
        self._fh.write(json.dumps(verdict.to_dict()))
        self._fh.write("\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
