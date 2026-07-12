"""The mock session / access-control service.

Real lockout logic: three consecutive failed logins emit "locked". The clock
and sleep are injectable so test_policies.py replays exactly these flows
deterministically.
"""

from __future__ import annotations

import time

from behave_rv.events.event import Event

EVENT_TYPE = "session.status"
TERMINAL_TYPE = "session.end"
LOCK_AFTER = 3


class SessionService:
    def __init__(self, emit, clock=time.time, sleep=time.sleep, pace=0.6):
        self._emit = emit
        self._clock = clock
        self._sleep = sleep
        self._pace = pace
        self._fails: dict[str, int] = {}

    def _ev(self, uid: str, status: str) -> None:
        self._emit(Event(EVENT_TYPE, self._clock(), {"user_id": uid},
                         {"status": status}, "session-service"))
        self._sleep(self._pace)

    def _end(self, uid: str) -> None:
        self._emit(Event(TERMINAL_TYPE, self._clock(), {"user_id": uid},
                         {}, "session-service"))

    def _register_fail(self, uid: str) -> bool:
        """The one source of lockout truth: count the failure, reset and
        report True when it is the one that locks the account."""
        self._fails[uid] = self._fails.get(uid, 0) + 1
        if self._fails[uid] >= LOCK_AFTER:
            self._fails[uid] = 0
            return True
        return False

    def _fail_login(self, uid: str) -> None:
        """Real lockout logic: the third consecutive failure emits locked."""
        locks = self._register_fail(uid)
        self._ev(uid, "login_fail")
        if locks:
            self._ev(uid, "locked")

    def _lock_via_fails(self, uid: str) -> None:
        for _ in range(LOCK_AFTER):
            self._fail_login(uid)

    # -- manual actions (driven by the board UI, one event per user click) ----

    def act(self, uid: str, status: str) -> None:
        """Emit a single user-caused status event, no pacing. The board never
        blocks an action; whether it was legal is the monitor's call."""
        self._emit(Event(EVENT_TYPE, self._clock(), {"user_id": uid},
                         {"status": status}, "session-service"))

    def fail_login(self, uid: str) -> bool:
        """Manual failed login, through the SAME lockout logic as the flows:
        the third consecutive click emits locked by itself. Returns True when
        this failure locked the account."""
        locks = self._register_fail(uid)
        self.act(uid, "login_fail")
        if locks:
            self.act(uid, "locked")
        return locks

    def end_session(self, uid: str) -> None:
        """Emit the terminal event: the session's story ends here, so pending
        obligations (like 'eventually logs out') settle now."""
        self._end(uid)

    # -- normal flows ---------------------------------------------------------

    def flow_login_work_logout(self, uid: str) -> None:
        for s in ("login_ok", "action", "action", "logout"):
            self._ev(uid, s)
        self._end(uid)

    def flow_lock_and_review(self, uid: str) -> None:
        self._lock_via_fails(uid)
        self._ev(uid, "review")

    def flow_unlock_contrast(self, uid: str) -> None:
        """locked -> reviewed -> unlocked -> action: the until rule stays
        clean, while the plain (latching) scoped rule fires -- the intended
        contrast between the two scope forms."""
        self._ev(uid, "login_ok")
        self._lock_via_fails(uid)
        self._ev(uid, "review")
        self._ev(uid, "unlocked")
        self._ev(uid, "action")

    def flow_flagged_reviewed(self, uid: str) -> None:
        for s in ("login_ok", "flagged", "review"):
            self._ev(uid, s)

    # -- buggy flows ------------------------------------------------------------

    def bug_action_without_login(self, uid: str) -> None:
        self._ev(uid, "action")

    def bug_logout_without_login(self, uid: str) -> None:
        self._ev(uid, "logout")

    def bug_stale_token(self, uid: str) -> None:
        """The seeded-fault evaluation's security bug: a locked user still acts."""
        self._ev(uid, "login_ok")
        self._lock_via_fails(uid)
        self._ev(uid, "review")
        self._ev(uid, "action")

    def bug_act_after_logout(self, uid: str) -> None:
        for s in ("login_ok", "action", "logout", "action"):
            self._ev(uid, s)

    def bug_lock_without_fail(self, uid: str) -> None:
        """A lock appearing with no failed attempt right before it."""
        for s in ("login_ok", "locked", "review"):
            self._ev(uid, s)

    def bug_lock_never_reviewed(self, uid: str) -> None:
        """The session demo's live timer: locked, then silence -- the review
        SLA is violated by the wall clock."""
        self._lock_via_fails(uid)

    def bug_relock_then_act(self, uid: str) -> None:
        """Phase 1 (unlock contrast: latching fires, until stays clean), then a
        RE-lock and an action: now the until rule fires too."""
        self.flow_unlock_contrast(uid)
        self._lock_via_fails(uid)
        self._ev(uid, "review")
        self._ev(uid, "action")


FLOWS = {
    "login": ("Play: login, work, logout", "normal", "flow_login_work_logout"),
    "lock_review": ("Play: 3 fails, lock, review", "normal", "flow_lock_and_review"),
    "unlock_contrast": ("Play: lock, unlock, act (scope contrast)", "normal",
                        "flow_unlock_contrast"),
    "flagged": ("Play: flagged + reviewed", "normal", "flow_flagged_reviewed"),
    "action_no_login": ("Trigger: action without login", "bug", "bug_action_without_login"),
    "logout_no_login": ("Trigger: logout without login", "bug", "bug_logout_without_login"),
    "stale_token": ("Trigger: locked user acts (stale token)", "bug", "bug_stale_token"),
    "act_after_logout": ("Trigger: act after logout", "bug", "bug_act_after_logout"),
    "lock_no_fail": ("Trigger: lock with no failed attempt", "bug", "bug_lock_without_fail"),
    "lock_no_review": ("Trigger: lock, never review (timer)", "bug", "bug_lock_never_reviewed"),
    "relock_act": ("Trigger: re-lock then act (until)", "bug", "bug_relock_then_act"),
}
