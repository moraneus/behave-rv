"""Per-key state-machine templates: trigger, scope guard, obligation, deadline, correlation key.

A :class:`Policy` is the compiled, shardable unit the engine runs: a correlation
key, the event types it cares about (for dispatch indexing), and a factory that
mints a fresh :class:`Monitor` per distinct key value.

Two operators are implemented for v1:

* ``never`` -- a safety property. Violated the moment a "bad" event matches;
  otherwise silent. No deadline.
* ``within`` -- a bounded-response property. A trigger arms a deadline; a
  response before it satisfies, the deadline passing without one violates.

Monitors are pure: they read the event and bounded instance state and return a
verdict status or ``None``. They never mutate the outside world.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Optional

from behave_rv.events.event import Event

Predicate = Callable[[Event], bool]


def _normalize_key(correlation_key: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(correlation_key, str):
        return (correlation_key,)
    return tuple(correlation_key)


class Monitor:
    """The per-key automaton interface the engine drives.

    ``trigger_event`` is the event that opened the current obligation; the engine
    uses it as the trigger on a timeout verdict, where no incoming event caused
    the verdict.
    """

    settled: bool = False
    trigger_event: Optional[Event] = None

    def on_event(self, event: Event) -> Optional[str]:
        raise NotImplementedError

    def next_deadline(self) -> Optional[float]:
        return None

    def on_timeout(self, now: float) -> Optional[str]:
        return None

    def on_terminal(self) -> Optional[str]:
        """Final verdict when the entity's lifetime ends (a terminal event)."""
        return None


class NeverMonitor(Monitor):
    def __init__(self, bad: Predicate) -> None:
        self.bad = bad

    def on_event(self, event: Event) -> Optional[str]:
        if self.settled:
            return None
        if self.bad(event):
            self.settled = True
            self.trigger_event = event
            return "violated"
        return None

    def on_terminal(self) -> Optional[str]:
        # The bad event never arrived over the entity's whole life: it held.
        if self.settled:
            return None
        self.settled = True
        return "satisfied"


class WithinMonitor(Monitor):
    def __init__(self, is_trigger: Predicate, is_response: Predicate, seconds: float) -> None:
        self.is_trigger = is_trigger
        self.is_response = is_response
        self.seconds = seconds
        self._deadline: Optional[float] = None

    def on_event(self, event: Event) -> Optional[str]:
        if self.settled:
            return None
        if self._deadline is None:
            if self.is_trigger(event):
                self._deadline = event.event_time + self.seconds
                self.trigger_event = event
            return None
        if self.is_response(event):
            self.settled = True
            return "satisfied" if event.event_time <= self._deadline else "violated"
        return None

    def next_deadline(self) -> Optional[float]:
        return None if self.settled else self._deadline

    def on_timeout(self, now: float) -> Optional[str]:
        if self.settled or self._deadline is None or now < self._deadline:
            return None
        self.settled = True
        return "violated"

    def on_terminal(self) -> Optional[str]:
        # Armed but unfulfilled when the entity ends: the response can never come.
        if self.settled or self._deadline is None:
            return None
        self.settled = True
        return "violated"


class BeforeMonitor(Monitor):
    """Precedence: the trigger event must have been preceded by the prior condition.

    "B may only happen after A" -- when B (the trigger) occurs, A (the prior) must
    already have been seen for this entity. Pending until the trigger fires; then
    satisfied if the prior was seen, violated otherwise. A past-time check over the
    instance's own witnessed state, with no deadline.
    """

    def __init__(self, prior: Predicate, trigger: Predicate) -> None:
        self.prior = prior
        self.trigger = trigger
        self._seen_prior = False

    def on_event(self, event: Event) -> Optional[str]:
        if self.settled:
            return None
        if self.prior(event):
            self._seen_prior = True
        if self.trigger(event):
            self.settled = True
            self.trigger_event = event
            return "satisfied" if self._seen_prior else "violated"
        return None


@dataclass(frozen=True)
class Policy:
    policy_id: str
    correlation_key: tuple[str, ...]
    event_types: frozenset[str]
    monitor_factory: Callable[[], Monitor]
    # Set by the Gherkin compiler so a verdict can be explained as the authored
    # scenario with the failing step marked. The engine ignores both.
    authored_scenario: Any = None
    failing_step_index: Optional[int] = None


def never(
    policy_id: str,
    *,
    correlation_key: str | Iterable[str],
    event_types: Iterable[str],
    bad: Predicate,
) -> Policy:
    return Policy(
        policy_id=policy_id,
        correlation_key=_normalize_key(correlation_key),
        event_types=frozenset(event_types),
        monitor_factory=lambda: NeverMonitor(bad),
    )


def within(
    policy_id: str,
    *,
    correlation_key: str | Iterable[str],
    seconds: float,
    is_trigger: Predicate,
    is_response: Predicate,
    event_types: Iterable[str],
) -> Policy:
    return Policy(
        policy_id=policy_id,
        correlation_key=_normalize_key(correlation_key),
        event_types=frozenset(event_types),
        monitor_factory=lambda: WithinMonitor(is_trigger, is_response, seconds),
    )


def before(
    policy_id: str,
    *,
    correlation_key: str | Iterable[str],
    prior: Predicate,
    trigger: Predicate,
    event_types: Iterable[str],
) -> Policy:
    return Policy(
        policy_id=policy_id,
        correlation_key=_normalize_key(correlation_key),
        event_types=frozenset(event_types),
        monitor_factory=lambda: BeforeMonitor(prior, trigger),
    )
