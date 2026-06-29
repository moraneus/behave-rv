"""The brief's first end-to-end target: one entity, one correlation key, the
`never` and `within` operators, replay mode, and verdicts.

Records a trace to a file and replays it through the identical engine pipeline,
proving the Phase 1 replay source and the Phase 3 engine compose.
"""

from behave_rv.compile.automaton import never, within
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.replay import ReplaySource, record_events


def ev(type, t, order_id, **payload):
    return Event(type=type, event_time=t, bindings={"order_id": order_id},
                 payload=payload, source="recorded")


def test_replay_drives_never_and_within_verdicts(tmp_path):
    trace = [
        ev("delivery.requested", 1.0, "A"),
        ev("delivery.fulfilled", 10.0, "A"),       # A responds in time -> satisfied
        ev("delivery.requested", 2.0, "B"),         # B never responds
        ev("order.status", 5.0, "B", status="cancelled"),  # B cancelled -> never violated
        ev("clock.tick", 40.0, "B"),                # advances event time past B's 32.0 deadline
    ]
    path = tmp_path / "trace.jsonl"
    record_events(path, trace)

    policies = [
        never("no-cancel", correlation_key="order_id",
              event_types={"order.status"},
              bad=lambda e: e.payload.get("status") == "cancelled"),
        within("deliver-fast", correlation_key="order_id", seconds=30,
               is_trigger=lambda e: e.type == "delivery.requested",
               is_response=lambda e: e.type == "delivery.fulfilled",
               event_types={"delivery.requested", "delivery.fulfilled"}),
    ]

    verdicts = Engine(policies).run(ReplaySource(path))
    summary = {(v.policy_id, v.entity_key["order_id"]): v.verdict for v in verdicts}

    assert summary == {
        ("deliver-fast", "A"): "satisfied",
        ("no-cancel", "B"): "violated",
        ("deliver-fast", "B"): "violated",
    }
