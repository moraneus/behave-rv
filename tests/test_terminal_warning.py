"""Unbounded eventualities are accepted, not refused -- so running them with no
terminal event configured must be a visible choice, not a silent one."""

import warnings

import pytest

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import compile_feature
from behave_rv.engine.loop import Engine, NoTerminalConfiguredWarning


def registry():
    reg = StepRegistry()

    @reg.trigger('an order is "{status}"', step_id="order.status.is",
                 event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        return event.type == "order.status" and event.payload.get("status") == status

    return reg


SETTLE_AT_TERMINAL = """
Feature: f
  Scenario: eventually invoiced
    Then an order is "invoiced" has happened

  Scenario: always clean
    Then an order is "clean" always holds

  Scenario: reviewed since flagged
    Then an order is "reviewed" since an order is "flagged"
"""

SAFETY_ONLY = """
Feature: f
  Scenario: paid after authorized
    When an order is "paid"
    Then an order is "authorized" before
"""


def test_engine_warns_for_terminal_settled_policies_without_a_terminal():
    policies = compile_feature(SETTLE_AT_TERMINAL, registry())
    with pytest.warns(NoTerminalConfiguredWarning, match="pending indefinitely") as caught:
        Engine(policies)
    message = str(caught[0].message)
    for policy_id in ("eventually invoiced", "always clean", "reviewed since flagged"):
        assert policy_id in message


def test_no_warning_with_a_terminal_or_for_safety_policies():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        Engine(compile_feature(SETTLE_AT_TERMINAL, registry()),
               terminal_event_types={"order.done"})
        Engine(compile_feature(SAFETY_ONLY, registry()))
