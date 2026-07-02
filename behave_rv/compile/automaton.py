"""Per-key state-machine templates: trigger, scope guard, obligation, deadline, correlation key.

A :class:`Policy` is the compiled, shardable unit the engine runs: a correlation
key, the event types it cares about (for dispatch indexing), and a factory that
mints a fresh :class:`Monitor` per distinct key value.

Operators implemented: the safety/response set (``never``, ``before``, ``within``)
and the past-time LTL fragment (``once``, ``historically``, ``previously``,
``since``). Each is a fixed-size-state :class:`Monitor` subclass; the engine drives
them all through the same interface.

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

    def deciding_events(self) -> list[Event]:
        """The small, fixed set of events that actually decided the verdict.

        Kept separate from the instance's bounded recent-context window so an
        explanation always shows the deciding evidence, however old it is. At most
        a handful of events; never grows with trace length.
        """
        return []


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

    def deciding_events(self) -> list[Event]:
        # The single bad event that caused the violation (None if it held).
        return [self.trigger_event] if self.trigger_event is not None else []


class WithinMonitor(Monitor):
    def __init__(self, is_trigger: Predicate, is_response: Predicate, seconds: float) -> None:
        self.is_trigger = is_trigger
        self.is_response = is_response
        self.seconds = seconds
        self._deadline: Optional[float] = None
        self._response_event: Optional[Event] = None

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
            self._response_event = event
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

    def deciding_events(self) -> list[Event]:
        # The arming trigger, plus the response when one settled it (a timeout
        # violation has no response, so just the trigger).
        return [e for e in (self.trigger_event, self._response_event) if e is not None]


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
        self._prior_event: Optional[Event] = None

    def on_event(self, event: Event) -> Optional[str]:
        if self.settled:
            return None
        if self.prior(event):
            if self._prior_event is None:  # capture the first prior that established precedence
                self._prior_event = event
            self._seen_prior = True
        if self.trigger(event):
            self.settled = True
            self.trigger_event = event
            return "satisfied" if self._seen_prior else "violated"
        return None

    def deciding_events(self) -> list[Event]:
        # On satisfaction: the prior that was seen, then the trigger. On violation:
        # the trigger only (no prior existed before it).
        return [e for e in (self._prior_event, self.trigger_event) if e is not None]


class OnceMonitor(Monitor):
    """once(phi): phi has held at some past-or-present point. Existential.

    State: implicit in `settled`. Satisfied the moment phi first holds; pending
    until then; violated at a terminal event if it never held.
    """

    def __init__(self, good: Predicate) -> None:
        self.good = good

    def on_event(self, event: Event) -> Optional[str]:
        if self.settled:
            return None
        if self.good(event):
            self.settled = True
            self.trigger_event = event
            return "satisfied"
        return None

    def on_terminal(self) -> Optional[str]:
        if self.settled:
            return None
        self.settled = True
        return "violated"

    def deciding_events(self) -> list[Event]:
        return [self.trigger_event] if self.trigger_event is not None else []


class HistoricallyMonitor(Monitor):
    """historically(phi): phi has held at every point so far. Universal; the dual
    of never (over occurrence predicates, every event has been a phi event).

    State: implicit in `settled`. Pending while it holds; violated the first event
    where phi fails; satisfied at a terminal event if it never failed.
    """

    def __init__(self, phi: Predicate) -> None:
        self.phi = phi

    def on_event(self, event: Event) -> Optional[str]:
        if self.settled:
            return None
        if not self.phi(event):
            self.settled = True
            self.trigger_event = event
            return "violated"
        return None

    def on_terminal(self) -> Optional[str]:
        if self.settled:
            return None
        self.settled = True
        return "satisfied"

    def deciding_events(self) -> list[Event]:
        return [self.trigger_event] if self.trigger_event is not None else []


class PreviouslyMonitor(Monitor):
    """previously(phi) at a trigger: phi held at the event immediately before the
    trigger for this entity. Triggered (When + Then), the immediate-predecessor
    companion to before (any-predecessor).

    State: `_prev_phi` (did phi hold at the last event) and `_prev_event`. Pending
    until the trigger; then satisfied if the immediately preceding event held phi,
    else violated.
    """

    def __init__(self, prior: Predicate, trigger: Predicate) -> None:
        self.prior = prior
        self.trigger = trigger
        self._prev_phi = False
        self._prev_event: Optional[Event] = None
        self._deciding_prior: Optional[Event] = None

    def on_event(self, event: Event) -> Optional[str]:
        if self.settled:
            return None
        if self.trigger(event):
            self.settled = True
            self.trigger_event = event
            if self._prev_phi:
                self._deciding_prior = self._prev_event
                return "satisfied"
            self._deciding_prior = None
            return "violated"
        # not the trigger: remember whether phi held here, for the next event's "previous"
        self._prev_phi = self.prior(event)
        self._prev_event = event
        return None

    def deciding_events(self) -> list[Event]:
        return [e for e in (self._deciding_prior, self.trigger_event) if e is not None]


class SinceMonitor(Monitor):
    """since(phi, psi) [safety reading]: after psi occurs, phi must hold at every
    event thereafter (until psi re-occurs). Self-contained.

    State: `_s` (the since-recurrence bool) and `_started`, plus `_anchor` (the
    last psi, for the explanation). Pending until settled; violated the first event
    where the chain breaks (phi fails after psi with no re-anchor); satisfied at a
    terminal event if never broken (including the vacuous case psi never occurred).
    """

    def __init__(self, phi: Predicate, psi: Predicate) -> None:
        self.phi = phi
        self.psi = psi
        self._s = False
        self._started = False
        self._anchor: Optional[Event] = None

    def on_event(self, event: Event) -> Optional[str]:
        if self.settled:
            return None
        psi_now = self.psi(event)
        phi_now = self.phi(event)
        new_s = psi_now or (phi_now and self._s)
        if psi_now:
            self._anchor = event
        if new_s and not self._started:
            self._started = True
        if self._started and self._s and not new_s:
            # phi failed after psi with no re-anchor: the since-chain broke
            self.settled = True
            self.trigger_event = event
            self._s = new_s
            return "violated"
        self._s = new_s
        return None

    def on_terminal(self) -> Optional[str]:
        if self.settled:
            return None
        self.settled = True
        return "satisfied"

    def deciding_events(self) -> list[Event]:
        return [e for e in (self._anchor, self.trigger_event) if e is not None]


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


def once(policy_id, *, correlation_key, event_types, good) -> Policy:
    return Policy(policy_id, _normalize_key(correlation_key), frozenset(event_types),
                  lambda: OnceMonitor(good))


def historically(policy_id, *, correlation_key, event_types, phi) -> Policy:
    return Policy(policy_id, _normalize_key(correlation_key), frozenset(event_types),
                  lambda: HistoricallyMonitor(phi))


def previously(policy_id, *, correlation_key, event_types, prior, trigger) -> Policy:
    return Policy(policy_id, _normalize_key(correlation_key), frozenset(event_types),
                  lambda: PreviouslyMonitor(prior, trigger))


def since(policy_id, *, correlation_key, event_types, phi, psi) -> Policy:
    return Policy(policy_id, _normalize_key(correlation_key), frozenset(event_types),
                  lambda: SinceMonitor(phi, psi))
