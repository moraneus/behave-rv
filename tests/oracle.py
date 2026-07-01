"""An independent, deliberately simple oracle for the operator verdicts.

It computes the verdict per correlation key by direct definition straight from
SEMANTICS.md, over the whole trace. It shares NO evaluation code with the engine:
it imports only the Event data type and re-derives everything (canonical ordering,
the operator state machines, the global clock horizon) by hand. Its independence
is the point -- if it shared a bug with the engine, the check would prove nothing.

A policy is a plain dict:
  {"operator": "never",  "correlation_key": ("order_id",), "bad": s}
  {"operator": "before", "correlation_key": ("order_id",), "prior": s, "trigger": s}
  {"operator": "within", "correlation_key": ("order_id",), "trigger": s,
   "response": s, "seconds": n}
Predicates are "payload['status'] == s", matching the registered example step.
"""

from __future__ import annotations

from math import inf

from behave_rv.events.event import Event


def canonical_sorted(events: list[Event]) -> list[Event]:
    return sorted(
        events,
        key=lambda e: (
            e.event_time,
            e.type,
            repr(sorted(e.bindings.items())),
            repr(sorted(e.payload.items())),
            e.source,
        ),
    )


def _key_of(event: Event, correlation_key: tuple[str, ...]):
    try:
        return tuple(event.bindings[f] for f in correlation_key)
    except KeyError:
        return None


def _status(event: Event):
    return event.payload.get("status")


def oracle_verdicts(trace: list[Event], policy: dict) -> dict:
    """Return {key_value: verdict} for every key that has at least one event."""
    ck = policy["correlation_key"]
    horizon = max((e.event_time for e in trace), default=None)

    groups: dict = {}
    for e in trace:
        k = _key_of(e, ck)
        if k is not None:
            groups.setdefault(k, []).append(e)

    return {
        (k[0] if len(k) == 1 else k): _verdict(policy, canonical_sorted(evs), horizon)
        for k, evs in groups.items()
    }


def admit(arrival_events: list[Event], grace: float):
    """Model the engine's late-drop admission, by definition (SEMANTICS.md).

    Single pass in ARRIVAL order with a global watermark = max_seen - grace. An
    event whose event_time is below the watermark is dropped as late (and does not
    advance max_seen); otherwise it is admitted. Returns (admitted, dropped).
    """
    admitted: list[Event] = []
    dropped: list[Event] = []
    max_seen = -inf
    watermark = -inf
    for e in arrival_events:
        if e.event_time < watermark:
            dropped.append(e)
        else:
            admitted.append(e)
            if e.event_time > max_seen:
                max_seen = e.event_time
            watermark = max_seen - grace
    return admitted, dropped


def oracle_with_admission(arrival_events: list[Event], policy: dict, grace: float):
    """Verdicts and dropped-late set for a given arrival order and grace.

    Admission is modelled by definition (not by calling the engine); the verdict is
    then computed over the admitted events in canonical order.
    """
    admitted, dropped = admit(arrival_events, grace)
    return oracle_verdicts(admitted, policy), dropped


def _verdict(policy: dict, events: list[Event], horizon) -> str:
    op = policy["operator"]

    if op == "never":
        bad = policy["bad"]
        return "violated" if any(_status(e) == bad for e in events) else "pending"

    if op == "before":
        prior, trigger = policy["prior"], policy["trigger"]
        seen_prior = False
        for e in events:
            if _status(e) == prior:
                seen_prior = True
            if _status(e) == trigger:
                return "satisfied" if seen_prior else "violated"
        return "pending"

    if op == "within":
        trigger, response, seconds = policy["trigger"], policy["response"], policy["seconds"]
        armed = False
        deadline = None
        for e in events:
            if armed and e.event_time >= deadline:
                return "violated"
            if not armed and _status(e) == trigger:
                armed = True
                deadline = e.event_time + seconds
            elif armed and _status(e) == response:
                return "satisfied"
        if armed and horizon is not None and horizon >= deadline:
            return "violated"
        return "pending"

    raise ValueError(f"unknown operator {op!r}")
