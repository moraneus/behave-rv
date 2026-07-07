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
KEYS = ["A", "B", "C"]

# The registered steps every generated policy binds to.
_REG = StepRegistry()


@_REG.trigger('an order is "{status}"', step_id="order.status.is",
              event_type="order.status", correlation_key="order_id")
def _order_status_is(ctx, event, status):
    return event.type == "order.status" and event.payload.get("status") == status


@_REG.trigger('a closing note is "{status}"', step_id="closing.note.is",
              event_type="order.terminal", correlation_key="order_id")
def _closing_note_is(ctx, event, status):
    return event.type == "order.terminal" and event.payload.get("status") == status


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
    st.builds(lambda sc, b, cl: {"operator": "scoped_never",
                                 "correlation_key": ("order_id",),
                                 "scope": sc, "bad": b, "close": cl},
              st.sampled_from(STATUSES), st.sampled_from(STATUSES),
              st.one_of(st.none(), st.sampled_from(STATUSES))),
    st.builds(lambda g: {"operator": "once", "correlation_key": ("order_id",), "good": g},
              st.sampled_from(STATUSES)),
    st.builds(lambda ph: {"operator": "historically", "correlation_key": ("order_id",),
                          "phi": ph},
              st.sampled_from(STATUSES)),
    st.builds(lambda pr, t: {"operator": "previously", "correlation_key": ("order_id",),
                             "prior": pr, "trigger": t},
              st.sampled_from(STATUSES), st.sampled_from(STATUSES)),
    st.builds(lambda ph, ps: {"operator": "since", "correlation_key": ("order_id",),
                              "phi": ph, "psi": ps},
              st.sampled_from(STATUSES), st.sampled_from(STATUSES)),
    st.builds(lambda p, t: {"operator": "before", "correlation_key": ("order_id",),
                            "prior": p, "trigger": t},
              st.sampled_from(STATUSES), st.sampled_from(STATUSES)),
    st.builds(lambda t, r, n: {"operator": "within", "correlation_key": ("order_id",),
                               "trigger": t, "response": r, "seconds": n},
              st.sampled_from(STATUSES), st.sampled_from(STATUSES), st.integers(1, 3)),
)


def _feature(policy: dict, name: str = "s") -> str:
    op = policy["operator"]
    if op == "never" and policy.get("event_type") == "order.terminal":
        # a policy watching the terminal event type itself (closes the M8 blind spot)
        return (f'Feature: p\n  Scenario: {name}\n'
                f'    Then a closing note is "{policy["bad"]}" never happens\n')
    if op == "never":
        # never is self-contained: predicate-first Then, no When.
        return (f'Feature: p\n  Scenario: {name}\n'
                f'    Then an order is "{policy["bad"]}" never happens\n')
    if op == "scoped_never":
        scope = f'an order is "{policy["scope"]}"'
        if policy["close"] is not None:
            scope += f' until an order is "{policy["close"]}"'
        return (f'Feature: p\n  Scenario: {name}\n    Given {scope}\n'
                f'    Then an order is "{policy["bad"]}" never happens\n')
    if op == "once":
        return (f'Feature: p\n  Scenario: {name}\n'
                f'    Then an order is "{policy["good"]}" has happened\n')
    if op == "historically":
        return (f'Feature: p\n  Scenario: {name}\n'
                f'    Then an order is "{policy["phi"]}" always holds\n')
    if op == "since":
        return (f'Feature: p\n  Scenario: {name}\n'
                f'    Then an order is "{policy["phi"]}" since an order is "{policy["psi"]}"\n')
    if op == "previously":
        return (f'Feature: p\n  Scenario: {name}\n    When an order is "{policy["trigger"]}"\n'
                f'    Then an order is "{policy["prior"]}" previously\n')
    if op == "before":
        then = f'an order is "{policy["prior"]}" before'
        when = policy["trigger"]
    else:
        then = f'an order is "{policy["response"]}" within "{policy["seconds"]}" seconds'
        when = policy["trigger"]
    return (f'Feature: p\n  Scenario: {name}\n    When an order is "{when}"\n'
            f'    Then {then}\n')


def _build(policy: dict, name: str = "s"):
    (p,) = compile_feature(_feature(policy, name), _REG)
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
