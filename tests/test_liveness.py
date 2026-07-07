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


# --- value-level liveness (closes H1) ----------------------------------------


def _run_stream(statuses):
    """Run order.status events with the given statuses; return the engine."""
    from behave_rv.compile.automaton import never

    pol = never("x", correlation_key="order_id", event_types={"order.status"},
                bad=lambda e: False)
    src = InProcessSource()
    for i, s in enumerate(statuses):
        src.emit(Event("order.status", float(i), {"order_id": "A"}, {"status": s}, "t"))
    engine = Engine([pol])
    engine.run(src)
    return engine


ORDER_POLICY = ('Feature: f\n  Scenario: order is never cancelled\n'
                '    Then an order is "cancelled" never happens\n')


def test_value_level_warning_when_the_value_never_appears():
    # the event TYPE appears, the concrete VALUE does not: this used to pass
    # silently (the value-granularity gap); now it warns, naming field + value.
    reg = registry()
    engine = _run_stream(["placed", "paid"])            # no "cancelled" anywhere
    with pytest.warns(UncheckablePolicyWarning, match="status='cancelled'"):
        compile_feature(ORDER_POLICY, reg,
                        observed_event_types=engine.observed_types,
                        observed_values=engine.observed_values)


def test_value_level_silent_when_the_value_appears():
    reg = registry()
    engine = _run_stream(["placed", "cancelled"])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        compile_feature(ORDER_POLICY, reg,
                        observed_event_types=engine.observed_types,
                        observed_values=engine.observed_values)


def test_h1_value_rename_chain_is_now_caught():
    # THE standing H1 finding: the service renamed 'locked' to 'LOCKED'; the
    # scoped policy silently stopped matching and every defense stayed quiet.
    # Value-level liveness now warns at compile against the observed stream.
    r = StepRegistry()

    @r.trigger('a user is "{status}"', step_id="user.is",
               event_type="session.status", correlation_key="user_id")
    def user_is(ctx, e, status):
        return e.type == "session.status" and e.payload.get("status") == status

    POLICY = ('Feature: f\n  Scenario: a locked user must never act\n'
              '    Given a user is "locked"\n'
              '    Then a user is "action" never happens\n')

    from behave_rv.compile.automaton import never
    pol = never("x", correlation_key="user_id", event_types={"session.status"},
                bad=lambda e: False)
    src = InProcessSource()
    for i, s in enumerate(["login_ok", "login_fail", "LOCKED", "action"]):  # the rename
        src.emit(Event("session.status", float(i), {"user_id": "u1"}, {"status": s}, "t"))
    engine = Engine([pol])
    engine.run(src)

    with pytest.warns(UncheckablePolicyWarning, match="status='locked'"):
        compile_feature(POLICY, r,
                        observed_event_types=engine.observed_types,
                        observed_values=engine.observed_values)
