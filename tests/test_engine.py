"""Phase 3: temporal operators + the engine producing verdicts over a replayed trace.

Two operators give the complete verdict story: ``never`` (a safety property
violated by an arriving event) and ``within`` (a bounded-response property
violated by a deadline the timer wheel fires on event time). Verdicts are
three-valued; a property that has not yet resolved on a finite prefix simply
emits nothing (pending).
"""

from behave_rv.compile.automaton import NeverMonitor, WithinMonitor, never, within
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource


def ev(type, t, order_id="A", **payload):
    return Event(type=type, event_time=t, bindings={"order_id": order_id},
                 payload=payload, source="test")


def is_cancelled(e):
    return e.type == "order.status" and e.payload.get("status") == "cancelled"


def is_requested(e):
    return e.type == "delivery.requested"


def is_fulfilled(e):
    return e.type == "delivery.fulfilled"


# --- operator automata (pure state machines) -------------------------------


def test_never_monitor_violates_on_the_bad_event():
    m = NeverMonitor(is_cancelled)
    assert m.on_event(ev("order.status", 1.0, status="placed")) is None
    assert m.on_event(ev("order.status", 2.0, status="cancelled")) == "violated"
    assert m.settled is True


def test_never_monitor_stays_silent_without_the_bad_event():
    m = NeverMonitor(is_cancelled)
    assert m.on_event(ev("order.status", 1.0, status="shipped")) is None
    assert m.next_deadline() is None


def test_within_monitor_satisfied_when_response_is_in_time():
    m = WithinMonitor(is_requested, is_fulfilled, seconds=30)
    assert m.on_event(ev("delivery.requested", 1.0)) is None
    assert m.next_deadline() == 31.0
    assert m.on_event(ev("delivery.fulfilled", 20.0)) == "satisfied"


def test_within_monitor_violates_on_timeout():
    m = WithinMonitor(is_requested, is_fulfilled, seconds=30)
    m.on_event(ev("delivery.requested", 1.0))
    assert m.on_timeout(31.0) == "violated"
    assert m.settled is True


def test_within_monitor_violates_on_a_late_response():
    m = WithinMonitor(is_requested, is_fulfilled, seconds=30)
    m.on_event(ev("delivery.requested", 1.0))
    assert m.on_event(ev("delivery.fulfilled", 40.0)) == "violated"


# --- engine over a replayed trace ------------------------------------------


def test_engine_emits_a_never_violation_for_the_entity():
    policy = never("no-cancel", correlation_key="order_id",
                   event_types={"order.status"}, bad=is_cancelled)
    src = InProcessSource()
    src.emit(ev("order.status", 1.0, status="placed"))
    src.emit(ev("order.status", 2.0, status="cancelled"))

    (v,) = Engine([policy]).run(src)

    assert v.policy_id == "no-cancel"
    assert v.verdict == "violated"
    assert v.entity_key == {"order_id": "A"}
    assert v.at == 2.0


def test_engine_isolates_entities_by_correlation_key():
    policy = never("no-cancel", correlation_key="order_id",
                   event_types={"order.status"}, bad=is_cancelled)
    src = InProcessSource()
    src.emit(ev("order.status", 1.0, order_id="A", status="cancelled"))
    src.emit(ev("order.status", 2.0, order_id="B", status="placed"))

    verdicts = Engine([policy]).run(src)

    assert [v.entity_key for v in verdicts] == [{"order_id": "A"}]


def test_engine_within_timeout_fires_on_event_time():
    policy = within("deliver-fast", correlation_key="order_id", seconds=30,
                    is_trigger=is_requested, is_response=is_fulfilled,
                    event_types={"delivery.requested", "delivery.fulfilled"})
    src = InProcessSource()
    src.emit(ev("delivery.requested", 1.0))
    src.emit(ev("order.touch", 35.0))  # advances event-time past the deadline

    (v,) = Engine([policy]).run(src)

    assert v.verdict == "violated"
    assert v.at == 31.0


def test_engine_within_satisfied_when_response_arrives():
    policy = within("deliver-fast", correlation_key="order_id", seconds=30,
                    is_trigger=is_requested, is_response=is_fulfilled,
                    event_types={"delivery.requested", "delivery.fulfilled"})
    src = InProcessSource()
    src.emit(ev("delivery.requested", 1.0))
    src.emit(ev("delivery.fulfilled", 10.0))

    (v,) = Engine([policy]).run(src)

    assert v.verdict == "satisfied"


def test_engine_stays_pending_when_stream_ends_before_deadline():
    policy = within("deliver-fast", correlation_key="order_id", seconds=30,
                    is_trigger=is_requested, is_response=is_fulfilled,
                    event_types={"delivery.requested", "delivery.fulfilled"})
    src = InProcessSource()
    src.emit(ev("delivery.requested", 1.0))
    src.emit(ev("order.touch", 5.0))  # before the 31.0 deadline

    assert Engine([policy]).run(src) == []


def test_emit_pending_surfaces_open_instances_at_stream_end():
    policy = within("deliver-fast", correlation_key="order_id", seconds=30,
                    is_trigger=is_requested, is_response=is_fulfilled,
                    event_types={"delivery.requested", "delivery.fulfilled"})
    src = InProcessSource()
    src.emit(ev("delivery.requested", 1.0))

    (v,) = Engine([policy]).run(src, emit_pending=True)
    assert v.verdict == "pending"
    assert v.entity_key == {"order_id": "A"}
