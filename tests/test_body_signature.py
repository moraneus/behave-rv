"""Fix 1: a body-level change to the trigger condition surfaces as a Break,
while a pure rename stays silent -- through the real registry/diff/notify path.
"""

from behave_rv.catalog.diff import classify_changes, signatures_equivalent
from behave_rv.catalog.registry import StepRegistry
from behave_rv.notify.channel import PolicyUse, notifications

USES = [PolicyUse("paid-after-authorized", "alice", frozenset({"order.status.is"}))]
OTHER = PolicyUse("fast-delivery", "bob", frozenset({"delivery.is"}))


def _v1():
    r = StepRegistry()

    @r.trigger('an order is "{status}"', step_id="order.status.is",
               event_type="order.status", correlation_key="order_id")
    def order_status_is(ctx, event, status):
        return event.type == "order.status" and event.payload.get("status") == status
    return r


def test_pure_rename_stays_silent():
    r2 = StepRegistry()

    @r2.trigger('an order is "{status}"', step_id="order.status.is",
                event_type="order.status", correlation_key="order_id")
    def matches_status(ctx, ev, status):  # function + parameter renamed only
        return ev.type == "order.status" and ev.payload.get("status") == status

    a, b = _v1().get("order.status.is").signature, r2.get("order.status.is").signature
    assert signatures_equivalent(a, b)
    n = notifications(_v1().entries(), r2.entries(), USES)
    assert n.breaks == [] and n.suggestions == [] and n.weakenings == []


def test_in_body_guard_change_breaks_and_is_scoped():
    r2 = StepRegistry()

    @r2.trigger('an order is "{status}"', step_id="order.status.is",
                event_type="order.status", correlation_key="order_id")
    def order_status_is(ctx, event, status):  # same name, NEW in-body guard
        return (event.type == "order.status" and event.payload.get("status") == status
                and event.payload.get("amount", 0) > 0)

    a, b = _v1().get("order.status.is").signature, r2.get("order.status.is").signature
    assert not signatures_equivalent(a, b)
    assert "amount" in b.payload_fields and "amount" not in a.payload_fields

    n = notifications(_v1().entries(), r2.entries(), [USES[0], OTHER])
    assert len(n.breaks) == 1
    assert n.breaks[0].owner == "alice"            # scoped to the policy that used it
    assert "amount" in n.breaks[0].detail or "condition" in n.breaks[0].detail
    assert [c.status for c in classify_changes(_v1().entries(), r2.entries())] == ["changed"]
