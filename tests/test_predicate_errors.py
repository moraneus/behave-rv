"""A raising predicate matches nothing, is recorded, and never kills the run.

Interrogation finding C3: one step author's bug (a KeyError inside a predicate)
crashed the whole engine with nothing recorded. The engine now contains it,
mirroring the sink-failure policy: contain, record, continue.
"""

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.automaton import PredicateError
from behave_rv.compile.compiler import compile_feature
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource


def _reg():
    r = StepRegistry()

    @r.trigger('an order is "{status}"', step_id="broken.step",
               event_type="order.status", correlation_key="order_id")
    def broken(ctx, e, status):
        return e.payload["missing_field"] == status     # the step author's bug

    @r.trigger('a delivery is "{status}"', step_id="healthy.step",
               event_type="order.status", correlation_key="order_id")
    def healthy(ctx, e, status):
        return e.payload.get("status") == status

    return r


FEAT = ('Feature: f\n'
        '  Scenario: broken policy\n    Then an order is "cancelled" never happens\n'
        '  Scenario: healthy policy\n    Then a delivery is "cancelled" never happens\n')


def test_raising_predicate_is_contained_recorded_and_isolated():
    policies = compile_feature(FEAT, _reg())
    src = InProcessSource()
    src.emit(Event("order.status", 1.0, {"order_id": "A"}, {"status": "cancelled"}, "t"))

    engine = Engine(policies)
    verdicts = engine.run(src, emit_pending=True)        # must not raise

    # the healthy policy on the same stream still produced its correct verdict
    healthy = [(v.policy_id, v.verdict) for v in verdicts if v.policy_id == "healthy policy"]
    assert healthy == [("healthy policy", "violated")]

    # the broken policy matched nothing (its predicate raised) -> pending
    broken = [(v.policy_id, v.verdict) for v in verdicts if v.policy_id == "broken policy"]
    assert broken == [("broken policy", "pending")]

    # and the failure is visible, naming the step and the policy
    assert engine.predicate_errors == 1
    assert isinstance(engine.first_predicate_error, PredicateError)
    assert engine.predicate_error_sources == [("broken policy", "broken.step")]


def test_raising_close_predicate_does_not_blind_the_forbidden_check():
    # Audit G2c: containment used to abort the whole on_event, so a raising
    # `until` closing predicate suppressed the forbidden check after it and the
    # policy sat pending. Containment is now per predicate call.
    r = StepRegistry()

    @r.trigger('a user is "{status}"', step_id="user.is",
               event_type="session.status", correlation_key="user_id")
    def user_is(ctx, e, status):
        return e.payload.get("status") == status

    @r.trigger('a flag is "{status}"', step_id="flag.is",
               event_type="session.status", correlation_key="user_id")
    def flag_is(ctx, e, status):
        raise RuntimeError("close predicate broken")

    feat = ('Feature: f\n  Scenario: s\n'
            '    Given a user is "locked" until a flag is "unlocked"\n'
            '    Then a user is "action" never happens\n')
    (p,) = compile_feature(feat, r)
    src = InProcessSource()
    src.emit(Event("session.status", 1.0, {"user_id": "u1"}, {"status": "locked"}, "t"))
    src.emit(Event("session.status", 2.0, {"user_id": "u1"}, {"status": "action"}, "t"))
    engine = Engine([p])
    verdicts = engine.run(src, emit_pending=True)

    assert [v.verdict for v in verdicts] == ["violated"]     # the check still ran
    assert engine.predicate_errors >= 1                       # and the break is visible
    assert ("s", "flag.is") in engine.predicate_error_sources


def test_raising_deciding_events_does_not_crash_the_engine():
    # Audit G2f: a raise inside deciding_events (monitor-internal, outside
    # on_event) crashed the run. Now contained like the sink path.
    from behave_rv.compile.automaton import NeverMonitor, Policy

    class EvilMonitor(NeverMonitor):
        def deciding_events(self):
            raise RuntimeError("deciding boom")

    pol = Policy("evil", ("user_id",), frozenset({"session.status"}),
                 lambda: EvilMonitor(lambda e: e.payload.get("status") == "x"))
    src = InProcessSource()
    src.emit(Event("session.status", 1.0, {"user_id": "u1"}, {"status": "x"}, "t"))
    engine = Engine([pol])
    (v,) = engine.run(src, emit_pending=True)

    assert v.verdict == "violated"                 # the verdict stands
    assert v.deciding_events == []                 # evidence absent, not fatal
    assert engine.predicate_errors == 1            # and the raise is visible


def test_raise_through_on_event_leaves_monitor_state_atomic():
    # Soundness pass: a raise propagating through on_event must leave the
    # monitor EXACTLY as it was (no partial scope flag), with the error
    # recorded, and a later clean event must evaluate from the pre-raise state.
    from behave_rv.compile.automaton import Policy, ScopedNeverMonitor

    def scope_pred(e):
        # the glitch event OPENS the scope (a state update)...
        return e.payload.get("status") in ("locked", "glitch")

    def bad_pred(e):
        # ...and then the forbidden predicate raises on it: without atomic
        # restore, the half-applied open scope (anchored at the glitch event)
        # would be kept.
        if e.payload.get("status") == "glitch":
            raise RuntimeError("raw predicate broken")
        return e.payload.get("status") == "action"

    captured = {}

    def factory():
        m = ScopedNeverMonitor(scope_pred, bad_pred)
        captured["m"] = m
        return m

    pol = Policy("p", ("user_id",), frozenset({"session.status"}), factory)

    def ue(status, t):
        return Event("session.status", float(t), {"user_id": "u1"},
                     {"status": status}, "t")

    src = InProcessSource()
    src.emit(ue("glitch", 1.0))     # scope pred would OPEN, then bad raises:
    src.emit(ue("locked", 2.0))     # ...state must have been restored (closed)
    src.emit(ue("action", 3.0))     # violation from the correct post-lock state
    engine = Engine([pol])
    verdicts = engine.run(src, emit_pending=True)

    assert engine.predicate_errors == 1
    m = captured["m"]
    assert m.settled is True                               # the clean events applied
    assert [v.verdict for v in verdicts] == ["violated"]
    # deciding events show the CLEAN opening lock at t=2, not the rolled-back glitch
    assert [e.event_time for e in verdicts[0].deciding_events] == [2.0, 3.0]


def test_raise_equals_skip_equivalence():
    # The strongest atomicity check: a trace with raising events must produce
    # exactly the verdicts of the same trace with those events removed.
    from behave_rv.compile.automaton import Policy, BeforeMonitor

    def make_policy():
        def prior(e):
            if e.payload.get("status") == "glitch":
                raise RuntimeError("boom")     # raw predicate: propagates
            return e.payload.get("status") == "authorized"

        def trigger(e):
            if e.payload.get("status") == "glitch":
                raise RuntimeError("boom")
            return e.payload.get("status") == "paid"

        return Policy("p", ("order_id",), frozenset({"order.status"}),
                      lambda: BeforeMonitor(prior, trigger))

    def oe(status, t):
        return Event("order.status", float(t), {"order_id": "A"},
                     {"status": status}, "t")

    full = [oe("authorized", 1.0), oe("glitch", 2.0), oe("paid", 3.0)]
    clean = [e for e in full if e.payload["status"] != "glitch"]

    def run(events):
        src = InProcessSource()
        for e in events:
            src.emit(e)
        return [(v.entity_key["order_id"], v.verdict, v.at)
                for v in Engine([make_policy()]).run(src, emit_pending=True)]

    assert run(full) == run(clean) == [("A", "satisfied", 3.0)]


def test_keyboard_interrupt_is_never_swallowed():
    import pytest

    r = StepRegistry()

    @r.trigger('a user is "{status}"', step_id="ki.step",
               event_type="session.status", correlation_key="user_id")
    def ki(ctx, e, status):
        raise KeyboardInterrupt()

    (p,) = compile_feature(
        'Feature: f\n  Scenario: s\n    Then a user is "x" never happens\n', r)
    src = InProcessSource()
    src.emit(Event("session.status", 1.0, {"user_id": "u1"}, {"status": "x"}, "t"))
    with pytest.raises(KeyboardInterrupt):
        Engine([p]).run(src)
