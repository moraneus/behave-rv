"""Property-based tests for terminal retirement and quiescence reclamation crossing
the reordering seam.

Findings this round (see SEMANTICS.md): terminal retirement is fully consistent
with the canonical verdict, so it is asserted EXACTLY against the oracle. Quiescence
TTL is best-effort, timer-driven GC whose exact timing is implementation-defined; it
is guaranteed only to be arrival-invariant and deterministic, which is what is
asserted for the TTL cases.

Centerpieces: `test_terminal_engine_equals_oracle` (verdict + dropped set match the
canonical oracle) and `test_no_silent_verdict_change_under_lifecycle` (a verdict may
differ across arrival orders only when the dropped-late set differs and is flagged).

Input space: order.status + terminal ("order.terminal") events over keys {A,B},
statuses from the vocabulary, integer event times in [0,20] (span exceeds grace),
grace in {1,2,3}, quiescence ttl in {None,3,5,8}, adversarial arrival permutations.
Policies compiled from Gherkin against the registered example step.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource
from tests.oracle import canonical_sorted, oracle_lifecycle
from tests.test_properties import _build, policies

STATUSES = ["authorized", "paid", "cancelled", "delivered", "shipped"]
KEYS = ["A", "B"]
TERMINAL = "order.terminal"

lc_triples = st.lists(
    st.tuples(st.sampled_from(KEYS),
              st.one_of(st.none(), st.sampled_from(STATUSES)),
              st.integers(0, 20)),
    min_size=1, max_size=14,
)
grace_strat = st.sampled_from([1, 2, 3])
ttl_strat = st.sampled_from([None, 3, 5, 8])

# includes a policy watching the terminal event type itself (closes the M8
# blind spot: retirement racing dispatch is only observable to such a policy)
terminal_watcher = st.just({"operator": "never", "correlation_key": ("order_id",),
                            "bad": "closed", "event_type": "order.terminal"})
lc_policies = st.one_of(policies, terminal_watcher)


def _events_lc(triples):
    out = []
    for (k, s, t) in triples:
        if s is None:
            out.append(Event(TERMINAL, float(t), {"order_id": k},
                             {"status": "closed"}, "gen"))
        else:
            out.append(Event("order.status", float(t), {"order_id": k}, {"status": s}, "gen"))
    return out


def _fp(e):
    return (e.event_time, e.type, tuple(sorted(e.bindings.items())),
            tuple(sorted(e.payload.items())), e.source)


def _fps(events):
    return sorted(_fp(e) for e in events)


class _Run:
    def __init__(self, verdicts, dropped, retired, reclaimed, late_events):
        self.verdicts = verdicts
        self.dropped = dropped
        self.retired = retired
        self.reclaimed = reclaimed
        self.late_events = late_events


def _engine_run(arrival, policy, grace, ttl) -> _Run:
    src = InProcessSource()
    for e in arrival:
        src.emit(e)
    engine = Engine([_build(policy)], grace=grace,
                    terminal_event_types={TERMINAL}, quiescence_ttl=ttl)
    verdicts = engine.run(src, emit_pending=True)
    ck = policy["correlation_key"]
    vs = sorted((tuple(v.entity_key[f] for f in ck), v.verdict, v.at) for v in verdicts)
    return _Run(vs, engine.dropped_late, set(engine.retired_keys),
                set(engine.reclaimed_keys), engine.late_events)


@st.composite
def lc_case(draw):
    """Full lifecycle: terminal + optional TTL, one adversarial arrival."""
    events = _events_lc(draw(lc_triples))
    return draw(st.permutations(events)), draw(grace_strat), draw(ttl_strat), draw(lc_policies)


@st.composite
def lc_two_arrivals(draw):
    events = _events_lc(draw(lc_triples))
    return (draw(st.permutations(events)), draw(st.permutations(events)),
            draw(grace_strat), draw(ttl_strat), draw(lc_policies))


# --- Property 1 (centerpiece): terminal retirement, verdict+dropped == oracle -


@settings(max_examples=600, deadline=None)
@given(lc_case())
def test_terminal_engine_equals_oracle(case):
    # ttl=None: the verdict guarantee (matches the pure operator semantics over the
    # admitted canonical trace, with terminal retirement) is exact here.
    arrival, grace, _ttl, policy = case
    ov, od, _, _ = oracle_lifecycle(list(arrival), policy, grace, {TERMINAL}, None)
    run = _engine_run(arrival, policy, grace, None)
    assert run.verdicts == sorted(ov)
    assert _fps(run.dropped) == _fps(od)


# --- Property 2: retired set matches the oracle (terminal, exact) ------------


@settings(max_examples=600, deadline=None)
@given(lc_case())
def test_retired_set_matches_oracle(case):
    arrival, grace, _ttl, policy = case
    _, _, oret, _ = oracle_lifecycle(list(arrival), policy, grace, {TERMINAL}, None)
    assert _engine_run(arrival, policy, grace, None).retired == oret


# --- Property 3 (centerpiece): no silent change + full invariance incl TTL ----


@settings(max_examples=600, deadline=None)
@given(lc_two_arrivals())
def test_no_silent_verdict_change_under_lifecycle(case):
    arrival_a, arrival_b, grace, ttl, policy = case
    a = _engine_run(arrival_a, policy, grace, ttl)
    b = _engine_run(arrival_b, policy, grace, ttl)
    if a.verdicts != b.verdicts:
        assert _fps(a.dropped) != _fps(b.dropped)
        assert a.late_events > 0 or b.late_events > 0


@settings(max_examples=600, deadline=None)
@given(lc_two_arrivals())
def test_same_admitted_set_same_outcome_incl_ttl(case):
    # When two arrival orders admit the SAME set (same dropped), retirement AND
    # quiescence reclamation AND verdicts must be identical -- proving lifecycle is
    # decided on the canonical basis, not arrival order, even under TTL.
    arrival_a, arrival_b, grace, ttl, policy = case
    a = _engine_run(arrival_a, policy, grace, ttl)
    b = _engine_run(arrival_b, policy, grace, ttl)
    if _fps(a.dropped) == _fps(b.dropped):
        assert a.verdicts == b.verdicts
        assert a.retired == b.retired
        assert a.reclaimed == b.reclaimed


# --- Property 4: determinism (with TTL) + per-key independence (terminal) -----


@settings(max_examples=300, deadline=None)
@given(lc_case())
def test_determinism_under_lifecycle(case):
    arrival, grace, ttl, policy = case
    assert _engine_run(arrival, policy, grace, ttl).verdicts == \
        _engine_run(arrival, policy, grace, ttl).verdicts


@settings(max_examples=400, deadline=None)
@given(lc_triples, grace_strat, policies)
def test_per_key_independence_under_retirement(triples, grace, policy):
    # Canonical (drop-free) arrival + ttl=None, so admission and reclamation are not
    # confounders: this isolates per-key isolation under terminal retirement.
    events = canonical_sorted(_events_lc(triples))
    if not events:
        return
    horizon = max(e.event_time for e in events)
    interleaved = _engine_run(events, policy, grace, None)
    for key in {e.bindings["order_id"] for e in events}:
        alone = [e for e in events if e.bindings["order_id"] == key]
        alone = alone + [Event("clock.tick", horizon, {"order_id": key}, {}, "gen")]
        a = _engine_run(alone, policy, grace, None)
        kt = (key,)
        assert [v for v in a.verdicts if v[0] == kt] == \
            [v for v in interleaved.verdicts if v[0] == kt]
