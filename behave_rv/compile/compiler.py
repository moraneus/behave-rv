"""Compile a parsed .feature policy into a runnable compile.Policy.

This is the seam that closes the loop: the human authors a policy in Gherkin and
the engine runs it, with no Python policy construction in the path. The compiler
reuses behave's parser (Phase 0) and emits the same Policy/automaton objects the
engine already runs (Phase 3).

v1 policy grammar (one scenario = one policy):

    [no Given]
    When  <registered step>            -- the triggering event
    Then  <temporal obligation>        -- the property

The temporal obligation is one of:

    Then it must never happen                       -> never (the When event is bad)
    Then <registered step> within "<n>" seconds     -> within (bounded response)
    Then <registered step> before                    -> before (precedence)

Each non-temporal step is resolved against the catalog by stable step_id (so a
rephrasing that maps to the same step_id still compiles). The correlation key is
taken from the resolved steps; a scenario that needs more than one independent
entity key is refused -- the v1 single-key fragment boundary.

Honestly unfinished in v1 (refused with a clear message rather than faked):
Given/scope steps are recognized but not yet wired into the operators.
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
_NEVER = re.compile(r"\bnever\b")


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
    when_steps, then_steps = _split_steps(scenario)

    when = when_steps[0]
    then = then_steps[0]
    trigger_res = _resolve_one(registry, when.name, "When")
    trigger_pred = _predicate(trigger_res)

    operator, operand_res, seconds = _parse_obligation(then.name, registry)

    used = [trigger_res] + ([operand_res] if operand_res is not None else [])
    correlation_key = _single_key(used, scenario)
    event_types = frozenset(r.signature.event_type for r in used)
    _warn_if_uncheckable(scenario, used, observed_event_types)

    if operator == "never":
        factory = _never_factory(trigger_pred)
    elif operator == "within":
        factory = _within_factory(trigger_pred, _predicate(operand_res), seconds)
    else:  # before
        factory = _before_factory(_predicate(operand_res), trigger_pred)

    return Policy(
        policy_id=scenario.name,
        correlation_key=correlation_key,
        event_types=event_types,
        monitor_factory=factory,
        authored_scenario=scenario,
        failing_step_index=scenario.steps.index(then),
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
    if len(when) != 1:
        raise CompileError(f"a v1 policy needs exactly one When step, found {len(when)}")
    if len(then) != 1:
        raise CompileError(f"a v1 policy needs exactly one Then step, found {len(then)}")
    return when, then


# -- obligation parsing -----------------------------------------------------


def _parse_obligation(text, registry):
    """Return (operator, operand_resolution_or_None, seconds_or_None)."""
    if _NEVER.search(text):
        return "never", None, None

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
        "'it must never happen', '<step> within \"<n>\" seconds', '<step> before'."
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
