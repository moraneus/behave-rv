"""Phase 5: garbage collection -- terminal events and quiescence TTL.

An instance is retired when it can no longer affect a verdict. Primary: an
explicit terminal event the agent exposed (definitive -- emits a final verdict).
Fallback: a quiescence TTL for entities with no declared terminal (silent memory
reclamation). Retiring an instance drops its witnessing trace.
"""

from behave_rv.compile.automaton import never, within
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event


def ev(type, t, order_id="A", **payload):
    return Event(type=type, event_time=t, bindings={"order_id": order_id},
                 payload=payload, source="test")


def is_cancelled(e):
    return e.payload.get("status") == "cancelled"


def no_cancel():
    return never("no-cancel", correlation_key="order_id",
                 event_types={"order.status"}, bad=is_cancelled)


def deliver_fast():
    return within("deliver-fast", correlation_key="order_id", seconds=30,
                  is_trigger=lambda e: e.type == "delivery.requested",
                  is_response=lambda e: e.type == "delivery.fulfilled",
                  event_types={"delivery.requested", "delivery.fulfilled"})


def run(policies, events, **gc):
    src = type("S", (), {"events": lambda self: iter(events)})()
    engine = Engine(policies, **gc)
    verdicts = engine.run(src)
    return engine, verdicts


# --- terminal events -------------------------------------------------------


def test_terminal_event_retires_the_instance():
    engine, _ = run([no_cancel()],
                    [ev("order.status", 1.0, status="placed"),
                     ev("order.delivered", 2.0)],
                    terminal_event_types={"order.delivered"})
    assert engine.live_instances == 0


def test_terminal_emits_final_satisfied_for_never_that_held():
    _, verdicts = run([no_cancel()],
                      [ev("order.status", 1.0, status="placed"),
                       ev("order.delivered", 2.0)],
                      terminal_event_types={"order.delivered"})
    (v,) = verdicts
    assert v.verdict == "satisfied"
    assert v.at == 2.0


def test_terminal_violates_a_pending_within():
    _, verdicts = run([deliver_fast()],
                      [ev("delivery.requested", 1.0),
                       ev("order.delivered", 5.0)],
                      terminal_event_types={"order.delivered"})
    (v,) = verdicts
    assert v.verdict == "violated"


def test_terminal_after_a_settled_verdict_emits_nothing_extra():
    engine, verdicts = run([no_cancel()],
                           [ev("order.status", 1.0, status="cancelled"),
                            ev("order.delivered", 2.0)],
                           terminal_event_types={"order.delivered"})
    assert [v.verdict for v in verdicts] == ["violated"]
    assert engine.live_instances == 0


# --- quiescence TTL --------------------------------------------------------


def test_quiescence_ttl_reclaims_a_quiet_instance_silently():
    engine, verdicts = run([no_cancel()],
                           [ev("order.status", 1.0, order_id="A", status="placed"),
                            ev("order.status", 200.0, order_id="B", status="placed")],
                           quiescence_ttl=100)
    assert verdicts == []
    assert engine.reclaimed == 1
    assert engine.live_instances == 1  # only B remains


def test_quiescence_does_not_reclaim_an_active_instance():
    engine, _ = run([no_cancel()],
                    [ev("order.status", 1.0, status="placed"),
                     ev("order.status", 50.0, status="shipped"),
                     ev("order.status", 90.0, status="packed")],
                    quiescence_ttl=100)
    assert engine.reclaimed == 0
    assert engine.live_instances == 1
