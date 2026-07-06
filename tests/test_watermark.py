"""The reordering window: release events in event-time order within a grace
window, and flag events that arrive after the watermark has already passed them.

This is what lets event time -- not arrival order -- drive sequencing for late
arrivals from distributed or telemetry sources.
"""

from behave_rv.events.event import Event
from behave_rv.events.watermark import ReorderBuffer


def ev(t):
    return Event("e", t, {"order_id": "A"}, {}, "test")


def _times(events):
    return [e.event_time for e in events]


def test_releases_buffered_events_in_event_time_order():
    buf = ReorderBuffer(grace=5)
    released = []
    for arrival in (ev(2.0), ev(1.0), ev(3.0)):  # out of order
        buf.push(arrival)
        released += buf.releasable()
    released += buf.flush()

    assert _times(released) == [1.0, 2.0, 3.0]


def test_holds_events_until_the_watermark_passes():
    buf = ReorderBuffer(grace=10)
    buf.push(ev(5.0))
    # watermark = max_seen(5) - grace(10) = -5, so nothing is safe yet
    assert buf.releasable() == []
    # only the flush at end-of-stream releases it
    assert _times(buf.flush()) == [5.0]


def test_releases_once_the_watermark_advances():
    buf = ReorderBuffer(grace=1)
    buf.push(ev(1.0))
    buf.push(ev(10.0))  # watermark = 9 -> 1.0 is now safe, 10.0 is not
    assert _times(buf.releasable()) == [1.0]


def test_non_finite_times_are_rejected_at_admission():
    # Interrogation D1: inf poisoned the watermark (everything after it was
    # late forever) and NaN entered the heap with undefined ordering. Both are
    # now rejected at push, recorded on `invalid`, distinct from `late`.
    buf = ReorderBuffer(grace=5)
    buf.push(ev(float("inf")))
    buf.push(ev(float("-inf")))
    buf.push(ev(float("nan")))
    assert len(buf.invalid) == 3
    assert buf.late == []
    assert buf._heap == []                       # nothing non-finite reaches the heap
    # the watermark is unpoisoned: normal events still flow
    buf.push(ev(1.0))
    buf.push(ev(2.0))
    assert _times(buf.flush()) == [1.0, 2.0]


def test_event_arriving_after_the_watermark_is_flagged_late():
    buf = ReorderBuffer(grace=1)
    buf.push(ev(1.0))
    buf.push(ev(10.0))
    buf.releasable()            # watermark advances to 9
    buf.push(ev(5.0))           # 5 < 9 -> too late

    assert _times(buf.late) == [5.0]
    # the late event is not injected into the ordered stream
    assert 5.0 not in _times(buf.flush())
