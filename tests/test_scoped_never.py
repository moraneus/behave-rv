"""Scoped never: the Given scope wired into the never operator.

Two forms: latching (`Given <p>` -- once open, open forever) and interval
(`Given <p> until <q>` -- open/close, may reopen). Violated at the first
forbidden event while the scope is open; satisfied at terminal otherwise,
including the vacuous never-opened case. Scope state updates BEFORE the
forbidden check on the same event (consistent with how `before` sets its
prior before testing its trigger).
"""

from __future__ import annotations

import pytest

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.automaton import ScopedNeverMonitor
from behave_rv.compile.compiler import CompileError, compile_feature
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource


def ev(status, t, uid="u1"):
    return Event("session.status", float(t), {"user_id": uid},
                 {"status": status}, "test")


def is_status(s):
    return lambda e: e.payload.get("status") == s


# --- the monitor, driven directly -------------------------------------------


def test_latching_scope_violates_on_forbidden_inside_scope():
    m = ScopedNeverMonitor(is_status("locked"), is_status("action"))
    assert m.on_event(ev("action", 1.0)) is None          # scope closed: no violation
    assert m.on_event(ev("locked", 2.0)) is None          # scope opens
    assert m.on_event(ev("action", 3.0)) == "violated"
    assert [e.payload["status"] for e in m.deciding_events()] == ["locked", "action"]


def test_latching_scope_never_closes():
    m = ScopedNeverMonitor(is_status("locked"), is_status("action"))
    m.on_event(ev("locked", 1.0))
    m.on_event(ev("unlocked", 2.0))                        # no closing predicate: ignored
    assert m.on_event(ev("action", 3.0)) == "violated"


def test_vacuous_scope_never_opens_satisfied_at_terminal():
    m = ScopedNeverMonitor(is_status("locked"), is_status("action"))
    assert m.on_event(ev("action", 1.0)) is None
    assert m.on_terminal() == "satisfied"


def test_interval_scope_closes_and_reopens():
    m = ScopedNeverMonitor(is_status("locked"), is_status("action"),
                           is_status("unlocked"))
    m.on_event(ev("locked", 1.0))
    m.on_event(ev("unlocked", 2.0))                        # scope closes
    assert m.on_event(ev("action", 3.0)) is None           # legit action after unlock
    m.on_event(ev("locked", 4.0))                          # reopens
    assert m.on_event(ev("action", 5.0)) == "violated"
    # deciding events are the RE-opening lock and the offending action
    assert [(e.payload["status"], e.event_time) for e in m.deciding_events()] == \
        [("locked", 4.0), ("action", 5.0)]


def test_same_event_opens_scope_before_forbidden_check():
    # state update precedes the forbidden test, as with before's same-event rule
    m = ScopedNeverMonitor(is_status("hot"), is_status("hot"))
    assert m.on_event(ev("hot", 1.0)) == "violated"
    assert [e.event_time for e in m.deciding_events()] == [1.0]   # deduped


def test_same_event_closes_scope_before_forbidden_check():
    m = ScopedNeverMonitor(is_status("locked"), is_status("reset"),
                           is_status("reset"))
    m.on_event(ev("locked", 1.0))
    assert m.on_event(ev("reset", 2.0)) is None            # closes first, no violation


# --- the compiler -------------------------------------------------------------


@pytest.fixture
def reg():
    r = StepRegistry()

    @r.trigger('a user is "{status}"', step_id="user.is",
               event_type="session.status", correlation_key="user_id")
    def f(ctx, e, status):
        return e.type == "session.status" and e.payload.get("status") == status

    @r.trigger('an order is "{status}"', step_id="order.is",
               event_type="order.status", correlation_key="order_id")
    def g(ctx, e, status):
        return e.type == "order.status" and e.payload.get("status") == status

    return r


def run(policy, events):
    src = InProcessSource()
    for e in events:
        src.emit(e)
    return Engine([policy], terminal_event_types={"session.end"}).run(
        src, emit_pending=True)


SCOPED = '''Feature: f
  Scenario: a locked user must never act
    Given a user is "locked"
    Then a user is "action" never happens
'''

SCOPED_UNTIL = '''Feature: f
  Scenario: a locked user must never act until unlocked
    Given a user is "locked" until a user is "unlocked"
    Then a user is "action" never happens
'''


def test_scoped_never_compiles_and_catches(reg):
    (p,) = compile_feature(SCOPED, reg)
    trace = [ev("login_ok", 1.0), ev("action", 2.0),      # legit
             ev("locked", 3.0), ev("action", 4.0)]         # the bug
    (v,) = [x for x in run(p, trace) if x.verdict != "pending"]
    assert v.verdict == "violated"
    assert [e.payload["status"] for e in v.deciding_events] == ["locked", "action"]


def test_scoped_until_compiles_and_respects_unlock(reg):
    (p,) = compile_feature(SCOPED_UNTIL, reg)
    ok = [ev("locked", 1.0), ev("unlocked", 2.0), ev("action", 3.0),
          Event("session.end", 4.0, {"user_id": "u1"}, {}, "test")]
    (v,) = run(p, ok)
    assert v.verdict == "satisfied"                        # unlock closed the scope


def test_unscoped_never_unchanged(reg):
    (p,) = compile_feature(
        'Feature: f\n  Scenario: s\n    Then a user is "banned" never happens\n', reg)
    (v,) = run(p, [ev("banned", 1.0)])
    assert v.verdict == "violated"


def test_when_plus_never_refusal_points_to_given(reg):
    bad = ('Feature: f\n  Scenario: s\n    When a user is "locked"\n'
           '    Then a user is "action" never happens\n')
    with pytest.raises(CompileError, match="Given"):
        compile_feature(bad, reg)


def test_given_plus_when_plus_never_refused(reg):
    bad = ('Feature: f\n  Scenario: s\n    Given a user is "locked"\n'
           '    When a user is "action"\n'
           '    Then a user is "banned" never happens\n')
    with pytest.raises(CompileError):
        compile_feature(bad, reg)


def test_given_on_other_operators_still_refused(reg):
    bad = ('Feature: f\n  Scenario: s\n    Given a user is "locked"\n'
           '    When a user is "action"\n'
           '    Then a user is "login_ok" before\n')
    with pytest.raises(CompileError, match="Given"):
        compile_feature(bad, reg)


def test_scope_key_mismatch_refused(reg):
    bad = ('Feature: f\n  Scenario: s\n    Given an order is "flagged"\n'
           '    Then a user is "action" never happens\n')
    with pytest.raises(CompileError, match="entity key"):
        compile_feature(bad, reg)
