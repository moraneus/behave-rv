"""Compile a parsed .feature policy into a runnable compile.Policy.

This is the seam that closes the loop: the human authors a policy in Gherkin and
the engine runs it, with no Python policy construction in the path. The compiler
reuses behave's parser (Phase 0) and emits the same Policy/automaton objects the
engine already runs (Phase 3).

v1 policy grammar (one scenario = one policy). Every operator is predicate-first
with a temporal suffix:

    never  (self-contained, no When -- the Then predicate is the forbidden event):
        Then <registered step> never happens

    before / within  (a triggering When plus the obligation):
        When <registered step>
        Then <registered step> before
        Then <registered step> within "<n>" seconds

Each step is resolved against the catalog by stable step_id (so a rephrasing that
maps to the same step_id still compiles). The correlation key is taken from the
resolved steps; a scenario that needs more than one independent entity key is
refused -- the v1 single-key fragment boundary.

Honestly unfinished in v1 (refused with a clear message rather than faked):
Given/scope steps, and the scoped "when X, then Y never happens" form, are
recognized but not wired.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from typing import Optional

from behave_rv.catalog.registry import Resolution, StepRegistry
from behave_rv.compile.automaton import (
    BeforeMonitor,
    Monitor,
    NeverMonitor,
    Policy,
    WithinMonitor,
)
from behave_rv.compile.parser_bridge import parse_feature
from behave_rv.events.event import Event
from behave_rv.steps.context import MatchContext

Predicate = Callable[[Event], bool]

_WITHIN = re.compile(r'^(?P<resp>.*?)\s+within\s+"?(?P<secs>\d+(?:\.\d+)?)"?\s+seconds?\s*$')
_BEFORE = re.compile(r"^(?P<prior>.*?)\s+before\s*$")
_NEVER = re.compile(r"^(?P<pred>.*?)\s+never\s+happens\s*$")


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
    when, then = _split_steps(scenario)
    operator, operand_res, seconds = _parse_obligation(then[0].name, registry)

    if operator == "never":
        # never is self-contained: the Then predicate itself is the forbidden event.
        if when:
            raise CompileError(
                f"a 'never' policy is self-contained and must not have a When step "
                f"({when[0].name!r}). 'when X, then Y never happens' is a scoped form "
                "outside the current fragment; write 'Then <predicate> never happens'."
            )
        used = [operand_res]
        factory = _never_factory(_predicate(operand_res))
    else:
        if len(when) != 1:
            raise CompileError(
                f"a v1 '{operator}' policy needs exactly one When step, found {len(when)}"
            )
        trigger_res = _resolve_one(registry, when[0].name, "When")
        trigger_pred = _predicate(trigger_res)
        used = [trigger_res, operand_res]
        if operator == "within":
            factory = _within_factory(trigger_pred, _predicate(operand_res), seconds)
        else:  # before
            factory = _before_factory(_predicate(operand_res), trigger_pred)

    correlation_key = _single_key(used, scenario)
    event_types = frozenset(r.signature.event_type for r in used)
    _warn_if_uncheckable(scenario, used, observed_event_types)

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

    if given:
        raise CompileError(
            "Given/scope steps are recognized but not yet wired into the v1 "
            f"operators: {given[0].name!r}. Express the property with When/Then for now."
        )
    if len(then) != 1:
        raise CompileError(f"a v1 policy needs exactly one Then step, found {len(then)}")
    # the When count is checked per operator in compile_scenario (never takes none).
    return when, then


# -- obligation parsing -----------------------------------------------------


def _parse_obligation(text, registry):
    """Return (operator, operand_resolution, seconds_or_None). The operand is the
    registered predicate the operator refers to (the forbidden event for never)."""
    m = _NEVER.match(text)
    if m:
        operand = _resolve_one(registry, m.group("pred").strip(), "never-predicate")
        return "never", operand, None

    m = _WITHIN.match(text)
    if m:
        operand = _resolve_one(registry, m.group("resp").strip(), "within-response")
        return "within", operand, float(m.group("secs"))

    m = _BEFORE.match(text)
    if m:
        operand = _resolve_one(registry, m.group("prior").strip(), "before-condition")
        return "before", operand, None

    raise CompileError(
        f"unrecognized temporal obligation: {text!r}. Supported forms: "
        "'<step> never happens', '<step> within \"<n>\" seconds', '<step> before'."
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


def _never_factory(bad: Predicate) -> Callable[[], Monitor]:
    return lambda: NeverMonitor(bad)


def _within_factory(trigger: Predicate, response: Predicate, seconds: float):
    return lambda: WithinMonitor(trigger, response, seconds)


def _before_factory(prior: Predicate, trigger: Predicate):
    return lambda: BeforeMonitor(prior, trigger)
