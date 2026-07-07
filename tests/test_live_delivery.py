"""Live verdict delivery: the sink and the subscription source.

The sink is a transport, not a semantic change: for any trace, the sequence of
verdicts delivered to a sink equals the batch list the same engine produces
without a sink, in the same order (pinned by a Hypothesis property). A sink
that raises is recorded and does not stop evaluation. The QueueSource stays
open: the engine blocks for the next event instead of exiting, and close()
ends the stream, flushing the reorder buffer so armed deadlines resolve.
"""

from __future__ import annotations

import threading
import time

import hypothesis.strategies as st
from hypothesis import given, settings

from behave_rv.compile.automaton import within
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource
from behave_rv.events.sources.subscription import QueueSource
from tests.test_properties import _build, _events, policies, triples


def _src(events):
    s = InProcessSource()
    for e in events:
        s.emit(e)
    return s


def _sig(v):
    return (v.policy_id, tuple(sorted(v.entity_key.items())), v.verdict, v.at)


def _oe(status, t, oid="A"):
    return Event("order.status", float(t), {"order_id": oid},
                 {"status": status}, "test")


# --- the sink contract -------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(triples, st.lists(policies, min_size=1, max_size=3))
def test_sink_sequence_equals_batch_list(tr, policy_dicts):
    # THE pinning property: sink delivery is a transport, not a semantic change.
    # Multiple policies over three keys make 3+ verdicts per run routine, so a
    # delivery fault conditioned on any verdict ordinal is visible (mutation M6
    # was missed when generated runs never exceeded 2 verdicts).
    events = _events(tr)
    built = [_build(p, name=f"p{i}") for i, p in enumerate(policy_dicts)]
    rebuilt = [_build(p, name=f"p{i}") for i, p in enumerate(policy_dicts)]
    batch = Engine(built).run(_src(events), emit_pending=True)

    delivered = []
    engine = Engine(rebuilt)
    returned = engine.run(_src(events), emit_pending=True, sink=delivered.append)

    assert [_sig(v) for v in delivered] == [_sig(v) for v in batch]
    assert returned == []                      # with a sink, run() does not accumulate
    assert engine.verdicts_delivered == len(batch)


def test_sink_object_with_emit_is_accepted():
    class Collector:
        def __init__(self):
            self.got = []

        def emit(self, v):
            self.got.append(v)

    sink = Collector()
    pol = _build({"operator": "never", "correlation_key": ("order_id",), "bad": "cancelled"})
    Engine([pol]).run(_src([_oe("cancelled", 1.0)]), sink=sink)
    assert [v.verdict for v in sink.got] == ["violated"]


def test_sink_exception_is_recorded_and_evaluation_continues():
    calls = []

    def bad_sink(v):
        calls.append(v)
        raise RuntimeError("alert channel down")

    pol = _build({"operator": "never", "correlation_key": ("order_id",), "bad": "cancelled"})
    events = [_oe("cancelled", 1.0, "A"), _oe("cancelled", 2.0, "B")]
    engine = Engine([pol])
    engine.run(_src(events), sink=bad_sink)     # must not raise

    assert len(calls) == 2                       # both verdicts still attempted
    assert engine.sink_errors == 2               # and the failures are recorded
    assert isinstance(engine.first_sink_error, RuntimeError)


# --- the subscription source --------------------------------------------------


def test_queue_source_blocks_until_events_arrive_and_close_ends():
    pol = _build({"operator": "never", "correlation_key": ("order_id",), "bad": "cancelled"})
    src = QueueSource()
    delivered = []
    engine = Engine([pol])

    t = threading.Thread(target=lambda: engine.run(src, emit_pending=True,
                                                   sink=delivered.append))
    t.start()
    time.sleep(0.2)
    assert t.is_alive()                          # quiet service: engine WAITS
    assert delivered == []

    src.push(_oe("placed", 1.0))
    src.push(_oe("cancelled", 2.0))
    src.close()
    t.join(timeout=5)
    assert not t.is_alive()
    assert [(v.entity_key["order_id"], v.verdict) for v in delivered] == \
        [("A", "violated")]


def test_close_flush_resolves_armed_within_when_horizon_passed():
    pol = within("deadline", correlation_key="order_id", seconds=30,
                 is_trigger=lambda e: e.payload.get("status") == "started",
                 is_response=lambda e: e.payload.get("status") == "completed",
                 event_types={"order.status"})
    src = QueueSource()
    src.push(_oe("started", 1.0))                # deadline 31.0
    src.push(_oe("tick", 40.0))                  # horizon passes the deadline
    src.close()                                   # buffered events flush here
    (v,) = Engine([pol]).run(src, emit_pending=True)
    assert v.verdict == "violated"
    assert v.at == 31.0


def test_close_leaves_within_pending_when_horizon_short():
    pol = within("deadline", correlation_key="order_id", seconds=30,
                 is_trigger=lambda e: e.payload.get("status") == "started",
                 is_response=lambda e: e.payload.get("status") == "completed",
                 event_types={"order.status"})
    src = QueueSource()
    src.push(_oe("started", 1.0))                # horizon 1.0 < deadline 31.0
    src.close()
    (v,) = Engine([pol]).run(src, emit_pending=True)
    assert v.verdict == "pending"
