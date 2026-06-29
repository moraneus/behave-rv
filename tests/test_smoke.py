"""Smoke tests: the scaffolding imports and the core dataclasses construct."""

from behave_rv import __version__
from behave_rv.catalog.entry import CatalogEntry, StepSignature
from behave_rv.events.event import Event
from behave_rv.verdict.record import Verdict


def test_version():
    assert __version__ == "0.0.1"


def test_event_constructs():
    e = Event(
        type="order.status",
        event_time=1.0,
        bindings={"order_id": "4471"},
        payload={"status": "cancelled"},
        source="inprocess",
    )
    assert e.bindings["order_id"] == "4471"


def test_catalog_and_verdict_construct():
    sig = StepSignature(
        event_type="order.status",
        trigger_condition="status == cancelled",
        payload_fields={"status": "str"},
        referenced_fields={"status"},
        correlation_key=("order_id",),
    )
    entry = CatalogEntry(
        step_id="s1",
        phrasing='an order is "{status}"',
        kind="trigger",
        signature=sig,
        provenance="human",
        observed=False,
        version=1,
    )
    assert entry.signature.correlation_key == ("order_id",)

    v = Verdict(
        policy_id="p1",
        entity_key={"order_id": "4471"},
        verdict="pending",
        trigger_event=Event("order.status", 1.0, {"order_id": "4471"}, {}, "inprocess"),
        witnessing_trace=[],
        at=1.0,
    )
    assert v.verdict == "pending"
