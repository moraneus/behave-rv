"""The evaluation context passed to RV step predicates.

A registered step is a pure matcher: it reads an event and returns a boolean, and
may call ``ctx.bind(...)`` to declare the correlation key it observed. The engine
already shards by correlation key (it reads ``event.bindings``), so for predicate
evaluation the context only needs to accept those bind calls; it records them for
provenance and never mutates anything outside itself.
"""

from __future__ import annotations

from typing import Any


class MatchContext:
    def __init__(self) -> None:
        self.bindings: dict[str, Any] = {}

    def bind(self, **kwargs: Any) -> None:
        self.bindings.update(kwargs)
