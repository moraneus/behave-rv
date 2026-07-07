"""Verify every order-service policy against the mock's own flows,
deterministically, with no web UI involved. The service runs with a fake
clock and instant sleeps, so the traces here are exactly the actions the
live demo performs."""

from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource

from demo.order_service.service import TERMINAL_TYPE, OrderService
from demo.order_service.steps import build_registry, load_policies

PACE = 0.6


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


def run_flow(flow_name, oid="X", advance_past_timers=False):
    clock = FakeClock()
    events = []
    service = OrderService(events.append, clock=clock, sleep=clock.sleep, pace=PACE)
    getattr(service, flow_name)(oid)
    if advance_past_timers:
        events.append(Event("clock.tick", clock.now + 60.0, {}, {}, "test"))
    registry = build_registry()
    policies = load_policies(registry)
    src = InProcessSource()
    for e in events:
        src.emit(e)
    verdicts = Engine(policies, terminal_event_types={TERMINAL_TYPE}).run(
        src, emit_pending=True)
    return {(v.policy_id, v.entity_key["order_id"]): v for v in verdicts}


QUIET = ["an order is never double charged", "an order is never charged back"]

PAID_AFTER_AUTH = "an order may only be paid after it was authorized"
SHIP_AFTER_PAY = "a shipment may only follow payment"
REFUND_AFTER_CANCEL = "a refund requires a prior cancellation"
INVOICED = "every order is eventually invoiced"
DELIVERED = "every order is eventually delivered"
REFUND_WINDOW = "a cancelled order is refunded within the window"
PAY_WINDOW = "an authorized order is paid within the window"
CANCELLED_NEVER_SHIPS = "a cancelled order is never shipped"
SINCE_FLAGGED = "a flagged order is only reviewed afterwards"


def assert_no_unexpected_violations(vmap, expected=()):
    violated = {(p, e) for (p, e), v in vmap.items() if v.verdict == "violated"}
    assert violated == set(expected), f"unexpected violations: {violated}"


def test_full_lifecycle_is_clean_and_settles_everything():
    vmap = run_flow("flow_full_lifecycle")
    assert_no_unexpected_violations(vmap)
    for policy in (PAID_AFTER_AUTH, SHIP_AFTER_PAY, INVOICED, DELIVERED,
                   PAY_WINDOW, CANCELLED_NEVER_SHIPS, SINCE_FLAGGED, *QUIET):
        assert vmap[(policy, "X")].verdict == "satisfied", policy


def test_cancel_refund_flow_is_clean():
    vmap = run_flow("flow_cancel_refund")
    assert_no_unexpected_violations(vmap)
    assert vmap[(REFUND_WINDOW, "X")].verdict == "satisfied"
    assert vmap[(REFUND_AFTER_CANCEL, "X")].verdict == "satisfied"
    # long-pending onces legitimately stay pending (no terminal on this path)
    assert vmap[(INVOICED, "X")].verdict == "pending"
    assert vmap[(DELIVERED, "X")].verdict == "pending"


def test_flagged_reviewed_flow_is_clean():
    vmap = run_flow("flow_flagged_reviewed")
    assert_no_unexpected_violations(vmap)
    assert vmap[(SINCE_FLAGGED, "X")].verdict == "pending"   # holding, unterminated


def test_bug_pay_without_auth():
    vmap = run_flow("bug_pay_without_auth")
    assert_no_unexpected_violations(vmap, {(PAID_AFTER_AUTH, "X")})
    v = vmap[(PAID_AFTER_AUTH, "X")]
    assert [e.payload["status"] for e in v.deciding_events] == ["paid"]


def test_bug_ship_without_pay():
    vmap = run_flow("bug_ship_without_pay")
    assert_no_unexpected_violations(vmap, {(SHIP_AFTER_PAY, "X")})


def test_bug_refund_without_cancel():
    vmap = run_flow("bug_refund_without_cancel")
    assert_no_unexpected_violations(vmap, {(REFUND_AFTER_CANCEL, "X")})


def test_bug_cancel_never_refund_fires_the_timer():
    vmap = run_flow("bug_cancel_never_refund", advance_past_timers=True)
    assert_no_unexpected_violations(vmap, {(REFUND_WINDOW, "X")})
    v = vmap[(REFUND_WINDOW, "X")]
    assert v.at == 0.6 + 5.0                              # the deadline's event time
    assert [e.payload["status"] for e in v.deciding_events] == ["cancelled"]


def test_bug_double_charge():
    vmap = run_flow("bug_double_charge")
    assert_no_unexpected_violations(vmap, {(QUIET[0], "X")})
    v = vmap[(QUIET[0], "X")]
    assert [e.payload["status"] for e in v.deciding_events] == ["double_charged"]


def test_bug_ship_after_cancel():
    vmap = run_flow("bug_ship_after_cancel")
    assert_no_unexpected_violations(vmap, {(CANCELLED_NEVER_SHIPS, "X")})
    v = vmap[(CANCELLED_NEVER_SHIPS, "X")]
    assert [e.payload["status"] for e in v.deciding_events] == ["cancelled", "shipped"]


def test_bug_pay_after_flag():
    vmap = run_flow("bug_pay_after_flag")
    assert_no_unexpected_violations(vmap, {(SINCE_FLAGGED, "X")})
    v = vmap[(SINCE_FLAGGED, "X")]
    assert [e.payload["status"] for e in v.deciding_events] == ["fraud_flagged", "paid"]


def test_quiet_policies_never_violate_across_all_flows():
    from demo.order_service.service import FLOWS
    for action, (_, _, flow_name) in FLOWS.items():
        vmap = run_flow(flow_name, advance_past_timers=True)
        for quiet in QUIET:
            v = vmap.get((quiet, "X"))
            if action == "double_charge" and quiet == QUIET[0]:
                assert v.verdict == "violated"           # its own trigger, only
            elif v is not None:
                assert v.verdict != "violated", (action, quiet)
