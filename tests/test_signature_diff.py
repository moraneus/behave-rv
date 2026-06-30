"""Phase 6: signature equivalence and rename-vs-break classification.

Equivalence is the behavioral signature -- event type, referenced fields,
correlation key, payload fields. A phrasing-only rename keeps the signature
equivalent and is absorbed silently. A change to a behavioral field is surfaced.
The bias is toward surfacing: when in doubt, treat a change as a break.
"""

from behave_rv.catalog.diff import classify_changes, signatures_equivalent
from behave_rv.catalog.entry import CatalogEntry, StepSignature


def sig(*, event_type="order.status", referenced=("status",), key=("order_id",),
        payload=None, trigger_condition="cond"):
    return StepSignature(
        event_type=event_type,
        trigger_condition=trigger_condition,
        payload_fields=payload or {},
        referenced_fields=set(referenced),
        correlation_key=tuple(key),
    )


def entry(step_id, *, phrasing="an order is \"{status}\"", signature=None):
    return CatalogEntry(step_id=step_id, phrasing=phrasing, kind="trigger",
                        signature=signature or sig(), provenance="llm",
                        observed=False, version=1)


# --- equivalence -----------------------------------------------------------


def test_equivalence_ignores_phrasing_level_trigger_condition():
    assert signatures_equivalent(sig(trigger_condition="a"), sig(trigger_condition="b"))


def test_equivalence_detects_behavioral_changes():
    assert not signatures_equivalent(sig(), sig(event_type="order.state"))
    assert not signatures_equivalent(sig(), sig(referenced=("state",)))
    assert not signatures_equivalent(sig(), sig(key=("order_id", "tenant_id")))


# --- classification --------------------------------------------------------


def _status(changes, step_id):
    return next(c.status for c in changes if c.step_id == step_id)


def test_phrasing_rename_with_equal_signature_is_renamed_not_changed():
    old = [entry("s1", phrasing='an order is "{status}"')]
    new = [entry("s1", phrasing='the order reaches "{status}"')]
    assert _status(classify_changes(old, new), "s1") == "renamed"


def test_identical_entry_is_unchanged():
    assert _status(classify_changes([entry("s1")], [entry("s1")]), "s1") == "unchanged"


def test_behavioral_change_is_changed():
    old = [entry("s1", signature=sig())]
    new = [entry("s1", signature=sig(event_type="order.state"))]
    assert _status(classify_changes(old, new), "s1") == "changed"


def test_added_and_removed_steps_are_classified():
    changes = classify_changes([entry("gone")], [entry("fresh")])
    assert _status(changes, "gone") == "removed"
    assert _status(changes, "fresh") == "added"
