"""Sibling module for package-attribute resolution tests: the fixture predicate
calls tests._pkg_helper_mod.check(...) -- same top-level package."""


def check(event, status):
    return event.type == "order.status" and event.payload.get("status") == status
