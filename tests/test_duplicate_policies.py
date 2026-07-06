"""A duplicate policy id must be refused, never silently dropped.

Interrogation finding A4: two scenarios sharing a name compiled to two policies
with one id, and the engine's id-keyed dict silently replaced the first -- its
violations were never emitted. Both layers now refuse.
"""

import pytest

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.automaton import never
from behave_rv.compile.compiler import CompileError, compile_feature
from behave_rv.engine.loop import Engine


@pytest.fixture
def reg():
    r = StepRegistry()

    @r.trigger('an order is "{status}"', step_id="s",
               event_type="order.status", correlation_key="order_id")
    def f(ctx, e, status):
        return e.payload.get("status") == status

    return r


def test_compiler_refuses_duplicate_scenario_names(reg):
    feat = ('Feature: f\n'
            '  Scenario: same name\n    Then an order is "cancelled" never happens\n'
            '  Scenario: same name\n    Then an order is "refunded" never happens\n')
    with pytest.raises(CompileError, match="duplicate scenario name 'same name'"):
        compile_feature(feat, reg)


def test_engine_refuses_duplicate_policy_ids():
    p1 = never("dup", correlation_key="order_id", event_types={"order.status"},
               bad=lambda e: False)
    p2 = never("dup", correlation_key="order_id", event_types={"order.status"},
               bad=lambda e: True)
    with pytest.raises(ValueError, match="duplicate policy_id"):
        Engine([p1, p2])
