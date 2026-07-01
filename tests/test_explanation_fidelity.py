"""Explanation fidelity: the rendered explanation must always contain the exact
events the operator used to decide, no matter how old they are.

For every generated trace and policy that yields a satisfied or violated verdict,
the verdict's deciding_events (which the renderer always shows) must equal the
events an independent oracle identifies as decisive. This pins the guarantee so a
deciding event can never again fall out of the bounded recent-context window.
"""

from __future__ import annotations

from hypothesis import given, settings

from behave_rv.engine.loop import Engine
from behave_rv.events.sources.inprocess import InProcessSource
from tests.oracle import canonical_sorted, decisive_for_key
from tests.test_properties import _build, _events, policies, triples


def _fp(e):
    return (e.event_time, e.type, tuple(sorted(e.bindings.items())),
            tuple(sorted(e.payload.items())), e.source)


def _fps(events):
    return sorted(_fp(e) for e in events)


@settings(max_examples=600, deadline=None)
@given(triples, policies)
def test_explanation_contains_exactly_the_deciding_events(tr, policy):
    events = _events(tr)
    src = InProcessSource()
    for e in events:
        src.emit(e)
    verdicts = Engine([_build(policy)]).run(src, emit_pending=True)

    horizon = max((e.event_time for e in events), default=None)
    by_key: dict = {}
    for e in events:
        by_key.setdefault(e.bindings["order_id"], []).append(e)

    for v in verdicts:
        if v.verdict not in ("satisfied", "violated"):
            continue
        key = v.entity_key["order_id"]
        exp_verdict, exp_decisive = decisive_for_key(
            policy, canonical_sorted(by_key[key]), horizon)
        assert exp_verdict == v.verdict            # oracle agrees on the verdict
        assert _fps(v.deciding_events) == _fps(exp_decisive)  # and on the deciding events
