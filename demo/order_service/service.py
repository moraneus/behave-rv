"""The mock order service: real domain flows that emit behave_rv events.

The clock and the sleep are injectable so the SAME flows run in two worlds:
live in the web app (wall clock, real sleeps) and deterministically in
test_policies.py (a fake clock, instant sleeps). The verification tests
therefore replay exactly the actions the demo performs.
"""

from __future__ import annotations

import time

from behave_rv.events.event import Event

EVENT_TYPE = "order.status"
TERMINAL_TYPE = "order.done"


class OrderService:
    def __init__(self, emit, clock=time.time, sleep=time.sleep, pace=0.6):
        self._emit = emit
        self._clock = clock
        self._sleep = sleep
        self._pace = pace

    def _ev(self, oid: str, status: str) -> None:
        self._emit(Event(EVENT_TYPE, self._clock(), {"order_id": oid},
                         {"status": status}, "order-service"))
        self._sleep(self._pace)

    def _done(self, oid: str) -> None:
        self._emit(Event(TERMINAL_TYPE, self._clock(), {"order_id": oid},
                         {}, "order-service"))

    # -- manual actions (driven by the board UI, one event per user click) ----

    def act(self, oid: str, status: str) -> None:
        """Emit a single user-caused status event, no pacing. The board never
        blocks an action; whether it was legal is the monitor's call."""
        self._emit(Event(EVENT_TYPE, self._clock(), {"order_id": oid},
                         {"status": status}, "order-service"))

    def close(self, oid: str) -> None:
        """Emit the terminal event: the order's story ends here, so pending
        'eventually' obligations settle now, satisfied or violated."""
        self._done(oid)

    # -- normal flows ---------------------------------------------------------

    def flow_full_lifecycle(self, oid: str) -> None:
        """create -> authorize -> pay -> invoice -> ship -> deliver -> done."""
        for s in ("created", "authorized", "paid", "invoiced", "shipped", "delivered"):
            self._ev(oid, s)
        self._done(oid)

    def flow_cancel_refund(self, oid: str) -> None:
        """A customer cancels immediately; the refund lands inside the window."""
        for s in ("created", "cancelled", "refunded"):
            self._ev(oid, s)

    def flow_flagged_reviewed(self, oid: str) -> None:
        """A fraud-flagged order is frozen and reviewed, nothing else."""
        for s in ("created", "fraud_flagged", "reviewed"):
            self._ev(oid, s)

    # -- buggy flows (each is one demonstrable violation) ----------------------

    def bug_pay_without_auth(self, oid: str) -> None:
        for s in ("created", "paid"):
            self._ev(oid, s)

    def bug_ship_without_pay(self, oid: str) -> None:
        for s in ("created", "shipped"):
            self._ev(oid, s)

    def bug_refund_without_cancel(self, oid: str) -> None:
        for s in ("created", "refunded"):
            self._ev(oid, s)

    def bug_cancel_never_refund(self, oid: str) -> None:
        """The marquee live-timer bug: cancelled, then silence. The refund
        window is violated by the wall clock while the stream is idle."""
        for s in ("created", "cancelled"):
            self._ev(oid, s)

    def bug_double_charge(self, oid: str) -> None:
        for s in ("created", "authorized", "paid", "invoiced", "double_charged"):
            self._ev(oid, s)

    def bug_ship_after_cancel(self, oid: str) -> None:
        """Paid first so the ship-after-pay rule stays satisfied: the one
        intended violation is the cancelled-scope shipping."""
        for s in ("created", "authorized", "paid", "invoiced",
                  "cancelled", "refunded", "shipped"):
            self._ev(oid, s)

    def bug_pay_after_flag(self, oid: str) -> None:
        """Authorized first so pay-after-auth stays satisfied: the one intended
        violation is the since rule (a flagged order progressed unreviewed)."""
        for s in ("created", "authorized", "fraud_flagged", "paid"):
            self._ev(oid, s)


#: action id -> (button label, "normal"|"bug", flow method name)
FLOWS = {
    "full": ("Play: full lifecycle", "normal", "flow_full_lifecycle"),
    "cancel_refund": ("Play: cancel + refund in window", "normal", "flow_cancel_refund"),
    "flagged": ("Play: flagged + reviewed", "normal", "flow_flagged_reviewed"),
    "pay_no_auth": ("Trigger: pay without auth", "bug", "bug_pay_without_auth"),
    "ship_no_pay": ("Trigger: ship without pay", "bug", "bug_ship_without_pay"),
    "refund_no_cancel": ("Trigger: refund without cancel", "bug", "bug_refund_without_cancel"),
    "cancel_no_refund": ("Trigger: cancel, never refund (timer)", "bug", "bug_cancel_never_refund"),
    "double_charge": ("Trigger: double charge", "bug", "bug_double_charge"),
    "ship_after_cancel": ("Trigger: ship a cancelled order", "bug", "bug_ship_after_cancel"),
    "pay_after_flag": ("Trigger: pay a flagged order", "bug", "bug_pay_after_flag"),
}
