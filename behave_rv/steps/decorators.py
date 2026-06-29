"""The RV step decorators: trigger (When), scope (Given), obligation (Then).

In `behave` a step acts and asserts. In RV a step is pure: it observes and
returns a boolean. trigger/scope/obligation functions are side-effect free and
deterministic. They never mutate the outside world.

This module exposes the ergonomic, module-level decorators bound to a process
``default_registry``. Tests and isolated tools can instead instantiate their own
:class:`~behave_rv.catalog.registry.StepRegistry` and use its decorator methods.
"""

from __future__ import annotations

from behave_rv.catalog.registry import StepRegistry

default_registry = StepRegistry()


def trigger(phrasing: str, **meta):
    """A When. Matches an event and binds the correlation key. Returns bool."""
    return default_registry.trigger(phrasing, **meta)


def scope(phrasing: str, **meta):
    """A Given. A predicate over observable state that activates the policy."""
    return default_registry.scope(phrasing, **meta)


def obligation(phrasing: str, **meta):
    """A Then. The property evaluated continuously. No side effects."""
    return default_registry.obligation(phrasing, **meta)
