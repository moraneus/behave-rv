"""The code-change catalog: specification stability, measured with ground truth.

Twenty-two realistic code changes, each applied one at a time against a fixed
baseline (the order demo's real service, step, and 11 policies), each with
ground truth declared in advance. For every case the harness:

1. verifies the ground truth empirically -- the SAME seeded-fault trace is
   replayed through the baseline and the changed version and the verdict sets
   are compared ("did the policy's real behavior change?");
2. runs the full defense stack -- catalog diff + scoped notifications, and
   compile-time liveness against the representative (post-change) stream;
3. classifies the outcome: CORRECT (caught a disconnect, or stayed silent on
   an absorb), FALSE ALARM (spoke on an unchanged behavior; acceptable by
   design in family D, counted), or MISS (a disconnect nobody caught).

Design decision: variants are in-memory step/service definitions, not git
mutations -- the same isolation, perfectly reproducible, nothing to restore.

Run the table:  python -m tests.stability_catalog
Asserted under pytest in tests/test_stability_catalog.py.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Callable, Optional

from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import (
    CompileError,
    UncheckablePolicyWarning,
    compile_feature,
)
from behave_rv.engine.loop import Engine, NoTerminalConfiguredWarning
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource
from behave_rv.notify.channel import notifications, uses_from_policies

from demo.order_service.service import TERMINAL_TYPE, OrderService
from demo.order_service.steps import POLICY_DIR, build_registry

# ---------------------------------------------------------------------------
# the fixed baseline: the order demo's service, step, and 11 policies


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


SEEDED_FLOWS = [
    "flow_full_lifecycle", "flow_cancel_refund", "flow_flagged_reviewed",
    "bug_pay_without_auth", "bug_ship_without_pay", "bug_refund_without_cancel",
    "bug_cancel_never_refund", "bug_double_charge", "bug_ship_after_cancel",
    "bug_pay_after_flag",
]


def record_trace(service_cls=OrderService) -> list[Event]:
    clock = _FakeClock()
    events: list[Event] = []
    service = service_cls(events.append, clock=clock, sleep=clock.sleep)
    for i, flow in enumerate(SEEDED_FLOWS):
        getattr(service, flow)(f"O{i + 1}")
    events.append(Event("clock.tick", clock.now + 60.0, {}, {}, "catalog"))
    return events


def policy_feature_texts(extra_features: str = "") -> list[str]:
    texts = [p.read_text() for p in sorted(POLICY_DIR.glob("*.feature"))]
    if extra_features:
        texts.append(extra_features)
    return texts


def compile_policies(registry, extra_features: str = ""):
    policies = []
    for text in policy_feature_texts(extra_features):
        policies.extend(compile_feature(text, registry))
    return policies


def run_verdicts(policies, events) -> set:
    src = InProcessSource()
    for e in events:
        src.emit(e)
    engine = Engine(policies, terminal_event_types={TERMINAL_TYPE})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NoTerminalConfiguredWarning)
        verdicts = engine.run(src, emit_pending=True)
    return {(v.policy_id, v.entity_key["order_id"], v.verdict) for v in verdicts}


def harvest(events) -> tuple[set, set]:
    """(observed_event_types, observed_values) from a representative stream."""
    src = InProcessSource()
    for e in events:
        src.emit(e)
    engine = Engine(compile_policies(build_registry()),
                    terminal_event_types={TERMINAL_TYPE})
    engine.run(src, emit_pending=True)
    return set(engine.observed_types), set(engine.observed_values)


def liveness_messages(registry, observed_types, observed_values,
                      extra_features: str = "") -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for text in policy_feature_texts(extra_features):
            try:
                compile_feature(text, registry,
                                observed_event_types=observed_types,
                                observed_values=observed_values)
            except CompileError:
                pass  # resolution refusal is its own defense; recorded separately
    return [str(w.message) for w in caught
            if issubclass(w.category, UncheckablePolicyWarning)]


# ---------------------------------------------------------------------------
# variant step registries (the "agent edited the step" families A, B, D)


def _register_baseline_shape(registry, *, phrasing='an order is "{status}"',
                             alias_old: bool = False):
    """The baseline predicate, re-registered under a possibly new wording."""

    @registry.trigger(phrasing, step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if event.type == "order.status" and event.payload.get("status") == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    if alias_old:
        registry.alias("order.status.is", 'an order is "{status}"')
    return registry


def registry_a1_function_renamed():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_status_predicate(ctx, event, status):
        if event.type == "order.status" and event.payload.get("status") == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def registry_a2_variable_renamed():
    # ctx/event are passed positionally, so they are representational; the
    # third parameter binds the {status} placeholder BY NAME and must keep it
    # (renaming it is case B7, a break, not an absorb)
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(match_ctx, incoming, status):
        if incoming.type == "order.status" and incoming.payload.get("status") == status:
            match_ctx.bind(order_id=incoming.bindings["order_id"])
            return True
        return False

    return registry


def registry_a3_rephrased_with_alias():
    return _register_baseline_shape(StepRegistry(),
                                    phrasing='the order reaches "{status}"',
                                    alias_old=True)


def registry_a4_reformatted():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if (
            event.type == "order.status"
            and event.payload.get("status") == status
        ):
            ctx.bind(
                order_id=event.bindings["order_id"],
            )
            return True
        return False

    return registry


def registry_b1_payload_field_renamed():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if event.type == "order.status" and event.payload.get("state") == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def registry_b2_event_type_changed():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.lifecycle", correlation_key="order_id")
    def order_is(ctx, event, status):
        if event.type == "order.lifecycle" and event.payload.get("status") == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def registry_b3_correlation_key_changed():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status",
                      correlation_key=("order_id", "tenant_id"))
    def order_is(ctx, event, status):
        if event.type == "order.status" and event.payload.get("status") == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def registry_b4_guard_tightened():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if event.type == "order.status" and event.payload.get("status") == status \
                and event.payload.get("channel") == "web":
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def registry_b5_step_deleted():
    return StepRegistry()


def registry_b7_binding_parameter_renamed():
    """The placeholder-bound parameter is renamed while the phrasing keeps
    {status}: the compiler's call-by-name (func(**{"status": ...})) now
    raises, is contained as no-match, and the policy goes dormant. Found by
    this catalog's first run: the alpha-normalized fingerprint erased
    parameter names, making this a silent break until the fingerprint fix."""
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, wanted):
        if event.type == "order.status" and event.payload.get("status") == wanted:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


# B6: two steps share one event type; only the changed step's policy may notify
B6_EXTRA_POLICY = """
Feature: amounts
  Scenario: no oversized order
    Then an order amount above "1000" is recorded never happens
"""


def _register_amount_step(registry, *, amount_field: str):
    if amount_field == "amount":
        @registry.trigger('an order amount above "{limit}" is recorded',
                          step_id="order.amount.exceeds",
                          event_type="order.status", correlation_key="order_id")
        def order_amount(ctx, event, limit):
            if event.type == "order.status" and \
                    float(event.payload.get("amount", 0)) > float(limit):
                ctx.bind(order_id=event.bindings["order_id"])
                return True
            return False
    else:
        @registry.trigger('an order amount above "{limit}" is recorded',
                          step_id="order.amount.exceeds",
                          event_type="order.status", correlation_key="order_id")
        def order_amount(ctx, event, limit):
            if event.type == "order.status" and \
                    float(event.payload.get("total", 0)) > float(limit):
                ctx.bind(order_id=event.bindings["order_id"])
                return True
            return False
    return registry


def registry_b6_baseline():
    return _register_amount_step(build_registry(), amount_field="amount")


def registry_b6_variant():
    return _register_amount_step(build_registry(), amount_field="total")


# C4: the predicate delegates to a helper whose condition changes.
# The step body is IDENTICAL in both versions; only the helper differs.

def _helper_matches_v1(event, status):
    return event.type == "order.status" and event.payload.get("status") == status


def _helper_matches_v2(event, status):
    return event.type == "order.status" and event.payload.get("status") == status.upper()


def _registry_with_helper(helper):
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if _MATCH_HELPER(event, status):
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def registry_c4_baseline():
    global _MATCH_HELPER
    _MATCH_HELPER = _helper_matches_v1
    return _registry_with_helper(_helper_matches_v1)


def registry_c4_variant():
    global _MATCH_HELPER
    _MATCH_HELPER = _helper_matches_v2
    return _registry_with_helper(_helper_matches_v2)


# A5: rename a helper (call site updated), body identical -- the hash covers
# body identities, not names, so this must absorb.

def _a5_helper_original(event, status):
    return event.type == "order.status" and event.payload.get("status") == status


def _a5_helper_renamed(event, status):
    return event.type == "order.status" and event.payload.get("status") == status


def registry_a5_baseline():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if _a5_helper_original(event, status):
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def registry_a5_variant():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if _a5_helper_renamed(event, status):
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


# A6: reorder two helper DEFINITIONS in the module (no call or body changes);
# the predicates live in two fixture modules differing only in definition order.

def _a6_registry(module):
    registry = StepRegistry()
    registry.register("trigger", 'an order is "{status}"', module.predicate,
                      step_id="order.status.is", event_type="order.status",
                      correlation_key="order_id")
    return registry


def registry_a6_baseline():
    from tests import _a6_order_one
    return _a6_registry(_a6_order_one)


def registry_a6_variant():
    from tests import _a6_order_two
    return _a6_registry(_a6_order_two)


# C4b: the helper change hides behind an UNRESOLVABLE call (the helper is
# passed as a default-argument VALUE); static resolution deliberately stops
# here, and the signature's unresolved_calls makes the weaker protection
# visible. The new documented boundary after the C4 fix.

def _c4b_check_v1(event, status):
    return event.type == "order.status" and event.payload.get("status") == status


def _c4b_check_v2(event, status):
    return event.type == "order.status" and event.payload.get("status") == status.upper()


def _c4b_registry(check):
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status, _check=check):
        if _check(event, status):
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def registry_c4b_baseline():
    return _c4b_registry(_c4b_check_v1)


def registry_c4b_variant():
    return _c4b_registry(_c4b_check_v2)


# D4: split one helper into two, behavior preserved -- the reachable set
# changes, so this alarms conservatively, alongside D1-D3.

def _d4_combined(event, status):
    return event.type == "order.status" and event.payload.get("status") == status


def _d4_type_ok(event):
    return event.type == "order.status"


def _d4_status_ok(event, status):
    return event.payload.get("status") == status


def _d4_split(event, status):
    return _d4_type_ok(event) and _d4_status_ok(event, status)


_D4_ACTIVE = _d4_combined


def _d4_registry():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if _D4_ACTIVE(event, status):
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def registry_d4_baseline():
    global _D4_ACTIVE
    _D4_ACTIVE = _d4_combined
    return _d4_registry()


def registry_d4_variant():
    global _D4_ACTIVE
    _D4_ACTIVE = _d4_split
    return _d4_registry()


def registry_d1_temp_variable():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        current = event.payload.get("status")
        if event.type == "order.status" and current == status:
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def registry_d2_commuted_operands():
    registry = StepRegistry()

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if event.payload.get("status") == status and event.type == "order.status":
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


def registry_d3_extracted_helper():
    registry = StepRegistry()

    def _is_order_status(event, status):
        return event.type == "order.status" and event.payload.get("status") == status

    @registry.trigger('an order is "{status}"', step_id="order.status.is",
                      event_type="order.status", correlation_key="order_id")
    def order_is(ctx, event, status):
        if _is_order_status(event, status):
            ctx.bind(order_id=event.bindings["order_id"])
            return True
        return False

    return registry


# ---------------------------------------------------------------------------
# variant application services (the "app changed under the step" family C)


class ServiceC1ValueRenamed(OrderService):
    """The app now emits "PAID" where it used to emit "paid"."""

    def _ev(self, oid, status):
        super()._ev(oid, "PAID" if status == "paid" else status)


class ServiceC2EventTypeRenamed(OrderService):
    """The app now emits order.lifecycle events instead of order.status."""

    def _ev(self, oid, status):
        self._emit(Event("order.lifecycle", self._clock(), {"order_id": oid},
                         {"status": status}, "order-service"))
        self._sleep(self._pace)


class ServiceC3FieldRenamed(OrderService):
    """The app now carries the value under payload field "state"."""

    def _ev(self, oid, status):
        self._emit(Event("order.status", self._clock(), {"order_id": oid},
                         {"state": status}, "order-service"))
        self._sleep(self._pace)


# ---------------------------------------------------------------------------
# the catalog itself


@dataclass
class Case:
    case_id: str
    family: str
    description: str
    # ground truth, declared in advance
    behavior_should_change: bool
    expected_defense: str            # "silent" | "diff" | "liveness" | "none (documented miss)"
    # the change
    variant_registry: Optional[Callable] = None     # step-side change
    variant_service: Optional[type] = None          # app-side change
    baseline_registry: Callable = build_registry    # overridden for B6/C4 pairs
    extra_policy: str = ""                          # B6 adds a second policy
    extra_events: list = field(default_factory=list)  # B6 exercises its step


@dataclass
class Outcome:
    case: Case
    behavior_changed: bool
    diff_breaks: list
    diff_status: str
    liveness: list[str]
    compile_refused: bool
    classification: str = ""
    notes: str = ""


CASES = [
    Case("A1", "absorb", "rename the step function",
         False, "silent", variant_registry=registry_a1_function_renamed),
    Case("A2", "absorb", "rename internal variables in the predicate",
         False, "silent", variant_registry=registry_a2_variable_renamed),
    Case("A3", "absorb", "change the phrasing, old wording retained as alias",
         False, "silent", variant_registry=registry_a3_rephrased_with_alias),
    Case("A4", "absorb", "reformat the predicate body",
         False, "silent", variant_registry=registry_a4_reformatted),
    Case("B1", "break", "rename the payload field the predicate reads",
         True, "diff", variant_registry=registry_b1_payload_field_renamed),
    Case("B2", "break", "change the declared event type",
         True, "diff", variant_registry=registry_b2_event_type_changed),
    Case("B3", "break", "change the correlation key",
         True, "diff", variant_registry=registry_b3_correlation_key_changed),
    Case("B4", "break", "tighten the guard inside the predicate body",
         True, "diff", variant_registry=registry_b4_guard_tightened),
    Case("B5", "break", "delete the step entirely",
         True, "diff", variant_registry=registry_b5_step_deleted),
    Case("B6", "break", "two steps share an event type; change one; scope check",
         True, "diff", variant_registry=registry_b6_variant,
         baseline_registry=registry_b6_baseline, extra_policy=B6_EXTRA_POLICY,
         extra_events=[Event("order.status", 500.0, {"order_id": "OB6"},
                             {"status": "created", "amount": "1500"}, "catalog")]),
    Case("B7", "break", "rename the placeholder-bound parameter (phrasing kept)",
         True, "diff", variant_registry=registry_b7_binding_parameter_renamed),
    Case("C1", "disconnect", 'app emits "PAID" instead of "paid", step untouched',
         True, "liveness", variant_service=ServiceC1ValueRenamed),
    Case("C2", "disconnect", "app emits a different event type, step untouched",
         True, "liveness", variant_service=ServiceC2EventTypeRenamed),
    Case("C3", "disconnect", "app carries the value under a different field name",
         True, "liveness", variant_service=ServiceC3FieldRenamed),
    Case("C4", "disconnect", "predicate delegates to a helper; the helper changes",
         True, "diff", variant_registry=registry_c4_variant,
         baseline_registry=registry_c4_baseline),
    Case("A5", "absorb", "rename a helper (call site updated), body identical",
         False, "silent", variant_registry=registry_a5_variant,
         baseline_registry=registry_a5_baseline),
    Case("A6", "absorb", "reorder two helper definitions, no call/body changes",
         False, "silent", variant_registry=registry_a6_variant,
         baseline_registry=registry_a6_baseline),
    Case("C4b", "disconnect", "helper change behind an unresolvable (value) call",
         True, "none (documented miss)", variant_registry=registry_c4b_variant,
         baseline_registry=registry_c4b_baseline),
    Case("D4", "conservative", "split one helper into two, behavior preserved",
         False, "silent", variant_registry=registry_d4_variant,
         baseline_registry=registry_d4_baseline),
    Case("D1", "conservative", "introduce a temporary variable in the predicate",
         False, "silent", variant_registry=registry_d1_temp_variable),
    Case("D2", "conservative", "reorder commutative boolean operands",
         False, "silent", variant_registry=registry_d2_commuted_operands),
    Case("D3", "conservative", "extract unchanged logic into a helper",
         False, "silent", variant_registry=registry_d3_extracted_helper),
]


def run_case(case: Case) -> Outcome:
    baseline_reg = case.baseline_registry()
    baseline_trace = record_trace() + case.extra_events
    baseline_policies = compile_policies(baseline_reg, case.extra_policy)
    baseline_verdicts = run_verdicts(baseline_policies, baseline_trace)
    uses = uses_from_policies(baseline_policies, owner="ops")

    # liveness warnings that already exist at BASELINE (e.g. the quiet
    # chargeback policy's never-observed value) are not this change speaking;
    # the defense's voice is the DELTA
    base_types, base_values = harvest(baseline_trace)
    baseline_liveness = set(liveness_messages(baseline_reg, base_types,
                                              base_values, case.extra_policy))

    compile_refused = False
    if case.variant_service is not None:
        # the APP changed: same registry and policies, different stream
        variant_trace = record_trace(case.variant_service) + case.extra_events
        variant_verdicts = run_verdicts(baseline_policies, variant_trace)
        variant_reg = case.baseline_registry()
        observed_types, observed_values = harvest(variant_trace)
    else:
        # the STEP changed: same stream, recompiled policies
        variant_reg = case.variant_registry()
        observed_types, observed_values = base_types, base_values
        try:
            variant_policies = compile_policies(variant_reg, case.extra_policy)
            variant_verdicts = run_verdicts(variant_policies, baseline_trace)
        except CompileError:
            variant_verdicts = set()          # the policies are gone
            compile_refused = True

    from behave_rv.catalog.diff import classify_changes
    changes = classify_changes(baseline_reg.entries(), variant_reg.entries())
    statuses = sorted({c.status for c in changes})
    notes_obj = notifications(baseline_reg.entries(), variant_reg.entries(), uses)
    liveness = sorted(set(liveness_messages(variant_reg, observed_types,
                                            observed_values, case.extra_policy))
                      - baseline_liveness)

    outcome = Outcome(
        case=case,
        behavior_changed=(variant_verdicts != baseline_verdicts),
        diff_breaks=notes_obj.breaks,
        diff_status=",".join(statuses),
        liveness=liveness,
        compile_refused=compile_refused,
    )
    _classify(outcome)
    return outcome


def _classify(outcome: Outcome) -> None:
    case = outcome.case
    spoke_diff = bool(outcome.diff_breaks)
    spoke_liveness = bool(outcome.liveness)
    spoke = spoke_diff or spoke_liveness or outcome.compile_refused

    if not case.behavior_should_change:
        outcome.classification = "FALSE ALARM" if spoke else "CORRECT (silent)"
    else:
        if case.expected_defense == "diff":
            outcome.classification = "CORRECT (diff)" if spoke_diff else "MISS"
        elif case.expected_defense == "liveness":
            outcome.classification = "CORRECT (liveness)" if spoke_liveness else "MISS"
        else:  # the documented C4 boundary
            outcome.classification = ("MISS (documented)" if not spoke
                                      else "PARTIAL (unexpectedly caught)")
    # sanity: the declared ground truth must match the replayed one
    if outcome.behavior_changed != case.behavior_should_change:
        outcome.notes = (f"GROUND TRUTH MISMATCH: declared "
                         f"{case.behavior_should_change}, measured "
                         f"{outcome.behavior_changed}")


def run_catalog() -> list[Outcome]:
    return [run_case(case) for case in CASES]


# ---------------------------------------------------------------------------
# the raw-definition baseline: what a naive comparison tool has WITHOUT stable
# identities or structural normalization -- every registered phrasing (primary
# and alias) mapped to its dispatch metadata and EXACT predicate source text.
# It flags when any baseline phrasing's definition changed or vanished (new
# phrasings are additions, not changes). This is the comparison row for the
# published Table: it quantifies what the identities + signatures buy.


def raw_definition_record(registry) -> dict:
    import inspect
    record = {}
    for entry in registry.entries():
        try:
            source = inspect.getsource(registry._funcs[entry.step_id])
        except (OSError, TypeError):
            source = "<unavailable>"
        for phrasing in (entry.phrasing, *registry._aliases.get(entry.step_id, [])):
            record[phrasing] = (entry.signature.event_type,
                                tuple(entry.signature.correlation_key), source)
    return record


def raw_diff_flags(case: Case) -> bool:
    baseline = raw_definition_record(case.baseline_registry())
    variant_registry = (case.variant_registry() if case.variant_registry
                        else case.baseline_registry())
    variant = raw_definition_record(variant_registry)
    return any(variant.get(phrase) != rec for phrase, rec in baseline.items())


def raw_baseline_row(outcomes: list[Outcome]) -> dict:
    """(detected, missed, silent, false alarms) for the raw baseline over the
    same 22 cases and the same replayed ground truth."""
    row = {"detected": 0, "missed": 0, "silent": 0, "false_alarms": 0}
    for outcome in outcomes:
        flagged = raw_diff_flags(outcome.case)
        if outcome.behavior_changed:
            row["detected" if flagged else "missed"] += 1
        else:
            row["false_alarms" if flagged else "silent"] += 1
    return row


def behave_rv_row(outcomes: list[Outcome]) -> dict:
    """The same four columns for the full tool (diff + liveness paths)."""
    row = {"detected": 0, "missed": 0, "silent": 0, "false_alarms": 0}
    for o in outcomes:
        caught = bool(o.diff_breaks) or bool(o.liveness) or o.compile_refused
        if o.behavior_changed:
            row["detected" if caught else "missed"] += 1
        else:
            row["false_alarms" if caught else "silent"] += 1
    return row


def render_table(outcomes: list[Outcome]) -> str:
    lines = [
        f"{'case':4} {'behavior?':10} {'diff':22} {'liveness':9} {'classification':26} description",
        "-" * 110,
    ]
    for o in outcomes:
        diff = f"{o.diff_status}({len(o.diff_breaks)} brk)"
        if o.compile_refused:
            diff += "+refusal"
        lines.append(
            f"{o.case.case_id:4} {str(o.behavior_changed):10} {diff:22} "
            f"{len(o.liveness):<9} {o.classification:26} {o.case.description}"
            + (f"   [{o.notes}]" if o.notes else "")
        )
    false_alarms = sum(1 for o in outcomes if o.classification == "FALSE ALARM")
    lines.append("-" * 110)
    lines.append(f"false alarms in conservative probes: {false_alarms}/"
                 f"{sum(1 for o in outcomes if o.case.family == 'conservative')}")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="write machine-readable results here")
    args = parser.parse_args(argv)
    outcomes = run_catalog()
    print(render_table(outcomes))
    print()
    print(f"{'method':20} {'detected':9} {'missed':7} {'silent':7} false-alarms")
    rows = {"raw_definition_diff": raw_baseline_row(outcomes),
            "behave_rv": behave_rv_row(outcomes)}
    for name, row in (("raw-definition diff", rows["raw_definition_diff"]),
                      ("behave_rv", rows["behave_rv"])):
        print(f"{name:20} {row['detected']:>9} {row['missed']:>7} "
              f"{row['silent']:>7} {row['false_alarms']:>12}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({
            "experiment": "predicate_stability",
            "cases": [{"case": o.case.case_id, "family": o.case.family,
                       "behavior_changed": o.behavior_changed,
                       "diff_status": o.diff_status,
                       "breaks": len(o.diff_breaks),
                       "liveness_warnings": len(o.liveness),
                       "classification": o.classification}
                      for o in outcomes],
            "table": rows,
        }, indent=1, sort_keys=True) + "\n")
        print(f"results written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
