"""Fix 3: the observed flag + liveness harvest (defense against silent telemetry gaps).

The engine records which event types it actually saw; the registry flips a step's
`observed` flag the first time its event type appears and reports steps never
seen; and compiling a policy that depends on a never-observed step warns the
author before deployment rather than accepting it silently.
"""

import warnings

import pytest

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import UncheckablePolicyWarning, compile_feature
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource


def registry():
    r = StepRegistry()

    @r.trigger('an order is "{status}"', step_id="order.status.is",
               event_type="order.status", correlation_key="order_id")
    def order(ctx, event, status):
        return event.type == "order.status" and event.payload.get("status") == status

    @r.trigger('a refund is "{status}"', step_id="refund.is",
               event_type="refund.status", correlation_key="order_id")
    def refund(ctx, event, status):
        return event.type == "refund.status" and event.payload.get("status") == status
    return r


FEATURE = '''Feature: refund safety
  Scenario: a refund is only issued after authorization
    When a refund is "issued"
    Then a refund is "authorized" before
'''


def _run_over_orders_only(reg):
    from behave_rv.compile.automaton import never
    pol = never("x", correlation_key="order_id", event_types={"order.status"},
                bad=lambda e: False)
    src = InProcessSource()
    src.emit(Event("order.status", 1.0, {"order_id": "A"}, {"status": "paid"}, "t"))
    eng = Engine([pol])
    eng.run(src)
    return eng


def test_replay_flags_a_never_observed_step():
    reg = registry()
    eng = _run_over_orders_only(reg)   # stream contains only order.status events

    unobserved = reg.mark_observed(eng.observed_types)

    assert reg.get("order.status.is").observed is True
    assert reg.get("refund.is").observed is False
    assert [e.step_id for e in unobserved] == ["refund.is"]


def test_observed_stays_false_until_the_event_appears():
    reg = registry()
    assert reg.get("refund.is").observed is False   # nothing seen yet
    reg.mark_observed({"refund.status"})             # now it appears
    assert reg.get("refund.is").observed is True


def test_compile_warns_when_a_policy_depends_on_an_unobserved_step():
    reg = registry()
    with pytest.warns(UncheckablePolicyWarning, match="refund.status"):
        compile_feature(FEATURE, reg, observed_event_types={"order.status"})


def test_compile_is_silent_when_the_step_was_observed():
    reg = registry()
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning becomes an error
        compile_feature(FEATURE, reg, observed_event_types={"refund.status"})
