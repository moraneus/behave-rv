"""Resolving a feature step line against the catalog by stable step_id.

A line is matched against registered phrasings (primary + aliases) and bound by
step_id, not raw text -- so a rephrasing that maps to the same step_id resolves
to the same step and params.
"""

import pytest

from behave_rv.catalog.registry import StepRegistry


@pytest.fixture
def reg():
    r = StepRegistry()

    @r.trigger('an order is "{status}"', step_id="order.status.is",
               event_type="order.status", correlation_key="order_id")
    def _is(ctx, event, status):
        return event.type == "order.status" and event.payload.get("status") == status

    return r


def test_resolve_matches_a_registered_step_and_extracts_params(reg):
    (res,) = reg.resolve('an order is "paid"')
    assert res.step_id == "order.status.is"
    assert res.params == {"status": "paid"}
    assert res.signature.correlation_key == ("order_id",)


def test_resolved_function_is_the_registered_predicate(reg):
    (res,) = reg.resolve('an order is "paid"')
    from behave_rv.events.event import Event

    e = Event("order.status", 1.0, {"order_id": "A"}, {"status": "paid"}, "t")
    assert res.func(None, e, **res.params) is True


def test_rephrasing_via_alias_resolves_to_the_same_step_id(reg):
    reg.alias("order.status.is", 'the order becomes "{status}"')

    (a,) = reg.resolve('an order is "paid"')
    (b,) = reg.resolve('the order becomes "paid"')

    assert a.step_id == b.step_id == "order.status.is"
    assert a.params == b.params == {"status": "paid"}


def test_resolve_returns_empty_when_nothing_matches(reg):
    assert reg.resolve("something entirely unregistered") == []


def test_alias_for_unknown_step_id_is_rejected(reg):
    with pytest.raises(KeyError):
        reg.alias("does.not.exist", "whatever")


def test_ambiguous_text_resolves_to_multiple_step_ids(reg):
    @reg.trigger('an order is "{state}"', step_id="order.other",
                 event_type="order.status", correlation_key="order_id")
    def _other(ctx, event, state):
        return True

    resolutions = reg.resolve('an order is "paid"')
    assert {r.step_id for r in resolutions} == {"order.status.is", "order.other"}
