"""Body-level trigger-condition fingerprinting and payload-field extraction.

The fingerprint is a sound over-approximation: it is invariant to identifier
renames and formatting (a pure rename stays silent) but reflects the structure
and constants of the matching condition (changing the condition surfaces). When
the source cannot be read, the fingerprint is empty and the diff biases toward
surfacing.
"""

from behave_rv.catalog.condition import condition_fingerprint, payload_fields


def base(ctx, event, status):
    return event.type == "order.status" and event.payload.get("status") == status


def renamed(ctx, ev, status):  # function + parameter renamed, condition identical
    return ev.type == "order.status" and ev.payload.get("status") == status


def guarded(ctx, event, status):  # same, plus an in-body guard
    return (event.type == "order.status" and event.payload.get("status") == status
            and event.payload.get("amount", 0) > 0)


def different_constant(ctx, event, status):  # reads a different payload key
    return event.type == "order.status" and event.payload.get("state") == status


def test_fingerprint_is_invariant_to_pure_renames():
    assert condition_fingerprint(base) == condition_fingerprint(renamed)


def test_fingerprint_changes_when_an_in_body_guard_is_added():
    assert condition_fingerprint(base) != condition_fingerprint(guarded)


def test_fingerprint_changes_when_a_condition_constant_changes():
    assert condition_fingerprint(base) != condition_fingerprint(different_constant)


def test_payload_fields_extracted_from_body():
    assert payload_fields(base) == {"status": "any"}
    assert payload_fields(guarded) == {"status": "any", "amount": "any"}


def test_fingerprint_empty_when_source_unavailable():
    # eval of a hardcoded literal (safe): produces a function inspect.getsource
    # cannot read, to exercise the source-unavailable fallback.
    f = eval("lambda ctx, event, status: True")
    assert condition_fingerprint(f) == ""
