"""Phase 0: prove behave's Gherkin parser is usable as a library, wrapped not edited.

The bridge adapts behave's parser/model without invoking its runner, so we reuse
the front end and replace the back end.
"""

from behave_rv.compile.parser_bridge import parse_feature

FEATURE = """
Feature: order lifecycle

  Scenario: an order is never cancelled after delivery
    Given after an order is "delivered"
    When an order is "cancelled"
    Then it must not happen
"""


def test_parse_feature_returns_feature_with_name():
    feature = parse_feature(FEATURE)
    assert feature.name == "order lifecycle"


def test_parse_feature_exposes_scenarios_and_steps():
    feature = parse_feature(FEATURE)
    (scenario,) = feature.scenarios

    assert scenario.name == "an order is never cancelled after delivery"
    assert [s.keyword for s in scenario.steps] == ["Given", "When", "Then"]
    assert scenario.steps[1].name == 'an order is "cancelled"'
