"""The evaluation context passed to RV step predicates.

A registered step is REQUIRED to be a deterministic, side-effect-free matcher: it
reads an event and returns a boolean. (The framework expects this and does not
enforce it.)

What ``ctx.bind(...)`` actually does, stated plainly: it records into THIS
per-evaluation scratch object, which the dispatcher does not consume and which is
discarded after the call. Entity identity comes from the step decorator's declared
``correlation_key`` fields, whose values the engine reads from ``event.bindings``
at dispatch. Calling ``bind()`` is therefore optional and has no effect on
dispatch; it is kept as a readable declaration of which binding the step observed,
and as a hook for future cross-checking of declared-vs-observed keys.
"""

from __future__ import annotations

from typing import Any


class MatchContext:
    def __init__(self) -> None:
        self.bindings: dict[str, Any] = {}

    def bind(self, **kwargs: Any) -> None:
        self.bindings.update(kwargs)
