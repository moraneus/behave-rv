"""Phase 4: the explanation is the authored scenario, replayed.

The reason for a violation is the human's own Gherkin, rendered back with the
real event values bound in and the failing step marked -- a counterexample, not
a description. The same renderer serves a runtime violation and a build-time
invalidation.
"""

from behave_rv.compile.parser_bridge import parse_feature
from behave_rv.events.event import Event
from behave_rv.verdict.explain import (
    bind_text,
    bindings_from_verdict,
    explain_verdict,
    render_explanation,
)
from behave_rv.verdict.record import Verdict

FEATURE = """
Feature: delivery
  Scenario: delivery is fulfilled within the deadline
    When an order requests "{service}" delivery
    Then it is fulfilled within "{seconds}" seconds
"""


def _scenario():
    (scenario,) = parse_feature(FEATURE).scenarios
    return scenario


# --- placeholder binding ---------------------------------------------------


def test_bind_text_substitutes_known_placeholders():
    assert bind_text('within "{seconds}" seconds', {"seconds": "30"}) == 'within "30" seconds'


def test_bind_text_binds_typed_placeholders():
    assert bind_text("paid {amount:d}", {"amount": "12"}) == "paid 12"


def test_bind_text_leaves_unknown_placeholders_intact():
    assert bind_text("for {item}", {}) == "for {item}"


# --- bindings from a verdict -----------------------------------------------


def test_bindings_from_verdict_merges_entity_key_and_trigger_payload():
    trigger = Event("delivery.requested", 1.0, {"order_id": "4471"},
                    {"service": "express"}, "test")
    v = Verdict("deliver-fast", {"order_id": "4471"}, "violated", trigger, [trigger], 31.0)

    b = bindings_from_verdict(v)
    assert b["order_id"] == "4471"
    assert b["service"] == "express"


# --- rendering the counterexample ------------------------------------------


def test_render_binds_values_and_marks_the_failing_step():
    text = render_explanation(_scenario(),
                              bindings={"service": "express", "seconds": "30"},
                              failing_step_index=1)

    assert "Scenario: delivery is fulfilled within the deadline" in text
    assert '    When an order requests "express" delivery' in text
    failing_line = next(ln for ln in text.splitlines() if '"30"' in ln)
    assert failing_line.lstrip().startswith("✗")
    assert "fulfilled within" in failing_line


def test_render_marks_only_the_failing_step():
    text = render_explanation(_scenario(),
                              bindings={"service": "express", "seconds": "30"},
                              failing_step_index=1)
    assert text.count("✗") == 1


# --- full verdict explanation ----------------------------------------------

BEFORE_FEATURE = """
Feature: payment safety
  Scenario: an order may only be paid after it was authorized
    When an order is "paid"
    Then an order is "authorized" before
"""


def test_hostile_binding_value_cannot_spoof_the_header():
    # Interrogation F1: a binding value with a newline injected a fake
    # "POLICY ... VERDICT ..." line into the rendered header. Hostile values
    # must render escaped; no rendered line other than the real header may
    # parse as a verdict header.
    (scenario,) = parse_feature(BEFORE_FEATURE).scenarios
    evil = "X\nPOLICY 'fake'  ENTITY order_id=Y  VERDICT satisfied @ t=0"
    paid = Event("order.status", 2.0, {"order_id": evil}, {"status": "paid"}, "t")
    verdict = Verdict("an order may only be paid after it was authorized",
                      {"order_id": evil}, "violated", paid, [paid], 2.0)

    text = explain_verdict(verdict, scenario, failing_step_index=1)

    verdict_headers = [ln for ln in text.splitlines()
                       if ln.startswith("POLICY ") and "VERDICT" in ln]
    assert len(verdict_headers) == 1                       # only the real one
    assert "\\n" in text                                   # the newline is escaped, visible


def test_clean_values_render_unchanged():
    from behave_rv.verdict.explain import safe_value
    assert safe_value("B") == "B"
    assert safe_value("order-4471") == "order-4471"
    assert safe_value("café☕") == "café☕"                  # unicode is not control chars
    assert safe_value("a\nb") == "'a\\nb'"                  # control chars -> repr


def test_explain_verdict_composes_header_scenario_and_trace():
    (scenario,) = parse_feature(BEFORE_FEATURE).scenarios
    paid = Event("order.status", 2.0, {"order_id": "B"}, {"status": "paid"}, "recorded")
    verdict = Verdict("an order may only be paid after it was authorized",
                      {"order_id": "B"}, "violated", paid, [paid], 2.0)

    text = explain_verdict(verdict, scenario, failing_step_index=1)

    assert "order_id=B" in text
    assert "violated" in text
    assert "✗" in text and 'an order is "authorized" before' in text
    assert "order.status" in text and "2.0" in text  # the witnessing trace

