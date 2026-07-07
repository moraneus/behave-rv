"""Verify every todo-app policy against the mock's own flows,
deterministically, no web UI involved."""

from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource

from demo.todo_app.service import TodoService
from demo.todo_app.steps import build_registry, load_policies


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


def run_flow(flow_name, eid="X", advance_past_timers=False):
    clock = FakeClock()
    events = []
    service = TodoService(events.append, clock=clock, sleep=clock.sleep)
    getattr(service, flow_name)(eid)
    if advance_past_timers:
        events.append(Event("clock.tick", clock.now + 60.0, {}, {}, "test"))
    src = InProcessSource()
    for e in events:
        src.emit(e)
    verdicts = Engine(load_policies(build_registry())).run(src, emit_pending=True)
    return {(v.policy_id, next(iter(v.entity_key.values()))): v for v in verdicts}


COMPLETE_AFTER_START = "a task may only be completed after it was started"
EDIT_AFTER_CREATE = "an edit follows a create"
REOPEN_AFTER_COMPLETE = "a reopen follows a completion"
EVENTUALLY_COMPLETED = "every task is eventually completed"
EVENTUALLY_ARCHIVED = "every task is eventually archived"
DUE_WINDOW = "a started task completes within the due window"
CHECKPOINT_WINDOW = "a started task reaches a checkpoint promptly"
ARCHIVED_NEVER_EDITED = "an archived task must never be edited"
DELETED_NEVER_EDITED = "a deleted task must never be edited"
BLOCKED_UNTIL = "a blocked task must not complete, until unblocked"
NEVER_CORRUPTED = "a task is never corrupted"
SYNC_HISTORICALLY = "sync always succeeds this session"


def violated(vmap):
    return {(p, e) for (p, e), v in vmap.items() if v.verdict == "violated"}


def test_quick_flow_is_clean():
    vmap = run_flow("flow_quick_task", advance_past_timers=True)
    assert violated(vmap) == set()
    for policy in (COMPLETE_AFTER_START, DUE_WINDOW, CHECKPOINT_WINDOW,
                   EVENTUALLY_COMPLETED):
        assert vmap[(policy, "X")].verdict == "satisfied", policy
    assert vmap[(EVENTUALLY_ARCHIVED, "X")].verdict == "pending"  # long-pending


def test_edit_archive_flow_is_clean():
    vmap = run_flow("flow_edit_and_archive", advance_past_timers=True)
    assert violated(vmap) == set()
    assert vmap[(EDIT_AFTER_CREATE, "X")].verdict == "satisfied"
    assert vmap[(EVENTUALLY_ARCHIVED, "X")].verdict == "satisfied"


def test_rework_flow_is_clean():
    vmap = run_flow("flow_rework", advance_past_timers=True)
    assert violated(vmap) == set()
    assert vmap[(REOPEN_AFTER_COMPLETE, "X")].verdict == "satisfied"


def test_block_unblock_flow_is_clean():
    vmap = run_flow("flow_block_unblock", advance_past_timers=True)
    assert violated(vmap) == set()
    assert vmap[(DUE_WINDOW, "X")].verdict == "satisfied"


def test_healthy_sync_is_clean_and_holding():
    vmap = run_flow("flow_sync_healthy")
    assert violated(vmap) == set()
    assert vmap[(SYNC_HISTORICALLY, "X")].verdict == "pending"    # still holding


def test_bug_complete_unstarted():
    vmap = run_flow("bug_complete_unstarted", advance_past_timers=True)
    assert violated(vmap) == {(COMPLETE_AFTER_START, "X")}


def test_bug_edit_uncreated():
    vmap = run_flow("bug_edit_uncreated")
    assert violated(vmap) == {(EDIT_AFTER_CREATE, "X")}


def test_bug_reopen_uncompleted():
    vmap = run_flow("bug_reopen_uncompleted", advance_past_timers=True)
    assert violated(vmap) == {(REOPEN_AFTER_COMPLETE, "X")}


def test_bug_edit_archived():
    vmap = run_flow("bug_edit_archived", advance_past_timers=True)
    assert violated(vmap) == {(ARCHIVED_NEVER_EDITED, "X")}
    v = vmap[(ARCHIVED_NEVER_EDITED, "X")]
    assert [e.payload["status"] for e in v.deciding_events] == ["archived", "edited"]


def test_bug_edit_deleted():
    vmap = run_flow("bug_edit_deleted")
    assert violated(vmap) == {(DELETED_NEVER_EDITED, "X")}


def test_bug_miss_due_window_fires_only_the_due_timer():
    vmap = run_flow("bug_miss_due_window", advance_past_timers=True)
    assert violated(vmap) == {(DUE_WINDOW, "X")}
    assert vmap[(CHECKPOINT_WINDOW, "X")].verdict == "satisfied"


def test_bug_miss_checkpoint_fires_both_timers():
    vmap = run_flow("bug_miss_checkpoint", advance_past_timers=True)
    assert violated(vmap) == {(DUE_WINDOW, "X"), (CHECKPOINT_WINDOW, "X")}


def test_bug_blocked_completes_fires_the_until_rule():
    vmap = run_flow("bug_blocked_completes", advance_past_timers=True)
    assert violated(vmap) == {(BLOCKED_UNTIL, "X")}
    v = vmap[(BLOCKED_UNTIL, "X")]
    assert [e.payload["status"] for e in v.deciding_events] == ["blocked", "completed"]


def test_bug_sync_fail_breaks_historically():
    vmap = run_flow("bug_sync_fails")
    assert violated(vmap) == {(SYNC_HISTORICALLY, "X")}
    v = vmap[(SYNC_HISTORICALLY, "X")]
    assert v.deciding_events[-1].payload["status"] == "sync_fail"


def run_manual(actions, advance_past_timers=False):
    """Replay board clicks: one service.act per (task_id, status) click, with
    the clock advancing between clicks the way real clicks are spaced."""
    clock = FakeClock()
    events = []
    service = TodoService(events.append, clock=clock, sleep=clock.sleep)
    for tid, status in actions:
        service.act(tid, status)
        clock.sleep(0.5)
    if advance_past_timers:
        events.append(Event("clock.tick", clock.now + 60.0, {}, {}, "test"))
    src = InProcessSource()
    for e in events:
        src.emit(e)
    verdicts = Engine(load_policies(build_registry())).run(src, emit_pending=True)
    return {(v.policy_id, next(iter(v.entity_key.values()))): v for v in verdicts}


def test_manual_board_clicks_clean_lifecycle():
    vmap = run_manual([("A", "created"), ("A", "started"), ("A", "checkpoint"),
                       ("A", "completed"), ("A", "archived")],
                      advance_past_timers=True)
    assert violated(vmap) == set()
    assert vmap[(EVENTUALLY_ARCHIVED, "A")].verdict == "satisfied"


def test_manual_board_clicks_illegal_actions_are_caught_per_task():
    # three tasks driven by hand in one interleaved session: one clean, one
    # completed unstarted, one edited after deletion
    vmap = run_manual([("A", "created"), ("B", "created"), ("C", "created"),
                       ("A", "started"), ("B", "completed"), ("C", "deleted"),
                       ("A", "checkpoint"), ("C", "edited"), ("A", "completed")],
                      advance_past_timers=True)
    assert violated(vmap) == {(COMPLETE_AFTER_START, "B"),
                              (DELETED_NEVER_EDITED, "C")}


def test_quiet_policies_never_violate_across_all_flows():
    from demo.todo_app.service import FLOWS
    for action, (_, _, flow_name) in FLOWS.items():
        vmap = run_flow(flow_name, advance_past_timers=True)
        for (policy, _entity), v in vmap.items():
            if policy in (NEVER_CORRUPTED, EVENTUALLY_ARCHIVED, EVENTUALLY_COMPLETED):
                assert v.verdict != "violated", (action, policy)
