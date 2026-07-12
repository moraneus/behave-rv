"""A6 fixture: helper definitions in one order (see _a6_order_two)."""


def _is_status(event, status):
    return event.type == "order.status" and event.payload.get("status") == status


def _has_key(event):
    return "order_id" in event.bindings


def predicate(ctx, event, status):
    if _is_status(event, status) and _has_key(event):
        ctx.bind(order_id=event.bindings["order_id"])
        return True
    return False
