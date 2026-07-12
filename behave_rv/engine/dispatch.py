"""Dispatcher: index by event type to candidate policies, then by key to the live instance.

This is the hot path. Keep it allocation light. The dispatcher does not own the
instances; it answers "which policies care about this event type" and "what is
this event's key for a given policy".
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Optional

from behave_rv.compile.automaton import Policy
from behave_rv.events.event import Event


class Dispatcher:
    def __init__(self, policies: Iterable[Policy]) -> None:
        self._by_type: dict[str, list[Policy]] = defaultdict(list)
        for policy in policies:
            for event_type in policy.event_types:
                self._by_type[event_type].append(policy)

    def candidates(self, event: Event) -> list[Policy]:
        return self._by_type.get(event.type, [])

    @staticmethod
    def key_of(policy: Policy, event: Event) -> Optional[tuple[str, ...]]:
        """The event's correlation key for this policy, or None if not carried."""
        try:
            return tuple(event.bindings[field] for field in policy.correlation_key)
        except KeyError:
            return None
