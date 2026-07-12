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


# -- package-attribute and boundary-form fixtures (mutation spot-check) -------

import json  # noqa: E402  (fixture imports live with their fixtures)

import parse  # noqa: E402
from parse import parse as third_party_by_name  # noqa: E402
from tests import _pkg_helper_mod  # noqa: E402


def pred_pkg_attr(ctx, event, status):
    """Calls a helper as module.attr where the module is a same-package
    sibling: the SECOND resolution form."""
    if _pkg_helper_mod.check(event, status):
        return True
    return False


def pred_third_party(ctx, event, status):
    """Module-attr into other top-level packages, and a third-party function
    imported by name: all must stay UNRESOLVED."""
    if third_party_by_name("{}", str(status)) or parse.parse("{}", "x"):
        return bool(json.dumps({"s": status}))
    return False


_TABLE = {"check": _pkg_helper_mod.check}

_lambda_helper = lambda event, status: event.type == "t"  # noqa: E731


def pred_dynamic_forms(ctx, event, status):
    """A subscripted call (no static name at all) and a lambda helper (no
    retrievable source): both must land in unresolved, never crash."""
    if _TABLE["check"](event, status) or _lambda_helper(event, status):
        return True
    return False


from tests._pkg_helper_mod import check as sibling_check  # noqa: E402


def pred_pkg_by_name(ctx, event, status):
    """A by-name import from a same-package SIBLING module: the Name branch's
    package clause (different module, same top-level package)."""
    if sibling_check(event, status):
        return True
    return False
