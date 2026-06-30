"""Render the authored Gherkin scenario with bound values, the failing step marked.

The reason for a violation is the human's own scenario, rendered back in its
Gherkin, as a counterexample -- not a description of the failure. One formalism
serves two situations: a runtime violation and a build-time policy invalidation.
A violation marks the failing step; an invalidation marks the step whose contract
moved. Both bind the placeholders with real values where available.
"""

from __future__ import annotations

import re
from typing import Any

from behave_rv.verdict.record import Verdict

# parse-style placeholders: {name} or {name:type}
_PLACEHOLDER = re.compile(r"\{\s*(\w+)\s*(?::[^}]*)?\}")

_FAIL_MARK = "✗"
_OK_INDENT = "  "  # aligns plain steps under the marked one


def bind_text(text: str, bindings: dict[str, str]) -> str:
    """Substitute ``{name}`` / ``{name:type}`` with bindings, leaving unknowns intact."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return bindings[name] if name in bindings else match.group(0)

    return _PLACEHOLDER.sub(replace, text)


def bindings_from_verdict(verdict: Verdict) -> dict[str, str]:
    """The real values to bind in: the entity key plus the trigger event's fields."""
    bindings: dict[str, str] = dict(verdict.entity_key)
    trigger = verdict.trigger_event
    if trigger is not None:
        bindings.update({k: str(v) for k, v in trigger.payload.items()})
        bindings.update(trigger.bindings)
    return bindings


def render_explanation(
    scenario: Any,
    *,
    bindings: dict[str, str],
    failing_step_index: int,
    mark: str = "violated",
) -> str:
    """Render a behave scenario back as Gherkin, values bound and one step marked.

    ``scenario`` is a behave ``Scenario`` model (from :func:`parse_feature`).
    """
    lines = [f"Scenario: {scenario.name}"]
    for i, step in enumerate(scenario.steps):
        bound = bind_text(step.name, bindings)
        body = f"{step.keyword} {bound}"
        if i == failing_step_index:
            lines.append(f"{_FAIL_MARK} {body}   # {mark}")
        else:
            lines.append(f"{_OK_INDENT}  {body}")
    return "\n".join(lines)
