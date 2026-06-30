"""Phase 4: verdict serialization and the JSON sink (no model in this path)."""

import io
import json

from behave_rv.events.event import Event
from behave_rv.verdict.record import Verdict
from behave_rv.verdict.sinks import JsonSink


def _verdict():
    trigger = Event("order.status", 2.0, {"order_id": "4471"}, {"status": "cancelled"}, "test")
    return Verdict(
        policy_id="no-cancel",
        entity_key={"order_id": "4471"},
        verdict="violated",
        trigger_event=trigger,
        witnessing_trace=[trigger],
        at=2.0,
    )


def test_verdict_to_dict_serializes_nested_events():
    d = _verdict().to_dict()
    assert d["policy_id"] == "no-cancel"
    assert d["entity_key"] == {"order_id": "4471"}
    assert d["verdict"] == "violated"
    assert d["trigger_event"]["payload"] == {"status": "cancelled"}
    assert d["witnessing_trace"][0]["type"] == "order.status"
    assert d["at"] == 2.0


def test_verdict_to_dict_handles_missing_trigger():
    v = Verdict("p", {"order_id": "1"}, "pending", None, [], 0.0)
    assert v.to_dict()["trigger_event"] is None


def test_json_sink_writes_one_json_object_per_verdict():
    stream = io.StringIO()
    sink = JsonSink(stream)

    sink.emit(_verdict())
    sink.emit(_verdict())

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["verdict"] == "violated"
