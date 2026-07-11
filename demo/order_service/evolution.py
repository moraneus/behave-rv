"""Claim 2, demonstrated: the catalog survives the agent's refactoring.

The committed ``catalog.json`` next to this file is the behavioral interface
between the order service's code and the human's 11 policies. This module
plays five "the agent rewrote the service" scenarios against it and shows
what each mechanism does and -- just as important -- does not catch:

* Act 1  a pure refactor (function + wording renamed, contract identical)
         is absorbed silently and the policies produce identical verdicts.
* Act 2  a contract change (new event type and payload field) surfaces as
         Break notifications scoped to exactly the policies that used the step.
* Act 3  a renamed status VALUE ("paid" -> "charged") is invisible to the
         signature diff -- correctly, the contract is unchanged -- and is
         caught instead by value-level liveness against an observed stream.
* Act 4  a silently dropped emission (the invoice tap stops firing) is also
         signature-invisible and is caught by the same liveness check.
* Act 5  the other two channels: a new uncovered step is a Suggestion, an
         agent test changing what it asserts is a Weakening. Never blurred
         with Breaks.

Run the narrated version:  python -m demo.order_service.evolution
Run the assertions:        pytest demo/order_service/test_evolution.py
"""

from __future__ import annotations

import warnings
from pathlib import Path

from behave_rv.catalog.diff import classify_changes
from behave_rv.catalog.registry import StepRegistry
from behave_rv.catalog.store import load_catalog, save_catalog
from behave_rv.compile.compiler import UncheckablePolicyWarning, compile_feature
from behave_rv.engine.loop import Engine
from behave_rv.events.sources.inprocess import InProcessSource
from behave_rv.notify.channel import AgentTest, notifications, uses_from_policies

from demo.order_service.service import TERMINAL_TYPE, OrderService
from demo.order_service.steps import POLICY_DIR, build_registry, load_policies

CATALOG_PATH = Path(__file__).parent / "catalog.json"
OWNER = "ops@example.com"


# -- shared plumbing ---------------------------------------------------------


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


def committed_catalog():
    return load_catalog(CATALOG_PATH)


def write_catalog():
    """Regenerate catalog.json from the current steps.py (the commit gate)."""
    save_catalog(CATALOG_PATH, build_registry().entries())


def policy_uses(policies):
    """Which step_ids each policy binds: the compiler records the resolved
    step_ids on each Policy (used_step_ids), so this is the real dependency
    map, not an event-type heuristic."""
    return uses_from_policies(policies, owner=OWNER)


def record_trace(service_cls, flow_names):
    """Replay flows through a service variant and return the emitted events."""
    clock = _FakeClock()
    events = []
    service = service_cls(events.append, clock=clock, sleep=clock.sleep)
    for i, flow in enumerate(flow_names):
        getattr(service, flow)(f"O{i + 1}")
    return events


def run_verdicts(policies, events):
    src = InProcessSource()
    for e in events:
        src.emit(e)
    verdicts = Engine(policies, terminal_event_types={TERMINAL_TYPE}).run(
        src, emit_pending=True)
    return {(v.policy_id, v.entity_key["order_id"]): v.verdict for v in verdicts}


def harvest_observed_values(events):
    """What a replay actually saw: the engine's (type, field, value) triples."""
    src = InProcessSource()
    for e in events:
        src.emit(e)
    engine = Engine(load_policies(build_registry()),
                    terminal_event_types={TERMINAL_TYPE})
    engine.run(src, emit_pending=True)
    return set(engine.observed_values)


def liveness_warnings(observed_values):
    """Compile every policy against an observed-value harvest; return the
    warning texts. This is the value-level liveness check."""
    warned = set()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for path in sorted(POLICY_DIR.glob("*.feature")):
            compile_feature(path.read_text(), build_registry(),
                            observed_values=observed_values)
    for w in caught:
        if issubclass(w.category, UncheckablePolicyWarning):
            warned.add(str(w.message))
    return warned


NORMAL_FLOWS = ["flow_full_lifecycle", "flow_cancel_refund", "flow_flagged_reviewed"]


# -- Act 1: the pure refactor, absorbed silently -----------------------------


def build_registry_v2():
    """The agent's refactor: the step function is renamed, its internals are
    renamed, and the primary wording is changed (the old wording stays as an
    alias). Same step_id, same behavioral contract."""
    registry = StepRegistry()

    @registry.trigger('the order status becomes "{status}"',
                      step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_status_matches(monitor_ctx, incoming, status):
        if incoming.type == "order.status" and incoming.payload.get("status") == status:
            monitor_ctx.bind(order_id=incoming.bindings["order_id"])
            return True
        return False

    registry.alias("order.status.is", 'an order is "{status}"')
    return registry


# -- Act 2: the contract change, surfaced and scoped --------------------------


def build_registry_v3():
    """The agent moves order state onto a new event contract: event type
    ``order.state`` with payload field ``state``. Same step_id -- this is the
    same logical step whose CONTRACT changed, which must surface as a Break."""
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.state", correlation_key="order_id")
    def order_is(ctx, event, status):
        if event.type == "order.state" and event.payload.get("state") == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


# -- Act 3: the status-value rename the signature cannot see ------------------


class OrderServiceV4(OrderService):
    """The agent renames the status value: orders are now "charged", never
    "paid". The step's contract is untouched, so the diff stays silent."""

    def _ev(self, oid, status):
        super()._ev(oid, "charged" if status == "paid" else status)


# -- Act 4: the dropped emission the signature cannot see ---------------------


class OrderServiceV5(OrderService):
    """The agent's refactor accidentally drops the invoice tap: the billing
    step still exists in the catalog, but nothing ever emits "invoiced"."""

    def _ev(self, oid, status):
        if status == "invoiced":
            return  # the tap is gone; the event never reaches the stream
        super()._ev(oid, status)


# -- Act 5: the other two channels --------------------------------------------


def build_registry_v6():
    """v1 plus a new tap no policy covers yet: returns processing."""
    registry = build_registry()

    @registry.trigger('an order return is recorded', step_id="order.return.recorded",
                      event_type="order.return", correlation_key="order_id")
    def order_returned(ctx, event):
        if event.type == "order.return":
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


OLD_AGENT_TESTS = [AgentTest(test_id="refund-latency", owner="agent",
                             asserts="a refund lands within 5s of cancellation")]
NEW_AGENT_TESTS = [AgentTest(test_id="refund-latency", owner="agent",
                             asserts="a refund lands within 30s of cancellation")]


# -- the narrated runner -------------------------------------------------------


def _statuses(changes):
    return {c.step_id: c.status for c in changes}


def main():
    committed = committed_catalog()
    uses = policy_uses(load_policies(build_registry()))
    print("=" * 72)
    print("Act 0  the committed contract")
    print(f"  {CATALOG_PATH.name}: {len(committed)} step(s), "
          f"bound by {len(uses)} human policies")
    sync = classify_changes(committed, build_registry().entries())
    print(f"  code vs committed catalog: {_statuses(sync)}")

    print("=" * 72)
    print("Act 1  pure refactor: function renamed, internals renamed, wording changed")
    v2 = build_registry_v2()
    changes = classify_changes(committed, v2.entries())
    notes = notifications(committed, v2.entries(), uses)
    print(f"  diff: {_statuses(changes)}")
    print(f"  notifications: {len(notes.breaks)} breaks, "
          f"{len(notes.suggestions)} suggestions  (absorbed silently)")
    trace = record_trace(OrderService, NORMAL_FLOWS + ["bug_pay_without_auth"])
    before = run_verdicts(load_policies(build_registry()), trace)
    after = run_verdicts(load_policies(v2), trace)
    print(f"  same trace, v1 vs v2 policies: verdicts identical = {before == after} "
          f"({len(after)} verdicts)")

    print("=" * 72)
    print("Act 2  contract change: event type and payload field move")
    v3 = build_registry_v3()
    changes = classify_changes(committed, v3.entries())
    notes = notifications(committed, v3.entries(), uses)
    print(f"  diff: {_statuses(changes)}")
    print(f"  BREAKS ({len(notes.breaks)}), scoped to the policies that used the step:")
    for b in notes.breaks:
        print(f"    ✗ {b.policy_id}  [{b.owner}]")
    if notes.breaks:
        print(f"  contract diff: {notes.breaks[0].detail}")

    print("=" * 72)
    print('Act 3  status value renamed in the code: "paid" -> "charged"')
    v4_entries = build_registry().entries()   # the registry is untouched by v4
    notes = notifications(committed, v4_entries, uses)
    print(f"  signature diff: {_statuses(classify_changes(committed, v4_entries))}"
          f" -> {len(notes.breaks)} breaks (the diff CANNOT see this)")
    baseline = liveness_warnings(harvest_observed_values(
        record_trace(OrderService, NORMAL_FLOWS)))
    v4_warned = liveness_warnings(harvest_observed_values(
        record_trace(OrderServiceV4, NORMAL_FLOWS)))
    for w in sorted(v4_warned - baseline):
        print(f"  liveness catch: {w}")

    print("=" * 72)
    print("Act 4  the invoice tap silently stops firing")
    v5_warned = liveness_warnings(harvest_observed_values(
        record_trace(OrderServiceV5, NORMAL_FLOWS)))
    print("  signature diff: 0 breaks (nothing changed in the catalog)")
    for w in sorted(v5_warned - baseline):
        print(f"  liveness catch: {w}")

    print("=" * 72)
    print("Act 5  the other channels stay separate")
    v6 = build_registry_v6()
    notes = notifications(committed, v6.entries(), uses,
                          old_tests=OLD_AGENT_TESTS, new_tests=NEW_AGENT_TESTS)
    for s in notes.suggestions:
        print(f"  suggestion: {s.step_id} ({s.phrasing!r}) -- {s.detail}")
    for w in notes.weakenings:
        print(f"  weakening:  {w.test_id} [{w.owner}] -- {w.detail}")
    print(f"  breaks on this diff: {len(notes.breaks)} (a proposal is never a warning)")


if __name__ == "__main__":
    main()
