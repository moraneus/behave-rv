"""Epoch-scale wall fires and the clock-horizon marker.

Two failure modes at the wall-clock / event-time seam are pinned here. First,
the wall-fire release boundary must advance by one ulp (magnitude-relative),
never by an absolute epsilon: at Unix-epoch event times (~1.8e9, ulp ~2.4e-7)
an absolute epsilon underflows, the strict release comparison never clears,
wall deadlines fall silent, and the live loop busy-spins. Second, a deadline
that fired live on the wall clock in the silence after the last event has no
recorded event to advance replay time past it; the clock-horizon marker a
recorder appends on close reproduces the verdict on replay.
"""

from __future__ import annotations

import threading
import time

from behave_rv.compile.automaton import within
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.replay import (
    HORIZON_EVENT_TYPE,
    ReplaySource,
    TraceRecorder,
    record_events,
)
from behave_rv.events.sources.subscription import QueueSource
from behave_rv.events.watermark import ReorderBuffer

EPOCH = 1.753e9  # a realistic time.time() magnitude; float ulp here is ~2.4e-7


def _order_event(status: str, t: float, oid: str = "A") -> Event:
    return Event("order.status", float(t), {"order_id": oid},
                 {"status": status}, "test")


def _within_policy(seconds: float = 0.2):
    return within("deadline", correlation_key="order_id", seconds=seconds,
                  is_trigger=lambda e: e.payload.get("status") == "started",
                  is_response=lambda e: e.payload.get("status") == "completed",
                  event_types={"order.status"})


# --- the wall-fire boundary at epoch magnitudes ------------------------------


def test_wall_clock_fires_deadline_at_epoch_event_times():
    # identical to the small-timestamp wall-fire test, at time.time() scale:
    # before the ulp fix this hung forever (and busy-spun) because the
    # absolute epsilon vanished below one ulp of the event time
    pol = _within_policy(seconds=0.2)
    src = QueueSource()
    delivered = []
    engine = Engine([pol], grace=0.1)
    t = threading.Thread(target=lambda: engine.run(src, sink=delivered.append))
    t.start()
    src.push(_order_event("started", EPOCH))       # deadline EPOCH+0.2; silence

    deadline_wall = time.monotonic() + 5.0
    while not delivered and time.monotonic() < deadline_wall:
        time.sleep(0.02)
    src.close()
    t.join(timeout=5)

    assert delivered, "the deadline never fired at epoch-scale event times"
    assert delivered[0].verdict == "violated"
    assert delivered[0].at == EPOCH + 0.2          # the deadline's event time


def test_release_through_commits_the_exact_boundary_at_epoch_scale():
    # an event AT the committed moment is released, in one batch, at any float
    # magnitude; a later same-timestamp sibling is late (committed-plus-flagged)
    buffer = ReorderBuffer(grace=5.0)
    event = _order_event("started", EPOCH)
    buffer.push(event)
    released = buffer.release_through(EPOCH)
    assert released == [event]
    late_sibling = _order_event("started", EPOCH, oid="B")
    buffer.push(late_sibling)
    assert buffer.late == [late_sibling]


def test_releasable_never_regresses_a_committed_watermark():
    # after a wall-fire commit, an ordinary release recomputation (max_seen -
    # grace, with its float rounding) must not pull the watermark back and
    # readmit what was already declared late
    buffer = ReorderBuffer(grace=5.0)
    buffer.push(_order_event("started", EPOCH))
    buffer.advance_clock(EPOCH + 5.0)              # max_seen = boundary + grace
    buffer.release_through(EPOCH)
    committed = buffer._watermark
    buffer.releasable()
    assert buffer._watermark >= committed


# --- the clock-horizon marker ------------------------------------------------


def test_recorder_horizon_reproduces_wall_fired_verdict_on_replay(tmp_path):
    # live: trigger, then silence, deadline fires on the wall clock. The
    # recorded trace ends at the trigger -- without the horizon the violation
    # replays as pending; with it, replay reproduces the verdict
    path = tmp_path / "trace.jsonl"
    stopped_at = EPOCH + 30.0
    recorder = TraceRecorder(path, clock=lambda: stopped_at)
    recorder(_order_event("started", EPOCH))
    recorder.close()

    verdicts = Engine([_within_policy(0.2)]).run(
        ReplaySource(path), emit_pending=True)
    assert [v.verdict for v in verdicts] == ["violated"]
    assert verdicts[0].at == EPOCH + 0.2


def test_recorder_without_clock_pins_horizon_at_last_event(tmp_path):
    # no clock: the horizon is the highest event time seen, so an armed
    # deadline stays honestly pending -- the recorder must never invent time
    path = tmp_path / "trace.jsonl"
    recorder = TraceRecorder(path)
    recorder(_order_event("started", EPOCH))
    recorder.close()

    verdicts = Engine([_within_policy(0.2)]).run(
        ReplaySource(path), emit_pending=True)
    assert [v.verdict for v in verdicts] == ["pending"]


def test_record_events_horizon_parameter(tmp_path):
    path = tmp_path / "trace.jsonl"
    record_events(path, [_order_event("started", EPOCH)], horizon=EPOCH + 30.0)
    verdicts = Engine([_within_policy(0.2)]).run(
        ReplaySource(path), emit_pending=True)
    assert [v.verdict for v in verdicts] == ["violated"]


def test_horizon_marker_is_inert_for_policies(tmp_path):
    # the marker carries no bindings and a reserved type: it creates no
    # monitor instance and decides nothing on its own
    path = tmp_path / "trace.jsonl"
    record_events(path, [], horizon=EPOCH)
    engine = Engine([_within_policy(0.2)])
    verdicts = engine.run(ReplaySource(path), emit_pending=True)
    assert verdicts == []
    assert engine.live_instances == 0
    assert HORIZON_EVENT_TYPE in engine.observed_types


def test_recorder_close_is_idempotent_and_context_managed(tmp_path):
    path = tmp_path / "trace.jsonl"
    with TraceRecorder(path, clock=lambda: EPOCH + 1.0) as recorder:
        recorder(_order_event("started", EPOCH))
    recorder.close()                               # second close: no error
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2                         # one event + one horizon
