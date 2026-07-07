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


def test_engine_runs_every_one_of_many_policies(reg):
    # Audit mutation N1 (an engine silently dropping a policy once handed 4+)
    # was invisible: nothing constructed more than 3 policies. Deterministic
    # pin: 6 policies, one violating event each, all 6 verdicts must appear.
    from behave_rv.engine.loop import Engine
    from behave_rv.events.event import Event
    from behave_rv.events.sources.inprocess import InProcessSource

    lines = ["Feature: many"]
    for i in range(6):
        lines.append(f'  Scenario: rule {i}\n    Then an order is "s{i}" never happens')
    policies = compile_feature("\n".join(lines) + "\n", reg)

    src = InProcessSource()
    for i in range(6):
        src.emit(Event("order.status", float(i), {"order_id": f"o{i}"},
                       {"status": f"s{i}"}, "t"))
    verdicts = Engine(policies).run(src, emit_pending=False)

    assert sorted(v.policy_id for v in verdicts) == [f"rule {i}" for i in range(6)]
    assert all(v.verdict == "violated" for v in verdicts)


def test_engine_refuses_duplicate_policy_ids():
    p1 = never("dup", correlation_key="order_id", event_types={"order.status"},
               bad=lambda e: False)
    p2 = never("dup", correlation_key="order_id", event_types={"order.status"},
               bad=lambda e: True)
    with pytest.raises(ValueError, match="duplicate policy_id"):
        Engine([p1, p2])
