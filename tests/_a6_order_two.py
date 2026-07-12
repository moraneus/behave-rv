"""A6 fixture: the SAME helpers as _a6_order_one, defined in swapped order.
No call or body changes -- fingerprints must be identical (order-independence
of the reachable-set hash)."""


def _has_key(event):
    return "order_id" in event.bindings


def _is_status(event, status):
    return event.type == "order.status" and event.payload.get("status") == status


def predicate(ctx, event, status):
    if _is_status(event, status) and _has_key(event):
        ctx.bind(order_id=event.bindings["order_id"])
        return True
    return False
