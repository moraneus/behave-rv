"""A raising predicate matches nothing, is recorded, and never kills the run.

Interrogation finding C3: one step author's bug (a KeyError inside a predicate)
crashed the whole engine with nothing recorded. The engine now contains it,
mirroring the sink-failure policy: contain, record, continue.
"""

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.automaton import PredicateError
from behave_rv.compile.compiler import compile_feature
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource


def _reg():
    r = StepRegistry()

    @r.trigger('an order is "{status}"', step_id="broken.step",
               event_type="order.status", correlation_key="order_id")
    def broken(ctx, e, status):
        return e.payload["missing_field"] == status     # the step author's bug

    @r.trigger('a delivery is "{status}"', step_id="healthy.step",
               event_type="order.status", correlation_key="order_id")
    def healthy(ctx, e, status):
        return e.payload.get("status") == status

    return r


FEAT = ('Feature: f\n'
        '  Scenario: broken policy\n    Then an order is "cancelled" never happens\n'
        '  Scenario: healthy policy\n    Then a delivery is "cancelled" never happens\n')


def test_raising_predicate_is_contained_recorded_and_isolated():
    policies = compile_feature(FEAT, _reg())
    src = InProcessSource()
    src.emit(Event("order.status", 1.0, {"order_id": "A"}, {"status": "cancelled"}, "t"))

    engine = Engine(policies)
    verdicts = engine.run(src, emit_pending=True)        # must not raise

    # the healthy policy on the same stream still produced its correct verdict
    healthy = [(v.policy_id, v.verdict) for v in verdicts if v.policy_id == "healthy policy"]
    assert healthy == [("healthy policy", "violated")]

    # the broken policy matched nothing (its predicate raised) -> pending
    broken = [(v.policy_id, v.verdict) for v in verdicts if v.policy_id == "broken policy"]
    assert broken == [("broken policy", "pending")]

    # and the failure is visible, naming the step and the policy
    assert engine.predicate_errors == 1
    assert isinstance(engine.first_predicate_error, PredicateError)
    assert engine.predicate_error_sources == [("broken policy", "broken.step")]
