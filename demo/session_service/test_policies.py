"""Verify every session-service policy against the mock's own flows,
deterministically, no web UI involved."""

from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource

from demo.session_service.service import TERMINAL_TYPE, SessionService
from demo.session_service.steps import build_registry, load_policies


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


def run_flow(flow_name, uid="U", advance_past_timers=False):
    clock = FakeClock()
    events = []
    service = SessionService(events.append, clock=clock, sleep=clock.sleep)
    getattr(service, flow_name)(uid)
    if advance_past_timers:
        events.append(Event("clock.tick", clock.now + 60.0, {}, {}, "test"))
    src = InProcessSource()
    for e in events:
        src.emit(e)
    verdicts = Engine(load_policies(build_registry()),
                      terminal_event_types={TERMINAL_TYPE}).run(src, emit_pending=True)
    return {(v.policy_id, v.entity_key["user_id"]): v for v in verdicts}


ACTION_AFTER_LOGIN = "an action requires a prior successful login"
LOGOUT_AFTER_LOGIN = "a logout follows a login"
LOCK_AFTER_FAIL = "a lockout follows a failed attempt"
LOCKED_NEVER_ACTS = "a locked user must never act"
LOGGEDOUT_NEVER_ACTS = "a logged-out user must never act"
LOCKED_UNTIL = "a user must not act while locked, until unlocked"
REVIEW_WINDOW = "a locked account is reviewed within the window"
EVENTUALLY_LOGOUT = "every session eventually logs out"
NEVER_DELETED = "a user is never deleted mid session"
SINCE_FLAGGED = "a flagged user is only reviewed afterwards"


def violated(vmap):
    return {(p, e) for (p, e), v in vmap.items() if v.verdict == "violated"}


def test_login_flow_is_clean_and_terminal_settles():
    vmap = run_flow("flow_login_work_logout")
    assert violated(vmap) == set()
    for policy in (ACTION_AFTER_LOGIN, LOGOUT_AFTER_LOGIN, EVENTUALLY_LOGOUT,
                   LOCKED_NEVER_ACTS, LOGGEDOUT_NEVER_ACTS, LOCKED_UNTIL,
                   NEVER_DELETED, SINCE_FLAGGED):
        assert vmap[(policy, "U")].verdict == "satisfied", policy


def test_lock_and_review_flow_is_clean():
    vmap = run_flow("flow_lock_and_review")
    assert violated(vmap) == set()
    assert vmap[(LOCK_AFTER_FAIL, "U")].verdict == "satisfied"
    assert vmap[(REVIEW_WINDOW, "U")].verdict == "satisfied"
    assert vmap[(EVENTUALLY_LOGOUT, "U")].verdict == "pending"   # long-pending


def test_unlock_contrast_only_the_latching_rule_fires():
    vmap = run_flow("flow_unlock_contrast")
    assert violated(vmap) == {(LOCKED_NEVER_ACTS, "U")}          # the contrast
    assert vmap[(LOCKED_UNTIL, "U")].verdict == "pending"        # unlock closed it


def test_flagged_flow_is_clean():
    vmap = run_flow("flow_flagged_reviewed")
    assert violated(vmap) == set()
    assert vmap[(SINCE_FLAGGED, "U")].verdict == "pending"       # holding


def test_bug_action_without_login():
    vmap = run_flow("bug_action_without_login")
    assert violated(vmap) == {(ACTION_AFTER_LOGIN, "U")}


def test_bug_logout_without_login():
    vmap = run_flow("bug_logout_without_login")
    assert violated(vmap) == {(LOGOUT_AFTER_LOGIN, "U")}


def test_bug_stale_token_fires_both_scope_forms():
    vmap = run_flow("bug_stale_token")
    assert violated(vmap) == {(LOCKED_NEVER_ACTS, "U"), (LOCKED_UNTIL, "U")}
    v = vmap[(LOCKED_NEVER_ACTS, "U")]
    assert [e.payload["status"] for e in v.deciding_events] == ["locked", "action"]


def test_bug_act_after_logout():
    vmap = run_flow("bug_act_after_logout")
    assert violated(vmap) == {(LOGGEDOUT_NEVER_ACTS, "U")}
    v = vmap[(LOGGEDOUT_NEVER_ACTS, "U")]
    assert [e.payload["status"] for e in v.deciding_events] == ["logout", "action"]


def test_bug_lock_without_fail():
    vmap = run_flow("bug_lock_without_fail")
    assert violated(vmap) == {(LOCK_AFTER_FAIL, "U")}


def test_bug_lock_never_reviewed_fires_the_timer():
    vmap = run_flow("bug_lock_never_reviewed", advance_past_timers=True)
    assert violated(vmap) == {(REVIEW_WINDOW, "U")}
    v = vmap[(REVIEW_WINDOW, "U")]
    assert [e.payload["status"] for e in v.deciding_events] == ["locked"]


def test_bug_relock_then_act_fires_until():
    vmap = run_flow("bug_relock_then_act")
    assert violated(vmap) == {(LOCKED_NEVER_ACTS, "U"), (LOCKED_UNTIL, "U")}
    v = vmap[(LOCKED_UNTIL, "U")]
    # the until rule anchors at the RE-lock, not the first (unlocked) interval
    assert [e.payload["status"] for e in v.deciding_events] == ["locked", "action"]
    assert v.deciding_events[0].event_time > 5.0


def test_quiet_policies_never_violate_across_all_flows():
    from demo.session_service.service import FLOWS
    for action, (_, _, flow_name) in FLOWS.items():
        vmap = run_flow(flow_name, advance_past_timers=True)
        for (policy, _entity), v in vmap.items():
            if policy in (NEVER_DELETED, SINCE_FLAGGED):
                assert v.verdict != "violated", (action, policy)
