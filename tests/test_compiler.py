"""Compile a parsed .feature policy into a runnable compile.Policy.

Steps resolve against the catalog by stable step_id; the temporal words bind to
the automaton templates; the correlation key is taken from the resolved steps and
a scenario that needs more than one independent entity key is refused at compile
time. The produced Policy is identical in form to the ones the engine runs today.
"""

import pytest

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import CompileError, compile_feature
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource


@pytest.fixture
def reg():
    r = StepRegistry()

    @r.trigger('an order is "{status}"', step_id="order.status.is",
               event_type="order.status", correlation_key="order_id")
    def _order(ctx, event, status):
        if event.type == "order.status" and event.payload.get("status") == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    @r.trigger('a delivery is "{status}"', step_id="delivery.is",
               event_type="delivery.status", correlation_key="order_id")
    def _delivery(ctx, event, status):
        return event.type == "delivery.status" and event.payload.get("status") == status

    @r.trigger('a customer is "{tier}"', step_id="customer.is",
               event_type="customer.status", correlation_key="customer_id")
    def _customer(ctx, event, tier):
        return event.type == "customer.status" and event.payload.get("tier") == tier

    return r


def order_ev(status, t, order_id="A"):
    return Event("order.status", t, {"order_id": order_id}, {"status": status}, "test")


def run(policy, events):
    src = InProcessSource()
    for e in events:
        src.emit(e)
    return Engine([policy]).run(src)


# --- before ----------------------------------------------------------------

BEFORE_FEATURE = '''
Feature: payment safety
  Scenario: an order may only be paid after it was authorized
    When an order is "paid"
    Then an order is "authorized" before
'''


def test_compile_before_produces_a_single_key_policy(reg):
    (policy,) = compile_feature(BEFORE_FEATURE, reg)
    assert policy.correlation_key == ("order_id",)
    assert policy.event_types == frozenset({"order.status"})
    assert policy.policy_id == "an order may only be paid after it was authorized"


def test_compiled_before_satisfied_and_violated(reg):
    (policy,) = compile_feature(BEFORE_FEATURE, reg)

    satisfied = run(policy, [order_ev("authorized", 1.0), order_ev("paid", 2.0)])
    violated = run(policy, [order_ev("paid", 1.0, order_id="B")])

    assert [v.verdict for v in satisfied] == ["satisfied"]
    assert [v.verdict for v in violated] == ["violated"]


def test_compiled_before_pending_emits_nothing(reg):
    (policy,) = compile_feature(BEFORE_FEATURE, reg)
    assert run(policy, [order_ev("authorized", 1.0)]) == []


def test_policy_carries_authored_scenario_and_failing_step(reg):
    (policy,) = compile_feature(BEFORE_FEATURE, reg)
    assert policy.failing_step_index == 1  # the Then
    assert policy.authored_scenario.steps[1].name == 'an order is "authorized" before'


# --- never -----------------------------------------------------------------


def test_compile_never(reg):
    feature = '''
Feature: no cancellation
  Scenario: an order is never cancelled
    Then an order is "cancelled" never happens
'''
    (policy,) = compile_feature(feature, reg)
    assert [v.verdict for v in run(policy, [order_ev("cancelled", 1.0)])] == ["violated"]


def test_never_with_a_when_is_refused(reg):
    feature = '''
Feature: no cancellation
  Scenario: scoped never is out of fragment
    When an order is "placed"
    Then an order is "cancelled" never happens
'''
    with pytest.raises(CompileError, match="self-contained|must not have a When"):
        compile_feature(feature, reg)


# --- within ----------------------------------------------------------------


def test_compile_within(reg):
    feature = '''
Feature: fast delivery
  Scenario: a requested delivery is fulfilled within the deadline
    When a delivery is "requested"
    Then a delivery is "fulfilled" within "30" seconds
'''
    (policy,) = compile_feature(feature, reg)
    src = InProcessSource()
    src.emit(Event("delivery.status", 1.0, {"order_id": "A"}, {"status": "requested"}, "t"))
    src.emit(Event("delivery.status", 10.0, {"order_id": "A"}, {"status": "fulfilled"}, "t"))
    assert [v.verdict for v in Engine([policy]).run(src)] == ["satisfied"]


# --- fragment boundary + errors --------------------------------------------


def test_two_independent_keys_are_refused(reg):
    feature = '''
Feature: cross entity
  Scenario: mixes an order and a customer
    When an order is "paid"
    Then a customer is "gold" before
'''
    with pytest.raises(CompileError, match="entity key"):
        compile_feature(feature, reg)


def test_unresolved_step_is_refused(reg):
    feature = '''
Feature: unknown
  Scenario: uses an unregistered step
    Then the moon is "blue" never happens
'''
    with pytest.raises(CompileError, match="no registered step"):
        compile_feature(feature, reg)


def test_scope_given_is_reported_as_not_wired(reg):
    feature = '''
Feature: scoped
  Scenario: uses a Given
    Given an order is "created"
    When an order is "paid"
    Then an order is "authorized" before
'''
    with pytest.raises(CompileError, match="scope|Given"):
        compile_feature(feature, reg)


# --- rephrasing binds by step_id -------------------------------------------


def test_rephrasing_with_same_step_id_compiles_and_runs_identically(reg):
    reg.alias("order.status.is", 'the order reaches "{status}"')
    rephrased = '''
Feature: payment safety (reworded)
  Scenario: an order may only be paid after it was authorized
    When the order reaches "paid"
    Then the order reaches "authorized" before
'''
    (canonical,) = compile_feature(BEFORE_FEATURE, reg)
    (reworded,) = compile_feature(rephrased, reg)

    trace = [order_ev("paid", 1.0)]
    assert [v.verdict for v in run(canonical, trace)] == ["violated"]
    assert [v.verdict for v in run(reworded, trace)] == ["violated"]
    assert reworded.correlation_key == canonical.correlation_key
