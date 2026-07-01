"""Property-based tests: the engine's verdict must equal the independent oracle's
verdict across a large generated input space, for the implemented operators
(never, before, within) and the event-time reordering.

Two centerpieces: `test_engine_equals_oracle` (the master property) and
`test_reordering_invariance` (arrival order must not change the verdict).

Input space (stated so the coverage is precise): order.status events over keys
{A,B}, statuses drawn from the example vocabulary, integer event times in [0,4]
(so the trace's event-time span <= the default grace of 5.0s -> no event is ever
dropped as late, the regime where reordering invariance is claimed). Policies are
compiled from Gherkin against a real registered step (`an order is "{status}"`),
so the engine's actual compile+automaton+dispatch+timer+reorder path is exercised.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import compile_feature
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource
from tests.oracle import canonical_sorted, oracle_verdicts

STATUSES = ["authorized", "paid", "cancelled", "delivered", "shipped"]
KEYS = ["A", "B"]

# One real registered step; every generated policy binds to it.
_REG = StepRegistry()


@_REG.trigger('an order is "{status}"', step_id="order.status.is",
              event_type="order.status", correlation_key="order_id")
def _order_status_is(ctx, event, status):
    return event.type == "order.status" and event.payload.get("status") == status


def _events(triples):
    return [Event("order.status", float(t), {"order_id": k}, {"status": s}, "gen")
            for (k, s, t) in triples]


triples = st.lists(
    st.tuples(st.sampled_from(KEYS), st.sampled_from(STATUSES), st.integers(0, 4)),
    max_size=12,
)

policies = st.one_of(
    st.builds(lambda b: {"operator": "never", "correlation_key": ("order_id",), "bad": b},
              st.sampled_from(STATUSES)),
    st.builds(lambda p, t: {"operator": "before", "correlation_key": ("order_id",),
                            "prior": p, "trigger": t},
              st.sampled_from(STATUSES), st.sampled_from(STATUSES)),
    st.builds(lambda t, r, n: {"operator": "within", "correlation_key": ("order_id",),
                               "trigger": t, "response": r, "seconds": n},
              st.sampled_from(STATUSES), st.sampled_from(STATUSES), st.integers(1, 3)),
)


def _feature(policy: dict) -> str:
    op = policy["operator"]
    if op == "never":
        then = "it must never happen"
        when = policy["bad"]
    elif op == "before":
        then = f'an order is "{policy["prior"]}" before'
        when = policy["trigger"]
    else:
        then = f'an order is "{policy["response"]}" within "{policy["seconds"]}" seconds'
        when = policy["trigger"]
    return (f'Feature: p\n  Scenario: s\n    When an order is "{when}"\n'
            f'    Then {then}\n')


def _build(policy: dict):
    (p,) = compile_feature(_feature(policy), _REG)
    return p


def _engine_verdicts(events, policy, grace=None):
    src = InProcessSource()
    for e in events:
        src.emit(e)
    engine = Engine([_build(policy)]) if grace is None else Engine([_build(policy)], grace=grace)
    verdicts = engine.run(src, emit_pending=True)
    return {v.entity_key["order_id"]: v.verdict for v in verdicts}


# --- Property 1 (centerpiece): engine == oracle ----------------------------


@settings(max_examples=500, deadline=None)
@given(triples, policies)
def test_engine_equals_oracle(tr, policy):
    events = _events(tr)
    assert _engine_verdicts(events, policy) == oracle_verdicts(events, policy)


# --- Property 2 (centerpiece): reordering invariance -----------------------


@st.composite
def trace_policy_and_shuffle(draw):
    tr = draw(triples)
    events = _events(tr)
    shuffled = draw(st.permutations(events))  # adversarial arrival orders
    policy = draw(policies)
    return events, shuffled, policy


@settings(max_examples=500, deadline=None)
@given(trace_policy_and_shuffle())
def test_reordering_invariance(data):
    events, shuffled, policy = data
    in_event_time_order = canonical_sorted(events)
    assert _engine_verdicts(in_event_time_order, policy) == _engine_verdicts(shuffled, policy)


# --- Property 3: determinism -----------------------------------------------


@settings(max_examples=300, deadline=None)
@given(triples, policies)
def test_determinism(tr, policy):
    events = _events(tr)
    assert _engine_verdicts(events, policy) == _engine_verdicts(events, policy)


# --- Property 4: per-key independence ---------------------------------------


@settings(max_examples=300, deadline=None)
@given(triples, policies)
def test_per_key_independence(tr, policy):
    events = _events(tr)
    if not events:
        return
    horizon = max(e.event_time for e in events)
    interleaved = _engine_verdicts(events, policy)

    for key in {e.bindings["order_id"] for e in events}:
        alone = [e for e in events if e.bindings["order_id"] == key]
        # advance the global clock to the same horizon so within-deadlines match
        alone = alone + [Event("clock.tick", horizon, {"order_id": key}, {}, "gen")]
        assert _engine_verdicts(alone, policy).get(key) == interleaved.get(key)
