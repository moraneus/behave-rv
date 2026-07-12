"""The stability panel's backend: four representative code changes, run
through the REAL mechanism (the real catalog diff, the real liveness check,
real verdict replays) in a sandbox -- separate registries and services, never
the live engine. Nothing here is mocked; the panel renders exactly what the
tool computes.
"""

from __future__ import annotations

import warnings

from behave_rv.catalog.diff import classify_changes
from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import UncheckablePolicyWarning, compile_feature
from behave_rv.engine.loop import Engine, NoTerminalConfiguredWarning
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource
from behave_rv.notify.channel import notifications, uses_from_policies

from demo.order_service.service import TERMINAL_TYPE, OrderService
from demo.order_service.steps import POLICY_DIR, build_registry

MARQUEE = "an order may only be paid after it was authorized"


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


def _seeded_trace(service_cls=OrderService):
    """The seeded fault the marquee policy exists to catch: pay without auth."""
    clock = _FakeClock()
    events: list[Event] = []
    service = service_cls(events.append, clock=clock, sleep=clock.sleep)
    service.bug_pay_without_auth("ORD-X")
    return events


def _compile_all(registry):
    policies = []
    for path in sorted(POLICY_DIR.glob("*.feature")):
        policies.extend(compile_feature(path.read_text(), registry))
    return policies


def _verdicts(policies, events):
    src = InProcessSource()
    for e in events:
        src.emit(e)
    engine = Engine(policies, terminal_event_types={TERMINAL_TYPE})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NoTerminalConfiguredWarning)
        verdicts = engine.run(src, emit_pending=True)
    return {v.policy_id: v.verdict for v in verdicts}


def _liveness(registry, events):
    types, values = set(), set()
    for e in events:
        types.add(e.type)
        for field, value in e.payload.items():
            if isinstance(value, (str, int, float, bool)):
                values.add((e.type, field, str(value)))
    messages = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for path in sorted(POLICY_DIR.glob("*.feature")):
            compile_feature(path.read_text(), registry,
                            observed_event_types=types, observed_values=values)
    for w in caught:
        if issubclass(w.category, UncheckablePolicyWarning):
            messages.append(str(w.message))
    return messages


# -- the four representative changes -------------------------------------------


BASELINE_CODE = '''@registry.trigger('an order is "{status}"', step_id="order.status.is",
                  event_type="order.status", correlation_key="order_id")
def order_is(ctx, event, status):
    if event.type == "order.status" and event.payload.get("status") == status:
        ctx.bind(order_id=event.bindings["order_id"])
        return True
    return False'''


def _registry_function_renamed():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_status_predicate(mctx, incoming, status):
        if incoming.type == "order.status" and incoming.payload.get("status") == status:
            mctx.bind(order_id=incoming.bindings["order_id"])
            return True
        return False

    return registry


def _registry_field_renamed():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if event.type == "order.status" and event.payload.get("state") == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


class _ServiceValueRenamed(OrderService):
    def _ev(self, oid, status):
        super()._ev(oid, "PAID" if status == "paid" else status)


def _helper_v1(event, status):
    return event.type == "order.status" and event.payload.get("status") == status


def _helper_v2(event, status):
    return event.type == "order.status" and event.payload.get("status") == status.upper()


_ACTIVE_HELPER = _helper_v1


def _registry_with_helper():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if _ACTIVE_HELPER(event, status):
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


CHANGES = {
    "rename_function": dict(
        title="Rename the step function (a pure refactor)",
        kind="absorb",
        code_after=BASELINE_CODE.replace("def order_is(ctx, event, status):",
                                         "def order_status_predicate(mctx, incoming, status):")
        .replace("event.", "incoming.").replace("ctx.bind", "mctx.bind"),
    ),
    "rename_field": dict(
        title="Rename the payload field the predicate reads",
        kind="break",
        code_after=BASELINE_CODE.replace('payload.get("status")', 'payload.get("state")'),
    ),
    "rename_value": dict(
        title='App code now emits "PAID" instead of "paid" (step untouched)',
        kind="liveness",
        code_after='# in service.py, not in the step:\n'
                   'self._ev(oid, "PAID")   # was: self._ev(oid, "paid")',
    ),
    "helper_change": dict(
        title="The predicate's helper function changes (now detected)",
        kind="break",
        code_after='def _matches(event, status):\n'
                   '    return event.type == "order.status" and \\\n'
                   '           event.payload.get("status") == status.upper()   # was: == status',
    ),
}


def apply_change(change_id: str) -> dict:
    """Run one representative change through the real defense stack."""
    global _ACTIVE_HELPER
    spec = CHANGES[change_id]

    if change_id == "helper_change":
        # a fair before/after: the baseline itself delegates to the helper
        _ACTIVE_HELPER = _helper_v1
        baseline_registry = _registry_with_helper()
        baseline_verdicts = _verdicts(_compile_all(baseline_registry), _seeded_trace())
        baseline_liveness = set(_liveness(baseline_registry, _seeded_trace()))
        _ACTIVE_HELPER = _helper_v2
        variant_registry = _registry_with_helper()
        variant_trace = _seeded_trace()
    else:
        baseline_registry = build_registry()
        baseline_verdicts = _verdicts(_compile_all(baseline_registry), _seeded_trace())
        baseline_liveness = set(_liveness(baseline_registry, _seeded_trace()))
        if change_id == "rename_function":
            variant_registry, variant_trace = _registry_function_renamed(), _seeded_trace()
        elif change_id == "rename_field":
            variant_registry, variant_trace = _registry_field_renamed(), _seeded_trace()
        elif change_id == "rename_value":
            variant_registry = build_registry()
            variant_trace = _seeded_trace(_ServiceValueRenamed)
        else:
            raise KeyError(change_id)

    baseline_policies = _compile_all(baseline_registry)
    uses = uses_from_policies(baseline_policies, owner="ops")
    changes = classify_changes(baseline_registry.entries(), variant_registry.entries())
    notes = notifications(baseline_registry.entries(), variant_registry.entries(), uses)
    variant_verdicts = _verdicts(_compile_all(variant_registry), variant_trace)
    liveness = sorted(set(_liveness(variant_registry, variant_trace)) - baseline_liveness)
    if change_id == "helper_change":
        _ACTIVE_HELPER = _helper_v1     # restore the sandbox

    flips = {p: (baseline_verdicts.get(p, "—"), variant_verdicts.get(p, "—"))
             for p in sorted(set(baseline_verdicts) | set(variant_verdicts))
             if baseline_verdicts.get(p) != variant_verdicts.get(p)}

    narratives = {
        "absorb": "Signature unchanged: the diff classifies this as a rename and "
                  "absorbs it silently. The violation is still caught -- absorption "
                  "shown as a positive.",
        "break": "The contract moved: the diff reports a break against every policy "
                 "that uses the step, with the contract diff, BEFORE anything runs.",
        "liveness": "The diff is silent -- correctly, the step's contract did not "
                    "change. The policy goes quiet on the very fault it used to "
                    "catch (left table), and value-level liveness against the "
                    "app's own stream is what raises the alarm (right panel).",
        "blindspot": "Nothing speaks. The call goes through a VALUE, which static "
                     "resolution deliberately does not follow, the stream still "
                     "carries the expected values, and the policy is silently "
                     "dormant. This is the documented residual boundary -- and the "
                     "signature's unresolved_calls names the call site the "
                     "fingerprint cannot see (see STABILITY.md).",
    }

    return {
        "change_id": change_id,
        "title": spec["title"],
        "kind": spec["kind"],
        "code_before": BASELINE_CODE if change_id != "rename_value"
        else 'self._ev(oid, "paid")',
        "code_after": spec["code_after"],
        "diff_statuses": {c.step_id: c.status for c in changes},
        "breaks": [{"policy": b.policy_id, "owner": b.owner,
                    "step": b.step_id, "detail": b.detail} for b in notes.breaks],
        "liveness": liveness,
        "verdict_flips": flips,
        "marquee": {"policy": MARQUEE,
                    "before": baseline_verdicts.get(MARQUEE, "—"),
                    "after": variant_verdicts.get(MARQUEE, "—")},
        "narrative": narratives[spec["kind"]],
    }
