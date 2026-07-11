"""Claim 2, asserted: rules bind to a stable behavioral identity, renames flow
through untouched, contract changes surface as scoped Breaks, and the two
changes signatures cannot see are caught by value-level liveness.

The scenarios live in evolution.py; run `python -m demo.order_service.evolution`
for the narrated version of exactly these assertions.
"""

from behave_rv.catalog.diff import classify_changes
from behave_rv.notify.channel import notifications

from demo.order_service.evolution import (
    NEW_AGENT_TESTS,
    NORMAL_FLOWS,
    OLD_AGENT_TESTS,
    OrderService,
    OrderServiceV4,
    OrderServiceV5,
    build_registry,
    build_registry_v2,
    build_registry_v3,
    build_registry_v6,
    committed_catalog,
    harvest_observed_values,
    liveness_warnings,
    load_policies,
    policy_uses,
    record_trace,
    run_verdicts,
)


def _uses():
    return policy_uses(load_policies(build_registry()))


def test_act0_committed_catalog_matches_the_code():
    # THE CI GATE: steps.py cannot drift from the committed contract silently.
    # A contract change without a catalog update (and thus a reviewable diff
    # plus notifications) fails right here.
    changes = classify_changes(committed_catalog(), build_registry().entries())
    assert {c.status for c in changes} == {"unchanged"}


def test_act1_pure_refactor_is_absorbed_silently():
    v2 = build_registry_v2()
    changes = classify_changes(committed_catalog(), v2.entries())
    # renamed, never "changed": the contract is intact
    assert {c.status for c in changes} == {"renamed"}
    notes = notifications(committed_catalog(), v2.entries(), _uses())
    assert notes.breaks == [] and notes.suggestions == [] and notes.weakenings == []


def test_act1_policies_produce_identical_verdicts_after_the_refactor():
    trace = record_trace(OrderService, NORMAL_FLOWS + ["bug_pay_without_auth"])
    before = run_verdicts(load_policies(build_registry()), trace)
    after = run_verdicts(load_policies(build_registry_v2()), trace)
    assert before == after and len(after) > 0


def test_act2_contract_change_breaks_and_is_scoped_to_every_using_policy():
    v3 = build_registry_v3()
    changes = classify_changes(committed_catalog(), v3.entries())
    assert {c.status for c in changes} == {"changed"}
    notes = notifications(committed_catalog(), v3.entries(), _uses())
    all_policies = {p.policy_id for p in load_policies(build_registry())}
    assert {b.policy_id for b in notes.breaks} == all_policies      # scoped: all 11 use it
    assert all(b.step_id == "order.status.is" for b in notes.breaks)
    # the detail is a human-readable contract diff, not just a flag
    detail = notes.breaks[0].detail
    assert "'order.status' -> 'order.state'" in detail
    assert "payload_fields" in detail


def test_act3_value_rename_is_invisible_to_the_diff_but_caught_by_liveness():
    # the v4 service renames the VALUE "paid" -> "charged"; the registry is
    # untouched, so the signature diff must stay silent -- honestly
    notes = notifications(committed_catalog(), build_registry().entries(), _uses())
    assert notes.breaks == []
    # ...and value-level liveness against an observed v4 stream catches it
    baseline = liveness_warnings(harvest_observed_values(
        record_trace(OrderService, NORMAL_FLOWS)))
    v4 = liveness_warnings(harvest_observed_values(
        record_trace(OrderServiceV4, NORMAL_FLOWS)))
    delta = v4 - baseline
    assert delta, "the value rename must surface as new liveness warnings"
    assert all("status='paid'" in w for w in delta)
    warned_policies = {w.split("'")[1] for w in delta}
    assert warned_policies == {"an order may only be paid after it was authorized",
                               "a shipment may only follow payment",
                               "an authorized order is paid within the window"}


def test_act4_dropped_emission_is_invisible_to_the_diff_but_caught_by_liveness():
    baseline = liveness_warnings(harvest_observed_values(
        record_trace(OrderService, NORMAL_FLOWS)))
    v5 = liveness_warnings(harvest_observed_values(
        record_trace(OrderServiceV5, NORMAL_FLOWS)))
    delta = v5 - baseline
    assert delta and all("status='invoiced'" in w for w in delta)
    assert any("eventually invoiced" in w for w in delta)


def test_act5_suggestion_and_weakening_channels_never_blur_with_breaks():
    v6 = build_registry_v6()
    notes = notifications(committed_catalog(), v6.entries(), _uses(),
                          old_tests=OLD_AGENT_TESTS, new_tests=NEW_AGENT_TESTS)
    assert notes.breaks == []                                        # a proposal warns no one
    assert [s.step_id for s in notes.suggestions] == ["order.return.recorded"]
    assert len(notes.weakenings) == 1
    assert "5s" in notes.weakenings[0].detail and "30s" in notes.weakenings[0].detail
