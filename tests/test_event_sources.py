"""Phase 1: prove events can be pushed through a source and read back.

Covers the in-process emitter, the replay source, Event serialization, and a
full round-trip: emit -> record to file -> replay -> identical events.
"""

from behave_rv.events.event import Event
from behave_rv.events.sources import EventSource
from behave_rv.events.sources.inprocess import InProcessSource
from behave_rv.events.sources.replay import ReplaySource, record_events


def _event(order_id="4471", status="cancelled", t=1.0):
    return Event(
        type="order.status",
        event_time=t,
        bindings={"order_id": order_id},
        payload={"status": status},
        source="inprocess",
    )


# --- Event serialization ---------------------------------------------------


def test_event_dict_round_trip_preserves_all_fields():
    e = _event()
    assert Event.from_dict(e.to_dict()) == e


# --- In-process emitter ----------------------------------------------------


def test_inprocess_source_is_an_event_source():
    assert isinstance(InProcessSource(), EventSource)


def test_inprocess_yields_emitted_events_in_order():
    src = InProcessSource()
    src.emit(_event(status="placed", t=1.0))
    src.emit(_event(status="cancelled", t=2.0))

    out = list(src.events())

    assert [e.payload["status"] for e in out] == ["placed", "cancelled"]


def test_inprocess_emit_convenience_builds_an_event():
    src = InProcessSource()
    src.emit_event(
        type="order.status",
        event_time=1.0,
        bindings={"order_id": "4471"},
        payload={"status": "placed"},
    )

    (e,) = list(src.events())

    assert e.type == "order.status"
    assert e.bindings == {"order_id": "4471"}
    assert e.source == "inprocess"


def test_inprocess_events_drains_the_queue():
    src = InProcessSource()
    src.emit(_event())

    list(src.events())

    assert list(src.events()) == []


# --- Replay source ---------------------------------------------------------


def test_replay_source_is_an_event_source(tmp_path):
    path = tmp_path / "trace.jsonl"
    record_events(path, [_event()])
    assert isinstance(ReplaySource(path), EventSource)


def test_replay_reads_back_recorded_events(tmp_path):
    path = tmp_path / "trace.jsonl"
    events = [_event(status="placed", t=1.0), _event(status="cancelled", t=2.0)]
    record_events(path, events)

    assert list(ReplaySource(path).events()) == events


# --- Full round-trip through both sources ----------------------------------


def test_emit_record_replay_round_trip(tmp_path):
    live = InProcessSource()
    live.emit(_event(status="placed", t=1.0))
    live.emit(_event(status="shipped", t=2.0))
    live.emit(_event(status="cancelled", t=3.0))

    path = tmp_path / "trace.jsonl"
    record_events(path, live.events())

    assert list(ReplaySource(path).events()) == [
        _event(status="placed", t=1.0),
        _event(status="shipped", t=2.0),
        _event(status="cancelled", t=3.0),
    ]


def test_trace_recorder_tees_a_live_stream_into_a_replayable_file(tmp_path):
    """Where trace files come from: the recorder passes events through
    unchanged while appending them in the exact format ReplaySource reads
    (and record_events writes), closing with a clock-horizon marker at the
    moment recording stopped."""
    from behave_rv.events.event import Event
    from behave_rv.events.sources.replay import (HORIZON_EVENT_TYPE,
                                                 ReplaySource, TraceRecorder,
                                                 record_events)

    events = [Event("t", 1.0, {"k": "A"}, {"status": "x"}, "app"),
              Event("t", 2.0, {"k": "A"}, {"status": "y"}, "app")]

    recorded = tmp_path / "live.jsonl"
    recorder = TraceRecorder(recorded)
    passed_through = [recorder(e) for e in events]
    recorder.close()
    assert passed_through == events                      # a true tee

    replayed = list(ReplaySource(recorded).events())
    assert replayed[:2] == events
    assert replayed[2].type == HORIZON_EVENT_TYPE        # the stop marker
    assert replayed[2].event_time == 2.0                 # no clock: last event

    reference = tmp_path / "batch.jsonl"
    record_events(reference, events, horizon=2.0)
    assert recorded.read_bytes() == reference.read_bytes()   # identical format
