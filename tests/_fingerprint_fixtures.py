"""Real-module fixtures for the call-graph fingerprint tests: inspect.getsource
needs functions defined in a file, and swapping the active helper through a
module global mirrors how an agent's edit changes which body is reachable."""

from behave_rv.catalog.condition import fingerprint_bundle  # noqa: F401


def helper_v1(event, status):
    return event.type == "t" and event.payload.get("status") == status


def helper_v2(event, status):
    return event.type == "t" and event.payload.get("status") == status.upper()


def helper_v1_reformatted(event, status):
    return (
        event.type == "t"
        and event.payload.get("status") == status
    )


def helper_v1_locals_renamed(incoming, wanted):
    return incoming.type == "t" and incoming.payload.get("status") == wanted


_ACTIVE = helper_v1
_INNER = None


def pred(ctx, event, status):
    if _ACTIVE(event, status):
        return True
    return False


def bundle_with(predicate, active=None, inner=None):
    global _ACTIVE, _INNER
    saved_active, saved_inner = _ACTIVE, _INNER
    if active is not None:
        _ACTIVE = active
    if inner is not None:
        _INNER = inner
    try:
        return fingerprint_bundle(predicate)
    finally:
        _ACTIVE, _INNER = saved_active, saved_inner


def _cycle_a(event):
    return _cycle_b(event)


def _cycle_b(event):
    return _cycle_a(event) or event.type == "t"


def pred_cyclic(ctx, event, status):
    return _cycle_a(event)


def pred_dynamic(ctx, event, status, check=helper_v1):
    return check(event, status) and event.payload.get("x")


def inner_v1(event):
    return event.type == "t"


def inner_v2(event):
    return event.type == "u"


_INNER = inner_v1


def _inner(event):
    return _INNER(event)


def _outer(event):
    return _inner(event)


def pred_deep(ctx, event, status):
    return _outer(event)
