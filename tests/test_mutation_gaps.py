"""Tests added by the mutation-testing triage (see MUTATION.md).

Each test kills one or more surviving mutants from the mutmut campaign; the
mutant ids appear in the comments. These are real suite gaps the campaign
exposed: behavior that was reachable and observable but never asserted.
"""

from __future__ import annotations

import threading
import time

import pytest

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.automaton import (
    NeverMonitor,
    WithinMonitor,
    before as make_before,
    never as make_never,
    report_predicate_error,
    set_predicate_error_collector,
    within as make_within,
)
from behave_rv.compile.compiler import (
    CompileError,
    UncheckablePolicyWarning,
    compile_feature,
)
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource
from behave_rv.events.sources.subscription import QueueSource


def ev(t, status, key="E1", etype="order.status", **payload):
    return Event(etype, float(t), {"order_id": key}, {"status": status, **payload}, "test")


def run_batch(policies, events, *, grace=5.0, terminal=(), emit_pending=False, sink=None):
    src = InProcessSource()
    for e in events:
        src.emit(e)
    engine = Engine(policies, terminal_event_types=terminal, grace=grace)
    verdicts = engine.run(src, emit_pending=emit_pending, sink=sink)
    return engine, verdicts


def basic_registry():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if event.type == "order.status" and event.payload.get("status") == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def compile_one(text):
    return compile_feature(text, basic_registry())


BEFORE_POLICY = """
Feature: f
  Scenario: paid after authorized
    When an order is "paid"
    Then an order is "authorized" before
"""

WITHIN_POLICY = """
Feature: f
  Scenario: refunded in window
    When an order is "cancelled"
    Then an order is "refunded" within "5" seconds
"""


# -- dispatch and retirement --------------------------------------------------


def test_dispatch_continues_past_a_policy_missing_its_key():
    # kills engine.loop _handle_event_32 (continue -> break in the candidates
    # loop): a policy whose correlation key is absent from the event must not
    # stop dispatch to the policies whose key IS present.
    keyless = make_never("needs-other-key", correlation_key="other_id",
                         event_types={"order.status"},
                         bad=lambda e: e.payload.get("status") == "bad")
    keyed = make_never("needs-order-key", correlation_key="order_id",
                       event_types={"order.status"},
                       bad=lambda e: e.payload.get("status") == "bad")
    _, verdicts = run_batch([keyless, keyed], [ev(1.0, "bad")], grace=0)
    assert [(v.policy_id, v.verdict) for v in verdicts] == [("needs-order-key", "violated")]


def test_terminal_settles_every_policy_for_the_entity():
    # kills engine.loop _retire_entity_7 and _retire_entity_13 (continue ->
    # break): a keyless policy and a never-instantiated policy come first in
    # registration order; the instantiated policy behind them must still settle.
    keyless = make_never("keyless", correlation_key="other_id",
                         event_types={"order.status"}, bad=lambda e: False)
    uninstantiated = make_never("uninstantiated", correlation_key="order_id",
                                event_types={"order.absent"}, bad=lambda e: False)
    live = make_never("live", correlation_key="order_id",
                      event_types={"order.status"}, bad=lambda e: False)
    events = [ev(1.0, "created"),
              Event("order.done", 2.0, {"order_id": "E1"}, {}, "test")]
    _, verdicts = run_batch([keyless, uninstantiated, live], events,
                            grace=0, terminal={"order.done"})
    assert {(v.policy_id, v.verdict) for v in verdicts} == {("live", "satisfied")}


def test_verdicts_carry_the_trigger_event():
    # kills engine.loop _verdict_10, _handle_event_60, run_89,
    # _fire_due_deadlines_12, _retire_entity_20 (trigger_event=None variants):
    # Verdict.trigger_event is part of the record, on every settlement path.
    policies = compile_one(BEFORE_POLICY + """
  Scenario: refunded in window
    When an order is "cancelled"
    Then an order is "refunded" within "5" seconds
""")
    # decided-by-event path
    _, verdicts = run_batch(policies, [ev(1.0, "paid")], grace=0)
    decided = [v for v in verdicts if v.policy_id == "paid after authorized"]
    assert decided[0].trigger_event is not None
    assert decided[0].trigger_event.payload["status"] == "paid"

    # timer path: the deadline verdict carries the arming event
    _, verdicts = run_batch(policies, [ev(1.0, "cancelled"), ev(100.0, "noise")], grace=0)
    timed = [v for v in verdicts if v.verdict == "violated"
             and v.policy_id == "refunded in window"]
    assert timed and timed[0].trigger_event.payload["status"] == "cancelled"

    # terminal path: an armed, unsettled within carries its arming event
    events = [ev(1.0, "cancelled"),
              Event("order.done", 2.0, {"order_id": "E1"}, {}, "test")]
    _, verdicts = run_batch(policies, events, grace=0, terminal={"order.done"})
    at_terminal = [v for v in verdicts if v.policy_id == "refunded in window"]
    assert at_terminal and at_terminal[0].trigger_event.payload["status"] == "cancelled"

    # emit_pending path: same, with no terminal configured
    _, verdicts = run_batch(policies, [ev(1.0, "cancelled")], grace=0, emit_pending=True)
    pending = [v for v in verdicts if v.policy_id == "refunded in window"]
    assert pending and pending[0].verdict == "pending"
    assert pending[0].trigger_event.payload["status"] == "cancelled"


def test_stale_deadline_timers_do_not_stop_later_due_deadlines():
    # kills engine.loop _fire_due_deadlines_5 (continue -> break): a stale
    # timer whose instance was retired sits ahead of a genuinely due deadline.
    policies = compile_one(WITHIN_POLICY)
    events = [ev(1.0, "cancelled", key="A"),                       # deadline 6.0
              Event("order.done", 2.0, {"order_id": "A"}, {}, "test"),  # retires A
              ev(3.0, "cancelled", key="B"),                       # deadline 8.0
              ev(50.0, "noise", key="C")]                          # fires both timers
    _, verdicts = run_batch(policies, events, grace=0, terminal={"order.done"})
    b = [v for v in verdicts if v.entity_key["order_id"] == "B"]
    assert b and b[0].verdict == "violated"


# -- quiescence TTL -----------------------------------------------------------


def test_ttl_validation_keeps_refreshed_instances():
    # kills engine.loop _reclaim_quiescent_7 (now - last -> now + last): a
    # stale timer fires but the instance was refreshed; it must be kept, and
    # the before-policy's memory with it.
    policies = compile_one(BEFORE_POLICY)
    src = InProcessSource()
    for e in [ev(0.0, "authorized"), ev(5.0, "refresh"), ev(11.0, "paid")]:
        src.emit(e)
    engine = Engine(policies, quiescence_ttl=10.0, grace=0)
    verdicts = engine.run(src)
    assert [v.verdict for v in verdicts] == ["satisfied"]
    assert engine.reclaimed == 0


def test_ttl_reclaims_exactly_at_the_boundary_and_counts():
    # kills engine.loop _reclaim_quiescent_8 (>= -> >), _9 (+= -> =),
    # _12 (append(None)), and _6 (continue -> break, via A's stale entry
    # sitting ahead of B's and D's live ones): entities quiet for exactly the
    # TTL are all reclaimed and recorded.
    policies = compile_one(BEFORE_POLICY)
    src = InProcessSource()
    for e in [ev(0.0, "authorized", key="A"), ev(0.0, "authorized", key="B"),
              ev(0.0, "authorized", key="D"),
              Event("order.done", 5.0, {"order_id": "A"}, {}, "test"),  # stale ttl entry for A
              ev(10.0, "noise", key="C")]:                              # B, D timers due
        src.emit(e)
    engine = Engine(policies, quiescence_ttl=10.0, grace=0,
                    terminal_event_types={"order.done"})
    engine.run(src)
    assert engine.reclaimed == 2
    assert ("B",) in engine.reclaimed_keys and ("D",) in engine.reclaimed_keys


def test_ttl_reclaim_forgets_the_instance_memory():
    # companion to the validation test: an entity genuinely quiet past the TTL
    # is reclaimed, so the before-policy's memory is honestly gone
    policies = compile_one(BEFORE_POLICY)
    src = InProcessSource()
    for e in [ev(0.0, "authorized"), ev(20.0, "paid")]:
        src.emit(e)
    engine = Engine(policies, quiescence_ttl=10.0, grace=0)
    verdicts = engine.run(src)
    assert [v.verdict for v in verdicts] == ["violated"]     # memory reclaimed
    assert engine.reclaimed == 1


# -- error logs and collector restoration --------------------------------------


def test_engine_run_restores_the_predicate_error_collector():
    # kills engine.loop run_98 and automaton _set_predicate_error_collector_1:
    # after run(), the previously installed collector must be back in place.
    mine: list = []
    previous = set_predicate_error_collector(mine)
    try:
        policies = compile_one(BEFORE_POLICY)
        run_batch(policies, [ev(1.0, "paid")], grace=0)
        assert report_predicate_error(ValueError("x")) is True
        assert len(mine) == 1
    finally:
        set_predicate_error_collector(previous)


def test_report_predicate_error_without_collector_reports_uncollected():
    # kills automaton _report_predicate_error_2 (return False -> True): with
    # no collector installed, the caller must be told to raise.
    previous = set_predicate_error_collector(None)
    try:
        assert report_predicate_error(ValueError("x")) is False
    finally:
        set_predicate_error_collector(previous)


def test_sink_error_log_records_the_policy_source():
    # kills engine.loop run_39: a failing sink is logged with WHICH policy's
    # verdict it dropped.
    policies = compile_one(BEFORE_POLICY)

    def bad_sink(verdict):
        raise RuntimeError("sink down")

    engine, _ = run_batch(policies, [ev(1.0, "paid")], grace=0, sink=bad_sink)
    assert engine.sink_errors == 1
    assert engine.sink_error_sources == ["paid after authorized"]


def test_monitor_internal_error_is_logged_with_policy_and_original():
    # kills engine.loop _verdict_2/_verdict_3, automaton PredicateError_1/_3:
    # a deciding_events() raise keeps the verdict, logs the policy id, and the
    # PredicateError chain keeps the original exception and a real message.
    class BrokenDeciding(NeverMonitor):
        def deciding_events(self):
            raise RuntimeError("broken evidence")

    from behave_rv.compile.automaton import Policy
    policy = Policy(policy_id="broken", correlation_key=("order_id",),
                    event_types=frozenset({"order.status"}),
                    monitor_factory=lambda: BrokenDeciding(
                        lambda e: e.payload.get("status") == "bad"))
    engine, verdicts = run_batch([policy], [ev(1.0, "bad")], grace=0)
    assert [v.verdict for v in verdicts] == ["violated"]
    assert verdicts[0].deciding_events == []
    assert engine.predicate_errors == 1
    (source,) = engine.predicate_error_sources
    assert source[0] == "broken"

    # the PredicateError contract itself
    from behave_rv.compile.automaton import PredicateError
    original = ValueError("boom")
    err = PredicateError("step.x", original)
    assert err.original is original
    assert "step.x" in str(err)


def test_verdict_deciding_raise_records_the_original_exception():
    # kills engine.loop _verdict_3 (exc -> None): first_predicate_error must
    # be the actual exception, not a placeholder.
    class BrokenDeciding(NeverMonitor):
        def deciding_events(self):
            raise RuntimeError("broken evidence")

    from behave_rv.compile.automaton import Policy
    policy = Policy(policy_id="broken", correlation_key=("order_id",),
                    event_types=frozenset({"order.status"}),
                    monitor_factory=lambda: BrokenDeciding(
                        lambda e: e.payload.get("status") == "bad"))
    engine, _ = run_batch([policy], [ev(1.0, "bad")], grace=0)
    assert isinstance(engine.first_predicate_error, RuntimeError)


def test_on_event_raise_is_logged_with_policy_and_exception():
    # kills engine.loop _handle_event_47/_handle_event_48: the atomic-restore
    # path (a raise propagating through on_event) records WHICH policy and
    # WHICH exception.
    class BrokenOnEvent(NeverMonitor):
        def on_event(self, event):
            raise RuntimeError("monitor bug")

    from behave_rv.compile.automaton import Policy
    policy = Policy(policy_id="explodes", correlation_key=("order_id",),
                    event_types=frozenset({"order.status"}),
                    monitor_factory=lambda: BrokenOnEvent(lambda e: False))
    engine, verdicts = run_batch([policy], [ev(1.0, "x")], grace=0)
    assert verdicts == []
    assert engine.predicate_error_sources == [("explodes", None)]
    assert isinstance(engine.first_predicate_error, RuntimeError)


def test_compiled_step_raise_keeps_the_original_exception():
    # kills compiler __predicate_10 (PredicateError(step_id, None)): the
    # containment wrapper must chain the step author's actual exception.
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="raising.step",
                      event_type="order.status", correlation_key="order_id")
    def raising(ctx, event, status):
        raise ValueError("author bug")

    policies = compile_feature("""
Feature: f
  Scenario: s
    Then an order is "bad" never happens
""", registry)
    engine, _ = run_batch(policies, [ev(1.0, "bad")], grace=0)
    assert engine.predicate_errors == 1
    err = engine.first_predicate_error
    assert isinstance(err.original, ValueError)
    assert str(err.original) == "author bug"


def test_type_liveness_warning_does_not_hide_later_value_warnings():
    # kills compiler _warn_if_uncheckable_17/_warn_if_uncheckable_19
    # (continue -> break): a step failing the TYPE check must not stop the
    # value check on the policy's remaining steps.
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        return event.type == "order.status" and event.payload.get("status") == status

    @registry.trigger('a shipment is "{status}"', step_id="ship.status.is",
                      event_type="ship.status", correlation_key="order_id")
    def ship_is(ctx, event, status):
        return event.type == "ship.status" and event.payload.get("status") == status

    feature = """
Feature: f
  Scenario: s
    When a shipment is "sent"
    Then an order is "authorized" before
"""
    with pytest.warns(UncheckablePolicyWarning) as caught:
        compile_feature(feature, registry,
                        observed_event_types={"order.status"},     # ship.status missing
                        observed_values={("order.status", "status", "paid")})
    messages = [str(w.message) for w in caught]
    assert any("ship.status" in m for m in messages)               # the type warning
    assert any("authorized" in m for m in messages)                # the later value warning

    # and with only the TYPE harvest: a healthy first step (values-omitted
    # continue) must not stop the type check on the second step
    with pytest.warns(UncheckablePolicyWarning, match="order.status"):
        compile_feature(feature, registry, observed_event_types={"ship.status"})


# -- monitor semantics exposed by the campaign ---------------------------------


def test_within_response_exactly_at_the_deadline_loses_the_tie():
    # pins the documented boundary (SEMANTICS.md "Deadline boundary"): a
    # response exactly at trigger_time + seconds is too late; the timeout wins
    # the tie because due deadlines fire before the event is dispatched. (This
    # is also why WithinMonitor.on_event's own <=/<' at equality is engine-
    # unreachable: mutant on_event_12 is equivalent, not a gap.)
    policies = compile_one(WITHIN_POLICY)
    _, verdicts = run_batch(policies, [ev(1.0, "cancelled"), ev(6.0, "refunded")], grace=0)
    assert [v.verdict for v in verdicts] == ["violated"]
    assert verdicts[0].at == 6.0                     # decided at the deadline itself


def test_within_on_timeout_before_the_deadline_is_not_due():
    # kills automaton WithinMonitor on_timeout_1 (or -> and precedence): the
    # defensive guard against an early timeout call must hold at unit level.
    monitor = WithinMonitor(lambda e: e.payload.get("status") == "cancelled",
                            lambda e: e.payload.get("status") == "refunded", 5.0)
    monitor.on_event(ev(1.0, "cancelled"))
    assert monitor.on_timeout(3.0) is None           # deadline is 6.0
    assert monitor.on_timeout(6.0) == "violated"


def test_since_and_historically_settle_satisfied_at_terminal():
    # kills automaton SinceMonitor on_terminal_3/4 and HistoricallyMonitor
    # on_terminal_3/4 (the returned verdict string itself): these settlements
    # were previously reached only by randomized property examples, so the
    # kill was seed-dependent. Pin it deterministically.
    registry = basic_registry()
    policies = compile_feature("""
Feature: f
  Scenario: reviewed since flagged
    Then an order is "reviewed" since an order is "flagged"

  Scenario: always created
    Then an order is "created" always holds
""", registry)
    events = [ev(1.0, "created"),
              Event("order.done", 2.0, {"order_id": "E1"}, {}, "test")]
    _, verdicts = run_batch(policies, events, grace=0, terminal={"order.done"})
    assert {(v.policy_id, v.verdict) for v in verdicts} == {
        ("reviewed since flagged", "satisfied"),
        ("always created", "satisfied"),
    }


def test_previously_satisfied_evidence_names_the_prior_event():
    # kills automaton PreviouslyMonitor on_event_5 (deciding prior -> None),
    # another seed-marginal kill pinned deterministically: a satisfied
    # previously-verdict's evidence is the prior event AND the trigger.
    registry = basic_registry()
    policies = compile_feature("""
Feature: f
  Scenario: lock follows fail
    When an order is "locked"
    Then an order is "failed" previously
""", registry)
    _, verdicts = run_batch(policies, [ev(1.0, "failed"), ev(2.0, "locked")], grace=0)
    (verdict,) = verdicts
    assert verdict.verdict == "satisfied"
    assert [e.payload["status"] for e in verdict.deciding_events] == ["failed", "locked"]


def test_since_stays_inactive_before_its_anchor():
    # kills automaton SinceMonitor on_event_9 (and -> or): events before the
    # anchor must not activate the regime.
    registry = basic_registry()
    policies = compile_feature("""
Feature: f
  Scenario: reviewed since flagged
    Then an order is "reviewed" since an order is "flagged"
""", registry)
    _, verdicts = run_batch(policies, [ev(1.0, "created"), ev(2.0, "paid")],
                            grace=0, emit_pending=True)
    assert [v.verdict for v in verdicts] == ["pending"]


def test_programmatic_constructors_carry_their_fields():
    # kills automaton _normalize_key_1 (tuple keys) and _before_1
    # (policy_id=None) on the programmatic construction path.
    composite = make_never("composite", correlation_key=("a", "b"),
                           event_types={"t"}, bad=lambda e: False)
    assert composite.correlation_key == ("a", "b")
    assert composite.policy_id == "composite"
    b = make_before("ordered", correlation_key="k", event_types={"t"},
                    prior=lambda e: True, trigger=lambda e: True)
    assert b.policy_id == "ordered"
    w = make_within("timed", correlation_key="k", seconds=1.0,
                    is_trigger=lambda e: True, is_response=lambda e: True,
                    event_types={"t"})
    assert w.policy_id == "timed"


# -- grace = 0 (arrival-order) contract ----------------------------------------


def test_grace_zero_keeps_arrival_order_and_its_bookkeeping():
    # kills engine.loop run_44 (grace > 0 -> >=: a zero-grace buffer would
    # canonically reorder equal timestamps), run_8/9/10 (late bookkeeping
    # resets), and _handle_event_7 (dropped_invalid content).
    policies = compile_one(BEFORE_POLICY)
    # same timestamp; canonical order would put "authorized" first and flip
    # the verdict, arrival order must keep "paid" first -> violated
    events = [ev(1.0, "paid"), ev(1.0, "authorized")]
    bad_time = Event("order.status", float("nan"), {"order_id": "E1"},
                     {"status": "noise"}, "test")
    engine, verdicts = run_batch(policies, events + [bad_time], grace=0)
    assert [v.verdict for v in verdicts] == ["violated"]
    assert engine.late_events == 0 and engine.dropped_late == []
    assert engine.invalid_events == 1
    assert engine.dropped_invalid[0] is bad_time


def test_batch_sources_with_a_next_event_attribute_stay_batch():
    # kills engine.loop run_47 (and -> or) and run_56 (live default True):
    # only an explicit live=True source takes the wall-clock path.
    class QueueLikeBatch(InProcessSource):
        live = False

        def next_event(self, timeout):
            raise AssertionError("batch source must not be pulled as live")

    class NoLiveFlag(InProcessSource):
        def next_event(self, timeout):
            raise AssertionError("source without a live flag must stay batch")

    for source_cls in (QueueLikeBatch, NoLiveFlag):
        src = source_cls()
        src.emit(ev(1.0, "paid"))
        verdicts = Engine(compile_one(BEFORE_POLICY), grace=0).run(src)
        assert [v.verdict for v in verdicts] == ["violated"]


# -- the live loop -------------------------------------------------------------


def _run_live_engine(policies, *, grace):
    src = QueueSource()
    got: list = []
    engine = Engine(policies, grace=grace)
    thread = threading.Thread(target=lambda: engine.run(src, sink=got.append), daemon=True)
    thread.start()
    return src, got, thread


def _wait_for(got, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(got):
            return True
        time.sleep(0.02)
    return False


def test_live_grace_zero_out_of_order_event_does_not_stall_the_wall_clock():
    # kills engine.loop _run_live_66/67/69/70/71/72/73 (the no-buffer live
    # branch): an out-of-order event must neither regress the clock anchor nor
    # crash dispatch; the armed deadline still fires on the wall.
    policies = compile_one("""
Feature: f
  Scenario: fast refund
    When an order is "cancelled"
    Then an order is "refunded" within "0.3" seconds
""")
    src, got, _ = _run_live_engine(policies, grace=0)
    src.push(ev(10.0, "cancelled"))
    src.push(ev(5.0, "stale"))          # out of order, still dispatched
    fired = _wait_for(got, lambda g: any(v.verdict == "violated" for v in g))
    src.close()
    assert fired, "the wall-clock deadline never fired"


def test_live_wall_fire_happens_neither_early_nor_a_second_late():
    # kills engine.loop _run_live_23 (immediate premature wall fire: the
    # response inside the window must win) and _run_live_29 (wait never
    # matures: the timeout case must still fire), and _run_live_40 (a
    # 1-second virtual-clock overshoot flags on-time events late).
    policies = compile_one("""
Feature: f
  Scenario: slow refund
    When an order is "cancelled"
    Then an order is "refunded" within "5" seconds

  Scenario: never bogus
    Then an order is "bogus" never happens
""")
    # (a) response arrives well inside the window: must be satisfied
    src, got, _ = _run_live_engine(policies, grace=0.2)
    src.push(ev(10.0, "cancelled"))
    time.sleep(0.4)
    src.push(ev(10.4, "refunded"))
    ok = _wait_for(got, lambda g: any(v.policy_id == "slow refund" for v in g))
    src.close()
    assert ok
    (verdict,) = [v for v in got if v.policy_id == "slow refund"]
    assert verdict.verdict == "satisfied"

    # (b) no response: the deadline fires on the wall within tolerance, and an
    # event just after the fire target is NOT dropped as late
    policies = compile_one("""
Feature: f
  Scenario: fast refund
    When an order is "cancelled"
    Then an order is "refunded" within "0.5" seconds

  Scenario: bogus caught
    Then an order is "bogus" never happens
""")
    src, got, _ = _run_live_engine(policies, grace=0.2)
    src.push(ev(10.0, "cancelled"))                       # fire target 10.7
    fired = _wait_for(got, lambda g: any(v.verdict == "violated"
                                         and v.policy_id == "fast refund" for v in g),
                      timeout=3.0)
    assert fired, "wall deadline did not fire"
    src.push(ev(10.9, "bogus"))                            # on time unless the tick overshot
    src.close()                                            # flush releases it
    caught = _wait_for(got, lambda g: any(v.policy_id == "bogus caught" for v in g))
    assert caught, "an on-time event after the wall fire was dropped as late"


# -- compiler messages: the documented refusal contract -------------------------


@pytest.mark.parametrize("feature, fragment", [
    ("Feature: f\n  Scenario: s\n    Given an order is \"a\"\n    Given an order is \"b\"\n"
     "    Then an order is \"c\" never happens", "at most one Given"),
    ("Feature: f\n  Scenario: s\n    When an order is \"a\"\n"
     "    Then an order is \"b\" has happened", "self-contained"),
    ("Feature: f\n  Scenario: s\n    Then an order is \"a\" before\n"
     "    Then an order is \"b\" before", "exactly one Then"),
    ("Feature: f\n  Scenario: s\n    Then an order is \"a\" before", "exactly one When"),
    ("Feature: f\n  Scenario: s\n    Then an order is \"a\" waffles eventually",
     "'<step> before'."),
])
def test_refusal_messages_are_the_documented_contract(feature, fragment):
    # kills compiler _split_steps_14/15/18, compile_scenario_48/55,
    # _parse_obligation_126/136: the refusal messages are quoted verbatim in
    # the README; they are a contract, not decoration.
    with pytest.raises(CompileError) as exc:
        compile_one(feature)
    assert fragment in str(exc.value)


@pytest.mark.parametrize("then, role", [
    ('Then a martian is "x" never happens', "never-predicate"),
    ('Then a martian is "x" has happened', "once-predicate"),
    ('Then a martian is "x" always holds', "historically-predicate"),
    ('Then a martian is "x" since an order is "flagged"', "since-phi"),
    ('Then an order is "reviewed" since a martian is "x"', "since-psi"),
])
def test_unresolved_self_contained_steps_name_their_role(then, role):
    # kills compiler _parse_obligation_3/12/25/38/51/63 (where=None): the
    # unresolved-step error says WHICH step slot failed to resolve.
    with pytest.raises(CompileError) as exc:
        compile_one(f"Feature: f\n  Scenario: s\n    {then}")
    assert role in str(exc.value)


@pytest.mark.parametrize("body, role", [
    ('When a martian is "x"\n    Then an order is "a" before', "When"),
    ('When an order is "paid"\n    Then a martian is "x" before', "before-condition"),
    ('When an order is "paid"\n    Then a martian is "x" previously', "previously-condition"),
    ('When an order is "paid"\n    Then a martian is "x" within "5" seconds', "within-response"),
    ('Given a martian is "x"\n    Then an order is "a" never happens', "Given-scope"),
    ('Given a martian is "x" until an order is "b"\n    Then an order is "a" never happens',
     "Given-scope"),
    ('Given an order is "b" until a martian is "x"\n    Then an order is "a" never happens',
     "Given-until"),
])
def test_unresolved_triggered_and_scoped_steps_name_their_role(body, role):
    # kills compiler compile_scenario_59, _parse_scope_5/17/29,
    # _parse_obligation_79/105/118
    with pytest.raises(CompileError) as exc:
        compile_one(f"Feature: f\n  Scenario: s\n    {body}")
    assert role in str(exc.value)


def test_ambiguous_step_error_names_the_candidates():
    # kills compiler _resolve_one_7
    registry = basic_registry()

    @registry.trigger('an order is "{s}"', step_id="order.other",
                      event_type="order.status", correlation_key="order_id")
    def other(ctx, event, s):
        return False

    with pytest.raises(CompileError) as exc:
        compile_feature("Feature: f\n  Scenario: s\n"
                        "    Then an order is \"x\" never happens", registry)
    assert "order.status.is" in str(exc.value) and "order.other" in str(exc.value)


# -- the smaller modules: contracts the campaign showed were unpinned -----------


def test_catalog_store_writes_a_stable_reviewable_artifact(tmp_path):
    # kills catalog.store save_catalog_18/19/22/23/24/25 (indent/sort_keys)
    # and catalog.condition payload_fields_30, catalog.registry register_25/
    # 27/36: the committed catalog is a diffable, deterministic, versioned
    # artifact with the documented field values.
    from behave_rv.catalog.store import load_catalog, save_catalog

    registry = basic_registry()
    path = tmp_path / "catalog.json"
    save_catalog(path, registry.entries())
    text = path.read_text()
    assert '\n  "' in text                                   # indented, multi-line
    assert text.index('"kind"') < text.index('"step_id"')    # keys sorted
    save_catalog(path, registry.entries())
    assert path.read_text() == text                          # byte-stable

    (entry,) = load_catalog(path)
    assert entry.provenance == "llm"
    assert entry.version == 1
    assert entry.signature.payload_fields == {"status": "any"}


def test_registry_alias_errors_and_resolution_phrasing():
    # kills catalog.registry alias_2 (message) and resolve_17 (phrasing=None):
    # the resolution reports WHICH phrasing matched, aliases included.
    registry = basic_registry()
    with pytest.raises(KeyError, match="unknown step_id"):
        registry.alias("nope", "whatever")
    registry.alias("order.status.is", 'the order reaches "{status}"')
    (res,) = registry.resolve('the order reaches "paid"')
    assert res.phrasing == 'the order reaches "{status}"'
    (res,) = registry.resolve('an order is "paid"')
    assert res.phrasing == 'an order is "{status}"'


def test_signature_change_description_names_exactly_the_changed_facets():
    # kills catalog.diff describe_signature_change_4/8/9/12/14: the Break
    # detail names what moved and nothing else.
    from behave_rv.catalog.diff import describe_signature_change
    from behave_rv.catalog.entry import StepSignature

    def sig(**over):
        base = dict(event_type="t", trigger_condition="c", payload_fields={"a": "any"},
                    referenced_fields={"a"}, correlation_key=("k",),
                    condition_fingerprint="f1")
        base.update(over)
        return StepSignature(**base)

    cases = [
        (sig(correlation_key=("k", "k2")), "correlation_key",
         ["payload_fields", "referenced_fields"]),
        (sig(referenced_fields={"b"}), "referenced_fields", ["correlation_key"]),
        (sig(payload_fields={"b": "any"}), "payload_fields",
         ["correlation_key", "referenced_fields"]),
        (sig(condition_fingerprint="f2"), "condition changed",
         ["payload_fields", "correlation_key"]),
    ]
    for changed, expected, absent in cases:
        detail = describe_signature_change(sig(), changed)
        assert expected in detail
        for facet in absent:
            assert facet not in detail


def test_print_sink_renders_violations_and_compact_lines():
    # kills verdict.sinks PrintSink __init___1/5 and emit_3/4/20/21/22/25/26/
    # 27/28: violations render the authored scenario followed by a blank line;
    # other verdicts are one compact line; compact_ok=False silences them.
    import io
    from behave_rv.verdict.sinks import PrintSink

    policies = compile_one(BEFORE_POLICY)
    stream = io.StringIO()
    _, verdicts = run_batch(policies, [ev(1.0, "authorized"), ev(2.0, "paid")],
                            grace=0, sink=PrintSink(policies, stream))
    out = stream.getvalue()
    assert "[order_id=E1] satisfied: paid after authorized @ t=2.0" in out

    stream = io.StringIO()
    run_batch(policies, [ev(1.0, "paid")], grace=0, sink=PrintSink(policies, stream))
    out = stream.getvalue()
    assert "✗" in out and 'an order is "paid"' in out       # the rendered scenario
    assert out.endswith("\n\n")                              # the separating blank line

    stream = io.StringIO()
    run_batch(policies, [ev(1.0, "authorized"), ev(2.0, "paid")],
              grace=0, sink=PrintSink(policies, stream, compact_ok=False))
    assert stream.getvalue() == ""


def test_safe_value_escapes_exactly_the_control_characters():
    # kills verdict.explain safe_value_6/7/10: printable stays raw (spaces
    # included), C0 controls and DEL are escaped, above-ASCII is untouched.
    from behave_rv.verdict.explain import safe_value

    assert safe_value("plain text") == "plain text"
    assert "\x1f" not in safe_value("a\x1fb")
    assert "\x7f" not in safe_value("a\x7fb")
    assert safe_value("caf\xe9") == "caf\xe9"


def test_explanation_marks_and_bindings_render():
    # kills verdict.explain explain_verdict_8/10/14 and render_explanation_4:
    # the failing step carries the violated mark and the bound value.
    from behave_rv.verdict.explain import explain_verdict

    policies = compile_one(BEFORE_POLICY)
    _, verdicts = run_batch(policies, [ev(1.0, "paid")], grace=0)
    (verdict,) = verdicts
    text = explain_verdict(verdict, policies[0].authored_scenario,
                           policies[0].failing_step_index)
    assert "✗" in text
    assert '"authorized"' in text and '"paid"' in text


def test_payload_fields_sees_subscript_reads():
    # kills catalog.condition payload_fields_28..32: the signature's field map
    # must cover ``event.payload["k"]`` reads, not only ``.get("k")``.
    from behave_rv.catalog.condition import payload_fields

    def step(ctx, event, limit):
        return event.payload["amount"] > float(limit)

    assert payload_fields(step) == {"amount": "any"}


def test_the_rendered_explanation_is_a_contract():
    # kills the verdict.explain family (explain_verdict_3/8/10/14/19/28/29/30/
    # 35, render_explanation_1/2/4/5/7): the README shows this format
    # verbatim; header, marks, comments, binding sources, and sections are
    # load-bearing, including the build-time invalidation path.
    from types import SimpleNamespace

    from behave_rv.verdict.explain import explain_verdict, render_explanation

    policies = compile_one(BEFORE_POLICY)
    # a context event ahead of the deciding one, so both sections render
    _, verdicts = run_batch(policies, [ev(0.5, "created"), ev(1.0, "paid")], grace=0)
    (violated,) = verdicts
    text = explain_verdict(violated, policies[0].authored_scenario,
                           policies[0].failing_step_index)
    lines = text.splitlines()
    assert "POLICY 'paid after authorized'" in text
    assert "ENTITY order_id=E1" in text
    assert "VERDICT violated @ t=1.0" in text
    assert "✗" in text
    assert any(line.endswith("# violated") for line in lines)
    # section headers are exact lines, and the output is genuinely line-joined
    assert "Deciding events:" in lines
    assert "Recent context:" in lines
    assert lines[1].startswith("Scenario:")

    # a composite entity key renders comma-separated
    from behave_rv.verdict.record import Verdict
    composite = Verdict(policy_id=violated.policy_id,
                        entity_key={"region": "eu", "order_id": "E1"},
                        verdict="violated", trigger_event=violated.trigger_event,
                        witnessing_trace=[], at=1.0,
                        deciding_events=violated.deciding_events)
    header = explain_verdict(composite, policies[0].authored_scenario,
                             policies[0].failing_step_index).splitlines()[0]
    assert "region=eu, order_id=E1" in header

    # placeholders flow through explain_verdict from the verdict's bindings
    phrasing_scenario = SimpleNamespace(name="catalog phrasing", steps=[
        SimpleNamespace(keyword="Then", name='an order is "{status}"')])
    text = explain_verdict(violated, phrasing_scenario, 0)
    assert 'an order is "paid"' in text

    # a pending verdict renders its own mark, not the violated one
    _, verdicts = run_batch(policies, [ev(1.0, "authorized")], grace=0,
                            emit_pending=True)
    (pending,) = verdicts
    text = explain_verdict(pending, policies[0].authored_scenario,
                           policies[0].failing_step_index)
    assert "·" in text and "# pending" in text and "✗" not in text

    # the invalidation path: placeholders bound directly, unknown mark falls
    # back to ✗, and the mark parameter's default is "violated"
    scenario = SimpleNamespace(name="catalog phrasing", steps=[
        SimpleNamespace(keyword="When", name='an order is "{status}"'),
        SimpleNamespace(keyword="Then", name='order {order_id} pays {amount}'),
    ])
    text = render_explanation(
        scenario, bindings={"status": "paid", "order_id": "E7", "amount": "9"},
        failing_step_index=1, mark="invalidated")
    assert 'an order is "paid"' in text
    assert "order E7 pays 9" in text
    assert "✗" in text and "# invalidated" in text
    assert "\n    When" in text                      # unmarked steps stay indented

    defaulted = render_explanation(scenario, bindings={}, failing_step_index=0)
    assert any(line.endswith("# violated") for line in defaulted.splitlines())

    # and bindings_from_verdict pulls from all three sources
    from behave_rv.verdict.explain import bindings_from_verdict
    bound = bindings_from_verdict(violated)
    assert bound["order_id"] == "E1"                 # entity key + trigger bindings
    assert bound["status"] == "paid"                 # trigger payload


def test_replay_skips_blank_lines_between_events(tmp_path):
    # kills events.sources.replay events_9 (continue -> break): a blank line
    # mid-file must not truncate the replay.
    from behave_rv.events.sources.replay import ReplaySource, record_events

    path = tmp_path / "trace.jsonl"
    record_events(path, [ev(1.0, "a"), ev(2.0, "b")])
    lines = path.read_text().splitlines()
    path.write_text(lines[0] + "\n\n" + lines[1] + "\n")
    replayed = list(ReplaySource(path).events())
    assert [e.payload["status"] for e in replayed] == ["a", "b"]


def test_parse_feature_carries_the_filename():
    # kills compile.parser_bridge parse_feature_2/4: the filename flows into
    # the parsed model (and from there into duplicate-policy diagnostics).
    from behave_rv.compile.parser_bridge import parse_feature

    feature = parse_feature("Feature: f\n  Scenario: s\n"
                            "    Then an order is \"x\" never happens",
                            filename="policies/x.feature")
    assert feature.filename == "policies/x.feature"


def test_emit_event_builds_a_dispatchable_event():
    # kills events.sources.inprocess emit_event_3/5: the convenience builder
    # must produce an event the engine can actually use.
    src = InProcessSource()
    src.emit_event("order.status", 1.5, {"order_id": "E9"}, {"status": "paid"})
    (event,) = list(src.events())
    assert event.event_time == 1.5
    assert event.payload == {"status": "paid"}
    assert event.bindings == {"order_id": "E9"}


def test_queue_source_close_contract():
    # kills events.sources.subscription close_2/close_3 and push_1: push
    # after close raises with a reason; close is idempotent.
    src = QueueSource()
    src.close()
    src.close()
    with pytest.raises(RuntimeError, match="closed"):
        src.push(ev(1.0, "x"))


def test_reorder_buffer_accepts_identical_duplicate_events():
    # regression pin (kills nothing by itself): exact duplicates are a legal
    # stream and must both come out. The sequence-counter mutants turned out
    # equivalent -- Event value-equality resolves heap ties without ordering
    # comparisons -- but this contract deserves a deterministic guard anyway.
    from behave_rv.events.watermark import ReorderBuffer

    buffer = ReorderBuffer(1.0)
    duplicate = ev(1.0, "same")
    buffer.push(duplicate)
    buffer.push(duplicate)
    buffer.push(ev(10.0, "later"))
    released = list(buffer.releasable()) + list(buffer.flush())
    assert [e.payload["status"] for e in released] == ["same", "same", "later"]


def test_value_liveness_warns_when_only_values_are_provided():
    # kills compiler _warn_if_uncheckable_3 (inverted early return): passing
    # observed_values alone must still produce value-level warnings.
    with pytest.warns(UncheckablePolicyWarning, match="paid"):
        compile_feature(BEFORE_POLICY, basic_registry(),
                        observed_values={("order.status", "status", "authorized")})


def test_value_liveness_checks_every_step_of_the_policy():
    # kills compiler _warn_if_uncheckable_17/19 (continue -> break): the first
    # step being healthy must not hide a later step's missing value.
    observed = {("order.status", "status", "paid")}      # trigger fine, prior missing
    with pytest.warns(UncheckablePolicyWarning, match="authorized"):
        compile_feature(BEFORE_POLICY, basic_registry(), observed_values=observed)
