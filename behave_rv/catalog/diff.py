"""Signature equivalence and break classification (rename vs break). Bias hard toward surfacing.

Two versions of a step are equivalent when, over every execution, they would emit
the same events with the same bindings at the same points. The signature is a
computable approximation: the behavioral fields are the event type, the
referenced fields, the correlation key, and the exposed payload fields. The
phrasing (and the ``trigger_condition`` field that mirrors it) is representational
and excluded from equivalence, so a rename flows through silently.

When a behavioral field changes, the step is a candidate break. When the change
cannot be proven representational, it is treated as a break -- a false alarm
costs a glance; a missed alarm costs a dormant policy the human still trusts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from behave_rv.catalog.entry import CatalogEntry, StepSignature

# classification statuses
UNCHANGED = "unchanged"
RENAMED = "renamed"
CHANGED = "changed"
ADDED = "added"
REMOVED = "removed"


def signatures_equivalent(a: StepSignature, b: StepSignature) -> bool:
    """True when two signatures are behaviorally equivalent (phrasing excluded).

    Compares the event type, referenced fields, correlation key, exposed payload
    fields, and the body-level condition fingerprint -- so a change to the
    matching condition inside the step body is NOT silently absorbed.
    """
    return (
        a.event_type == b.event_type
        and a.referenced_fields == b.referenced_fields
        and a.correlation_key == b.correlation_key
        and a.payload_fields == b.payload_fields
        and a.condition_fingerprint == b.condition_fingerprint
    )


@dataclass(frozen=True)
class StepChange:
    step_id: str
    status: str
    old: Optional[CatalogEntry]
    new: Optional[CatalogEntry]


def classify_changes(
    old: list[CatalogEntry], new: list[CatalogEntry]
) -> list[StepChange]:
    """Classify every step_id across two catalog versions."""
    old_by = {e.step_id: e for e in old}
    new_by = {e.step_id: e for e in new}

    changes: list[StepChange] = []
    for step_id in sorted(set(old_by) | set(new_by)):
        o = old_by.get(step_id)
        n = new_by.get(step_id)
        if o is None:
            status = ADDED
        elif n is None:
            status = REMOVED
        elif not signatures_equivalent(o.signature, n.signature):
            status = CHANGED
        elif o.phrasing != n.phrasing:
            status = RENAMED
        else:
            status = UNCHANGED
        changes.append(StepChange(step_id, status, o, n))
    return changes


def describe_signature_change(old: StepSignature, new: StepSignature) -> str:
    """A human-readable contract diff between two signatures."""
    parts: list[str] = []
    if old.event_type != new.event_type:
        parts.append(f"event_type {old.event_type!r} -> {new.event_type!r}")
    if old.correlation_key != new.correlation_key:
        parts.append(f"correlation_key {tuple(old.correlation_key)} -> {tuple(new.correlation_key)}")
    if old.referenced_fields != new.referenced_fields:
        parts.append(
            f"referenced_fields {set(old.referenced_fields)} -> {set(new.referenced_fields)}"
        )
    if old.payload_fields != new.payload_fields:
        parts.append(f"payload_fields {old.payload_fields} -> {new.payload_fields}")
    if old.condition_fingerprint != new.condition_fingerprint:
        parts.append(
            "trigger condition changed (step body or binding parameters; the "
            "structural fingerprint is conservative -- a behavior-preserving "
            "refactor such as a temporary variable, reordered operands, or an "
            "extracted helper also trips it, so review the step body)"
        )
    return "; ".join(parts) if parts else "no behavioral change"
