"""Phase 6: the build-time notification channel (break / weakening / suggestion).

Three message types on separate channels so they never blur. A break only
notifies the humans whose policies used the affected step (the scoping rule).
A rename is absorbed silently. New monitorable behavior no policy covers becomes
a suggestion. An agent test that changes what it asserts is a weakening.
"""

from behave_rv.catalog.entry import CatalogEntry, StepSignature
from behave_rv.notify.channel import AgentTest, PolicyUse, notifications


def sig(*, event_type="order.status", referenced=("status",), key=("order_id",)):
    return StepSignature(event_type=event_type, trigger_condition="cond",
                         payload_fields={}, referenced_fields=set(referenced),
                         correlation_key=tuple(key))


def entry(step_id, *, phrasing="p", signature=None):
    return CatalogEntry(step_id=step_id, phrasing=phrasing, kind="trigger",
                        signature=signature or sig(), provenance="llm",
                        observed=False, version=1)


USES = [PolicyUse(policy_id="no-cancel", owner="alice", step_ids=frozenset({"s1"}))]


# --- breaks ----------------------------------------------------------------


def test_changed_used_step_breaks_and_is_scoped_to_the_owner():
    old = [entry("s1", signature=sig())]
    new = [entry("s1", signature=sig(event_type="order.state"))]

    notes = notifications(old, new, USES)

    (b,) = notes.breaks
    assert b.step_id == "s1"
    assert b.policy_id == "no-cancel"
    assert b.owner == "alice"
    assert "event_type" in b.detail
    assert notes.suggestions == []


def test_changed_unused_step_does_not_break():
    old = [entry("s2", signature=sig())]
    new = [entry("s2", signature=sig(event_type="order.state"))]

    assert notifications(old, new, USES).breaks == []


def test_removed_used_step_breaks():
    notes = notifications([entry("s1")], [], USES)
    (b,) = notes.breaks
    assert b.policy_id == "no-cancel"
    assert "removed" in b.detail


def test_only_owners_who_used_the_step_are_notified():
    uses = [
        PolicyUse("no-cancel", "alice", frozenset({"s1"})),
        PolicyUse("other", "bob", frozenset({"s2"})),
    ]
    old = [entry("s1", signature=sig())]
    new = [entry("s1", signature=sig(key=("order_id", "tenant_id")))]

    owners = {b.owner for b in notifications(old, new, uses).breaks}
    assert owners == {"alice"}


# --- silent rename ---------------------------------------------------------


def test_rename_is_absorbed_silently():
    old = [entry("s1", phrasing='an order is "{status}"', signature=sig())]
    new = [entry("s1", phrasing='the order reaches "{status}"', signature=sig())]

    notes = notifications(old, new, USES)
    assert notes.breaks == []
    assert notes.suggestions == []


# --- suggestions -----------------------------------------------------------


def test_new_uncovered_step_becomes_a_suggestion():
    notes = notifications([], [entry("fresh", phrasing="something new")], USES)
    (s,) = notes.suggestions
    assert s.step_id == "fresh"
    assert s.phrasing == "something new"


# --- weakenings ------------------------------------------------------------


def test_agent_test_changing_its_assertion_is_a_weakening():
    old_tests = [AgentTest("t1", owner="agent", asserts="delivered within 30s")]
    new_tests = [AgentTest("t1", owner="agent", asserts="delivered within 60s")]

    notes = notifications([], [], [], old_tests=old_tests, new_tests=new_tests)
    (w,) = notes.weakenings
    assert w.test_id == "t1"
    assert "30s" in w.detail and "60s" in w.detail


def test_unchanged_agent_test_is_not_a_weakening():
    tests = [AgentTest("t1", owner="agent", asserts="same")]
    notes = notifications([], [], [], old_tests=tests, new_tests=tests)
    assert notes.weakenings == []
