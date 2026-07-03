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


def test_print_sink_renders_violations_and_compacts_the_rest():
    from behave_rv.catalog.registry import StepRegistry
    from behave_rv.compile.compiler import compile_feature
    from behave_rv.engine.loop import Engine
    from behave_rv.events.sources.inprocess import InProcessSource
    from behave_rv.verdict.sinks import PrintSink

    reg = StepRegistry()

    @reg.trigger('an order is "{status}"', step_id="s",
                 event_type="order.status", correlation_key="order_id")
    def f(ctx, e, status):
        return e.type == "order.status" and e.payload.get("status") == status

    policies = compile_feature(
        'Feature: f\n  Scenario: no cancel\n'
        '    Then an order is "cancelled" never happens\n', reg)
    stream = io.StringIO()
    src = InProcessSource()
    src.emit(Event("order.status", 1.0, {"order_id": "A"}, {"status": "placed"}, "t"))
    src.emit(Event("order.status", 2.0, {"order_id": "A"}, {"status": "cancelled"}, "t"))
    Engine(policies).run(src, emit_pending=True, sink=PrintSink(policies, stream=stream))

    out = stream.getvalue()
    assert "✗ Then" in out and "Deciding events" in out   # violation fully rendered
    assert out.count("VERDICT") == 1                       # only the violation is verbose


def test_json_file_sink_appends_lines(tmp_path):
    from behave_rv.verdict.sinks import JsonFileSink

    path = tmp_path / "verdicts.jsonl"
    sink = JsonFileSink(path)
    sink.emit(_verdict())
    sink.emit(_verdict())
    sink.close()

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["entity_key"] == {"order_id": "4471"}
