"""The RV step registry: maps registered trigger/scope/obligation steps to catalog entries.

A registry holds the registered step functions and the :class:`CatalogEntry`
each one produces. The signature captures the declared event
type and correlation key, plus the referenced fields derived from the phrasing
placeholders (the subset a policy can actually bind or read).

``step_id`` is author-assigned and stable across renames, and is never reused
within a registry.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Iterable

import parse

from behave_rv.catalog.condition import condition_fingerprint
from behave_rv.catalog.condition import payload_fields as extract_payload_fields
from behave_rv.catalog.entry import CatalogEntry, StepSignature

# parse-style placeholders: {name} or {name:type}
_PLACEHOLDER = re.compile(r"\{\s*(\w+)\s*(?::[^}]*)?\}")

Step = Callable[..., bool]


@dataclass(frozen=True)
class Resolution:
    """A feature step line resolved to a registered step, bound by step_id."""

    step_id: str
    func: Step
    params: dict[str, Any]
    signature: StepSignature
    phrasing: str  # the phrasing (primary or alias) that matched


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
        self._aliases: dict[str, list[str]] = {}

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
        signature = StepSignature(
            event_type=event_type,
            trigger_condition=phrasing,
            payload_fields=extract_payload_fields(func),
            referenced_fields=referenced_fields(phrasing),
            correlation_key=_normalize_key(correlation_key),
            condition_fingerprint=condition_fingerprint(func),
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

        existing = self._entries.get(step_id)
        if existing is not None:
            # A reload of the same step (identical kind, phrasing, signature) is a
            # no-op; a genuinely different step under the same id is a reuse error.
            if (existing.kind, existing.phrasing, existing.signature) == (
                entry.kind,
                entry.phrasing,
                entry.signature,
            ):
                return existing
            raise ValueError(
                f"step_id {step_id!r} is already registered with a different "
                "signature; ids are never reused"
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

    def alias(self, step_id: str, phrasing: str) -> None:
        """Register another phrasing for an existing step_id.

        A human may phrase the same step differently; an alias keeps both
        wordings resolving to the one stable step_id.
        """
        if step_id not in self._entries:
            raise KeyError(f"cannot alias unknown step_id {step_id!r}")
        self._aliases.setdefault(step_id, []).append(phrasing)

    def resolve(self, text: str) -> list[Resolution]:
        """Resolve a feature step line to registered steps by matching phrasings.

        Returns one :class:`Resolution` per distinct matching step_id (a step is
        matched against its primary phrasing and any aliases). The caller decides
        what to do with zero or multiple matches.
        """
        resolutions: list[Resolution] = []
        for step_id, entry in self._entries.items():
            for phrasing in (entry.phrasing, *self._aliases.get(step_id, [])):
                match = parse.parse(phrasing, text)
                if match is not None:
                    resolutions.append(
                        Resolution(
                            step_id=step_id,
                            func=self._funcs[step_id],
                            params=dict(match.named),
                            signature=entry.signature,
                            phrasing=phrasing,
                        )
                    )
                    break  # one match per step_id is enough
        return resolutions

    # -- access -------------------------------------------------------------

    def mark_observed(self, observed_types: Iterable[str]) -> list[CatalogEntry]:
        """Flip ``observed`` for steps whose event type appears in ``observed_types``.

        Returns the steps still never observed -- likely dead or wrong, or an
        event the system never actually emits (a silent telemetry gap).
        """
        seen = set(observed_types)
        for entry in self._entries.values():
            if entry.signature.event_type in seen:
                entry.observed = True
        return [e for e in self._entries.values() if not e.observed]

    def get(self, step_id: str) -> CatalogEntry:
        return self._entries[step_id]

    def entries(self) -> list[CatalogEntry]:
        return list(self._entries.values())
