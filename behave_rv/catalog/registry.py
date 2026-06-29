"""The RV step registry: maps registered trigger/scope/obligation steps to catalog entries.

A registry holds the registered step functions and the :class:`CatalogEntry`
each one produces. The signature is computed simply for v1: the declared event
type and correlation key, plus the referenced fields derived from the phrasing
placeholders (the subset a policy can actually bind or read).

``step_id`` is author-assigned and stable across renames, and is never reused
within a registry.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Iterable

from behave_rv.catalog.entry import CatalogEntry, StepSignature

# parse-style placeholders: {name} or {name:type}
_PLACEHOLDER = re.compile(r"\{\s*(\w+)\s*(?::[^}]*)?\}")

Step = Callable[..., bool]


def referenced_fields(phrasing: str) -> set[str]:
    """The placeholder names a policy can bind in this phrasing."""
    return set(_PLACEHOLDER.findall(phrasing))


def _normalize_key(correlation_key: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(correlation_key, str):
        return (correlation_key,)
    return tuple(correlation_key)


class StepRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, CatalogEntry] = {}
        self._funcs: dict[str, Step] = {}

    # -- registration -------------------------------------------------------

    def register(
        self,
        kind: str,
        phrasing: str,
        func: Step,
        *,
        step_id: str,
        event_type: str,
        correlation_key: str | Iterable[str],
        provenance: str = "llm",
    ) -> CatalogEntry:
        if step_id in self._entries:
            raise ValueError(f"step_id {step_id!r} is already registered; ids are never reused")

        signature = StepSignature(
            event_type=event_type,
            trigger_condition=phrasing,
            payload_fields={},
            referenced_fields=referenced_fields(phrasing),
            correlation_key=_normalize_key(correlation_key),
        )
        entry = CatalogEntry(
            step_id=step_id,
            phrasing=phrasing,
            kind=kind,
            signature=signature,
            provenance=provenance,
            observed=False,
            version=1,
        )
        self._entries[step_id] = entry
        self._funcs[step_id] = func
        return entry

    def _decorator(self, kind: str, phrasing: str, **meta):
        def wrap(func: Step) -> Step:
            self.register(kind, phrasing, func, **meta)
            return func

        return wrap

    def trigger(self, phrasing: str, **meta):
        """A When. Matches an event and binds the correlation key. Returns bool."""
        return self._decorator("trigger", phrasing, **meta)

    def scope(self, phrasing: str, **meta):
        """A Given. A predicate over observable state that activates the policy."""
        return self._decorator("scope", phrasing, **meta)

    def obligation(self, phrasing: str, **meta):
        """A Then. The property evaluated continuously. No side effects."""
        return self._decorator("obligation", phrasing, **meta)

    # -- access -------------------------------------------------------------

    def get(self, step_id: str) -> CatalogEntry:
        return self._entries[step_id]

    def func_for(self, step_id: str) -> Step:
        return self._funcs[step_id]

    def entries(self) -> list[CatalogEntry]:
        return list(self._entries.values())
