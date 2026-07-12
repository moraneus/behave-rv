"""Per-key instance state: automaton state plus a bounded window of recent events
(the witnessing trace).

One monitor instance per distinct correlation key value, holding its automaton
and a bounded window of its own recent events used to explain a verdict.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from behave_rv.compile.automaton import Monitor
from behave_rv.events.event import Event

DEFAULT_TRACE_WINDOW = 64


@dataclass
class Instance:
    policy_id: str
    entity_key: dict[str, str]
    monitor: Monitor
    trace: deque[Event] = field(default_factory=lambda: deque(maxlen=DEFAULT_TRACE_WINDOW))
    last_activity: float = 0.0  # event time of the most recent witnessed event

    def witness(self, event: Event) -> None:
        self.trace.append(event)
        self.last_activity = event.event_time

    def witnessing_trace(self) -> list[Event]:
        return list(self.trace)
