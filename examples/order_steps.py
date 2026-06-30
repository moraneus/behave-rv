"""The agent's monitorable surface for the order example.

Importing this module registers the RV steps into the default registry. This is
the only Python in the policy path -- the policy itself is authored in Gherkin.
"""

from behave_rv.steps import default_registry, trigger


@trigger('an order is "{status}"', step_id="order.status.is",
         event_type="order.status", correlation_key="order_id")
def order_status_is(ctx, event, status):
    """Matches an order.status event carrying the given status, binding order_id."""
    if event.type == "order.status" and event.payload.get("status") == status:
        ctx.bind(order_id=event.bindings["order_id"])
        return True
    return False


# A second wording for the SAME step_id. A policy may use either phrasing; both
# bind to order.status.is, so a rephrasing flows through untouched.
default_registry.alias("order.status.is", 'the order reaches "{status}"')


@trigger('a customer is "{tier}"', step_id="customer.tier.is",
         event_type="customer.status", correlation_key="customer_id")
def customer_tier_is(ctx, event, tier):
    """A separately-keyed entity (customer_id), used only to show the single-key
    fragment boundary being enforced at compile time."""
    return event.type == "customer.status" and event.payload.get("tier") == tier
