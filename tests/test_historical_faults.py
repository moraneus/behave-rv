"""Directed regressions for the two historical ordering faults, independent of
Hypothesis. Both were found by property testing, but mutation testing showed
re-detection is probabilistic at committed case counts (~4/5 runs for the
release-boundary fault). These pin the exact minimal counterexamples across
every arrival permutation, so a regression fails deterministically.
"""

import itertools

from behave_rv.compile.automaton import before
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource


def _run(arrival, policy, grace):
    src = InProcessSource()
    for e in arrival:
        src.emit(e)
    return tuple(v.verdict for v in Engine([policy], grace=grace).run(src, emit_pending=True))


def _e(status, t):
    return Event("order.status", float(t), {"order_id": "A"}, {"status": status}, "t")


def test_same_timestamp_tiebreak_fault_stays_fixed():
    # Historical fault 1: the reorder heap broke event-time ties by arrival
    # sequence, so same-timestamp events were processed in arrival order and the
    # verdict depended on arrival. Minimal counterexample: prior and trigger at
    # the same timestamp; canonical content order puts 'paid' before 'shipped'.
    policy = before("p", correlation_key="order_id",
                    prior=lambda e: e.payload.get("status") == "paid",
                    trigger=lambda e: e.payload.get("status") == "shipped",
                    event_types={"order.status"})
    events = [_e("paid", 0.0), _e("shipped", 0.0)]
    outcomes = {_run(list(p), policy, grace=5) for p in itertools.permutations(events)}
    assert outcomes == {("satisfied",)}


def test_release_boundary_fault_stays_fixed():
    # Historical fault 2: releasable() released events AT the watermark, so a
    # same-timestamp sibling arriving later was emitted in a separate batch, out
    # of canonical order. The exact original counterexample, all 24 arrival
    # permutations: the verdict must be identical (canonical order has
    # cancelled@17 before paid@17, so the trigger fires with no prior: violated).
    policy = before("p", correlation_key="order_id",
                    prior=lambda e: e.payload.get("status") == "paid",
                    trigger=lambda e: e.payload.get("status") == "cancelled",
                    event_types={"order.status"})
    events = [_e("authorized", 0.0), _e("authorized", 19.0),
              _e("paid", 17.0), _e("cancelled", 17.0)]
    outcomes = {_run(list(p), policy, grace=2) for p in itertools.permutations(events)}
    assert outcomes == {("violated",)}
