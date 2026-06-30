"""The `before` precedence operator: a trigger event must have been preceded by a
prior condition for the same entity. Past-time safety property with all three
verdicts. Drives "an order may only be paid after it was authorized".
"""

from behave_rv.compile.automaton import BeforeMonitor
from behave_rv.events.event import Event


def ev(status, t):
    return Event("order.status", t, {"order_id": "A"}, {"status": status}, "test")


def is_authorized(e):
    return e.payload.get("status") == "authorized"


def is_paid(e):
    return e.payload.get("status") == "paid"


def test_satisfied_when_prior_precedes_the_trigger():
    m = BeforeMonitor(prior=is_authorized, trigger=is_paid)
    assert m.on_event(ev("authorized", 1.0)) is None
    assert m.on_event(ev("paid", 2.0)) == "satisfied"
    assert m.settled is True


def test_violated_when_trigger_has_no_prior():
    m = BeforeMonitor(prior=is_authorized, trigger=is_paid)
    assert m.on_event(ev("paid", 1.0)) == "violated"


def test_pending_with_prior_but_no_trigger():
    m = BeforeMonitor(prior=is_authorized, trigger=is_paid)
    assert m.on_event(ev("authorized", 1.0)) is None
    assert m.next_deadline() is None
    assert m.settled is False


def test_unrelated_events_are_ignored():
    m = BeforeMonitor(prior=is_authorized, trigger=is_paid)
    assert m.on_event(ev("shipped", 1.0)) is None
    assert m.on_event(ev("paid", 2.0)) == "violated"
