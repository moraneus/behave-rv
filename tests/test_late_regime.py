"""Property-based tests for the late-event regime: traces whose event-time span
exceeds the grace window, so events are dropped as late. The centerpieces are
`test_late_engine_equals_oracle` (engine and oracle must agree on the verdict AND
on which events were dropped) and `test_no_silent_verdict_change_across_drops` (a
verdict may change with arrival order only when the dropped-late set differs and is
flagged -- never silently).

Input space: order.status events over keys {A,B}, statuses from the example
vocabulary, integer event times in [0,20] (span can far exceed the grace),
grace in {1,2,3}, adversarial arrival orders (arbitrary permutations, which force
drops). Policies compiled from Gherkin against the registered example step.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource
from tests.oracle import admit, canonical_sorted, oracle_verdicts, oracle_with_admission
from tests.test_properties import _build, _events, policies

STATUSES = ["authorized", "paid", "cancelled", "delivered", "shipped"]
KEYS = ["A", "B"]

wide_triples = st.lists(
    st.tuples(st.sampled_from(KEYS), st.sampled_from(STATUSES), st.integers(0, 20)),
    min_size=1, max_size=14,
)
grace_strat = st.sampled_from([1, 2, 3])


def _fp(e: Event):
    return (e.event_time, e.type, tuple(sorted(e.bindings.items())),
            tuple(sorted(e.payload.items())), e.source)


def _fps(events):
    return sorted(_fp(e) for e in events)


class _Run:
    def __init__(self, verdicts, dropped, late_events):
        self.verdicts = verdicts
        self.dropped = dropped
        self.late_events = late_events


def _engine_run(arrival, policy, grace) -> _Run:
    src = InProcessSource()
    for e in arrival:
        src.emit(e)
    engine = Engine([_build(policy)], grace=grace)
    verdicts = engine.run(src, emit_pending=True)
    return _Run({v.entity_key["order_id"]: v.verdict for v in verdicts},
                engine.dropped_late, engine.late_events)


@st.composite
def late_case(draw):
    events = _events(draw(wide_triples))
    arrival = draw(st.permutations(events))
    return arrival, draw(grace_strat), draw(policies)


@st.composite
def late_case_two_arrivals(draw):
    events = _events(draw(wide_triples))
    a = draw(st.permutations(events))
    b = draw(st.permutations(events))
    return a, b, draw(grace_strat), draw(policies)


# --- Property 1 (centerpiece): engine == oracle, verdicts AND dropped set ----


@settings(max_examples=500, deadline=None)
@given(late_case())
def test_late_engine_equals_oracle(case):
    arrival, grace, policy = case
    exp_verdicts, exp_dropped = oracle_with_admission(list(arrival), policy, grace)
    run = _engine_run(arrival, policy, grace)
    assert run.verdicts == exp_verdicts
    assert _fps(run.dropped) == _fps(exp_dropped)


# --- Property 2: admitted-set invariance ------------------------------------


@settings(max_examples=500, deadline=None)
@given(late_case())
def test_admitted_set_invariance(case):
    arrival, grace, policy = case
    admitted, _ = admit(list(arrival), grace)
    # Canonical (event-time-ascending) arrival of the admitted events admits all of
    # them, so it is a DIFFERENT arrival order that admits the SAME set. The verdict
    # over that set must be identical.
    clean = canonical_sorted(admitted)
    assert (_engine_run(arrival, policy, grace).verdicts
            == _engine_run(clean, policy, grace).verdicts)


# --- Property 3: drop characterization / ascending admits everything --------


@settings(max_examples=300, deadline=None)
@given(late_case())
def test_ascending_arrival_admits_everything(case):
    arrival, grace, policy = case
    events = list(arrival)
    ascending = canonical_sorted(events)
    run = _engine_run(ascending, policy, grace)
    # drops are purely an out-of-order phenomenon: canonical arrival drops nothing,
    # and the verdict then equals the drop-free canonical verdict over the whole trace.
    assert run.dropped == []
    assert run.late_events == 0
    assert run.verdicts == oracle_verdicts(events, policy)


# --- Property 4 (centerpiece): no silent verdict change across drops ---------


@settings(max_examples=500, deadline=None)
@given(late_case_two_arrivals())
def test_no_silent_verdict_change_across_drops(case):
    arrival_a, arrival_b, grace, policy = case
    a = _engine_run(arrival_a, policy, grace)
    b = _engine_run(arrival_b, policy, grace)
    if a.verdicts != b.verdicts:
        # a difference is permitted ONLY when the admitted sets differ, i.e. the
        # dropped-late sets differ, and at least one arrival flagged a drop.
        assert _fps(a.dropped) != _fps(b.dropped)
        assert a.late_events > 0 or b.late_events > 0
