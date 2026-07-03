"""Compile a parsed .feature policy into a runnable compile.Policy.

This is the seam that closes the loop: the human authors a policy in Gherkin and
the engine runs it, with no Python policy construction in the path. The compiler
reuses behave's parser (Phase 0) and emits the same Policy/automaton objects the
engine already runs (Phase 3).

v1 policy grammar (one scenario = one policy). Every operator is predicate-first
with a temporal suffix:

    never  (self-contained, no When -- the Then predicate is the forbidden event;
    optionally scoped by a Given, latching or interval):
        [Given <registered step> [until <registered step>]]
        Then <registered step> never happens

    before / within / previously  (a triggering When plus the obligation):
        When <registered step>
        Then <registered step> before
        Then <registered step> within "<n>" seconds
        Then <registered step> previously

Each step is resolved against the catalog by stable step_id (so a rephrasing that
maps to the same step_id still compiles). The correlation key is taken from the
resolved steps (a Given scope must share the Then's key); a scenario that needs
more than one independent entity key is refused -- the single-key fragment.

Honestly unfinished (refused with a clear message rather than faked): Given on
operators other than never, and combining Given with a When (scoped triggered
obligations).
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from typing import Optional

from behave_rv.catalog.registry import Resolution, StepRegistry
from behave_rv.compile.automaton import (
    BeforeMonitor,
    HistoricallyMonitor,
    Monitor,
    NeverMonitor,
    OnceMonitor,
    Policy,
    PreviouslyMonitor,
    ScopedNeverMonitor,
    SinceMonitor,
    WithinMonitor,
)
from behave_rv.compile.parser_bridge import parse_feature
from behave_rv.events.event import Event
from behave_rv.steps.context import MatchContext

Predicate = Callable[[Event], bool]

_WITHIN = re.compile(r'^(?P<resp>.*?)\s+within\s+"?(?P<secs>\d+(?:\.\d+)?)"?\s+seconds?\s*$')
_BEFORE = re.compile(r"^(?P<prior>.*?)\s+before\s*$")
_NEVER = re.compile(r"^(?P<pred>.*?)\s+never\s+happens\s*$")
_ONCE = re.compile(r"^(?P<pred>.*?)\s+has\s+happened\s*$")
_HISTORICALLY = re.compile(r"^(?P<pred>.*?)\s+always\s+holds\s*$")
_PREVIOUSLY = re.compile(r"^(?P<pred>.*?)\s+previously\s*$")
_SINCE = re.compile(r"^(?P<phi>.*?)\s+since\s+(?P<psi>.*)$")

# operators whose Then predicate(s) need no When trigger
_SELF_CONTAINED = {"never", "once", "historically", "since"}


class CompileError(Exception):
    """A policy could not be compiled, with a human-readable reason."""


class UncheckablePolicyWarning(UserWarning):
    """A compiled policy depends on a step whose event has never been observed."""


def compile_feature(
    text_or_feature,
    registry: StepRegistry,
    *,
    observed_event_types: Optional[set[str]] = None,
) -> list[Policy]:
    """Compile every scenario in a feature into a Policy.

    If ``observed_event_types`` is given, warn (do not refuse) when a policy
    depends on a step whose event type has never been observed in that stream.
    """
    feature = parse_feature(text_or_feature) if isinstance(text_or_feature, str) else text_or_feature
    return [
        compile_scenario(scenario, registry, observed_event_types=observed_event_types)
        for scenario in feature.scenarios
    ]


def compile_scenario(
    scenario,
    registry: StepRegistry,
    *,
    observed_event_types: Optional[set[str]] = None,
) -> Policy:
    given, when, then = _split_steps(scenario)
    operator, operands, seconds = _parse_obligation(then[0].name, registry)

    if given and operator != "never":
        raise CompileError(
            "Given/scope steps are only wired for 'never' so far; other operators "
            f"do not take a scope yet: {given[0].name!r}. Express the property "
            "with When/Then for now."
        )

    scope_res = close_res = None
    if operator == "never":
        if when:
            raise CompileError(
                "a 'never' policy takes a Given scope, not a When trigger "
                f"({when[0].name!r}). To restrict the obligation to a scope, write "
                "'Given <predicate>' (or 'Given <predicate> until <predicate>') "
                "before 'Then <predicate> never happens'."
            )
        if given:
            scope_res, close_res = _parse_scope(given[0].name, registry)
        trigger_res = None
        used = list(operands) + [r for r in (scope_res, close_res) if r is not None]
    elif operator in _SELF_CONTAINED:
        # once/historically/since are self-contained: the Then predicate(s)
        # are the whole property, no When trigger.
        if when:
            raise CompileError(
                f"a '{operator}' policy is self-contained and must not have a When "
                f"step ({when[0].name!r}); write the property as a single Then."
            )
        trigger_res = None
        used = list(operands)
    else:
        if len(when) != 1:
            raise CompileError(
                f"a v1 '{operator}' policy needs exactly one When step, found {len(when)}"
            )
        trigger_res = _resolve_one(registry, when[0].name, "When")
        used = [trigger_res, *operands]

    correlation_key = _single_key(used, scenario)
    event_types = frozenset(r.signature.event_type for r in used)
    _warn_if_uncheckable(scenario, used, observed_event_types)
    factory = _build_factory(operator, trigger_res, operands, seconds,
                             scope_res=scope_res, close_res=close_res)

    return Policy(
        policy_id=scenario.name,
        correlation_key=correlation_key,
        event_types=event_types,
        monitor_factory=factory,
        authored_scenario=scenario,
        failing_step_index=scenario.steps.index(then[0]),
    )


# -- step layout ------------------------------------------------------------


def _split_steps(scenario):
    given = [s for s in scenario.steps if s.step_type == "given"]
    when = [s for s in scenario.steps if s.step_type == "when"]
    then = [s for s in scenario.steps if s.step_type == "then"]

    if len(given) > 1:
        raise CompileError(f"a policy takes at most one Given scope, found {len(given)}")
    if len(then) != 1:
        raise CompileError(f"a v1 policy needs exactly one Then step, found {len(then)}")
    # Given wiring and the When count are checked per operator in compile_scenario.
    return given, when, then


_UNTIL = re.compile(r"^(?P<scope>.*?)\s+until\s+(?P<close>.*)$")


def _parse_scope(text, registry):
    """Parse a Given line: '<predicate>' (latching) or '<predicate> until
    <predicate>' (interval). Returns (scope_res, close_res_or_None)."""
    m = _UNTIL.match(text)
    if m:
        return (_resolve_one(registry, m.group("scope").strip(), "Given-scope"),
                _resolve_one(registry, m.group("close").strip(), "Given-until"))
    return _resolve_one(registry, text.strip(), "Given-scope"), None


# -- obligation parsing -----------------------------------------------------


def _parse_obligation(text, registry):
    """Return (operator, operands_tuple, seconds_or_None).

    operands is the tuple of registered predicates the operator refers to (one for
    most, two for since: (phi, psi)). Checked longest/most-specific suffix first.
    """
    def one(pred_text, where):
        return (_resolve_one(registry, pred_text.strip(), where),)

    m = _NEVER.match(text)
    if m:
        return "never", one(m.group("pred"), "never-predicate"), None
    m = _ONCE.match(text)
    if m:
        return "once", one(m.group("pred"), "once-predicate"), None
    m = _HISTORICALLY.match(text)
    if m:
        return "historically", one(m.group("pred"), "historically-predicate"), None
    m = _SINCE.match(text)
    if m:
        phi = _resolve_one(registry, m.group("phi").strip(), "since-phi")
        psi = _resolve_one(registry, m.group("psi").strip(), "since-psi")
        return "since", (phi, psi), None
    m = _PREVIOUSLY.match(text)
    if m:
        return "previously", one(m.group("pred"), "previously-condition"), None
    m = _WITHIN.match(text)
    if m:
        return "within", one(m.group("resp"), "within-response"), float(m.group("secs"))
    m = _BEFORE.match(text)
    if m:
        return "before", one(m.group("prior"), "before-condition"), None

    raise CompileError(
        f"unrecognized temporal obligation: {text!r}. Supported forms: "
        "'<step> never happens' (optionally scoped by 'Given <step>' or "
        "'Given <step> until <step>'), '<step> has happened', '<step> always holds', "
        "'<step> previously', '<step> since <step>', "
        "'<step> within \"<n>\" seconds', '<step> before'."
    )


# -- resolution + predicates ------------------------------------------------


def _warn_if_uncheckable(scenario, used, observed_event_types) -> None:
    if observed_event_types is None:
        return
    for res in used:
        if res.signature.event_type not in observed_event_types:
            warnings.warn(
                f"policy {scenario.name!r} depends on step {res.step_id!r} whose event "
                f"{res.signature.event_type!r} has never been observed in the available "
                "stream; the policy may be uncheckable.",
                UncheckablePolicyWarning,
                stacklevel=3,
            )


def _resolve_one(registry: StepRegistry, text: str, where: str) -> Resolution:
    matches = registry.resolve(text)
    if not matches:
        raise CompileError(f"no registered step matches the {where} step: {text!r}")
    if len(matches) > 1:
        raise CompileError(
            f"ambiguous {where} step {text!r}: matches step_ids "
            f"{[m.step_id for m in matches]}"
        )
    return matches[0]


def _predicate(resolution: Resolution) -> Predicate:
    func, params = resolution.func, resolution.params
    return lambda event: bool(func(MatchContext(), event, **params))


def _single_key(resolutions: list[Resolution], scenario) -> tuple[str, ...]:
    keys = {r.signature.correlation_key for r in resolutions}
    if len(keys) > 1:
        raise CompileError(
            f"scenario {scenario.name!r} references more than one entity key "
            f"{sorted(keys)}; the v1 fragment is one correlation key per scenario"
        )
    return next(iter(keys))


# -- monitor factories (kept tiny so closures don't capture loop vars) -------


def _build_factory(operator, trigger_res, operands, seconds,
                   scope_res=None, close_res=None) -> Callable[[], Monitor]:
    """Map (operator, resolved predicates) to a fresh-monitor factory. This is the
    only per-operator wiring the engine needs; the loop drives every monitor
    through the same Monitor interface."""
    p = [_predicate(r) for r in operands]
    t = _predicate(trigger_res) if trigger_res is not None else None
    if operator == "never" and scope_res is not None:
        sp = _predicate(scope_res)
        cp = _predicate(close_res) if close_res is not None else None
        return lambda: ScopedNeverMonitor(sp, p[0], cp)
    factories = {
        "never": lambda: NeverMonitor(p[0]),
        "once": lambda: OnceMonitor(p[0]),
        "historically": lambda: HistoricallyMonitor(p[0]),
        "since": lambda: SinceMonitor(p[0], p[1]),      # phi since psi
        "within": lambda: WithinMonitor(t, p[0], seconds),
        "before": lambda: BeforeMonitor(p[0], t),        # <prior> before, When=trigger
        "previously": lambda: PreviouslyMonitor(p[0], t),  # <prior> previously, When=trigger
    }
    return factories[operator]
