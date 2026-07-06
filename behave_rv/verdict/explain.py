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

# The glyph on the marked step follows the verdict, so mark and comment agree.
_MARKS = {"satisfied": "✓", "violated": "✗", "pending": "·"}
_DEFAULT_MARK = "✗"  # for other reasons the step is highlighted (e.g. invalidated)
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


def safe_value(value: Any) -> str:
    """Render a monitored-system-controlled string safely: clean values pass
    through unchanged; a value containing control characters (newlines, ANSI
    escapes, ...) renders as its repr so it cannot spoof or mangle the output
    an operator reads."""
    text = str(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        return repr(text)
    return text


def explain_verdict(verdict: Verdict, scenario: Any, failing_step_index: int) -> str:
    """The full counterexample: a header, the authored scenario with the failing
    step marked and values bound, and the witnessing trace with event times."""
    entity = ", ".join(f"{k}={safe_value(v)}" for k, v in verdict.entity_key.items())
    header = (
        f"POLICY {verdict.policy_id!r}  ENTITY {entity}  "
        f"VERDICT {verdict.verdict} @ t={verdict.at}"
    )
    body = render_explanation(
        scenario,
        bindings=bindings_from_verdict(verdict),
        failing_step_index=failing_step_index,
        mark=verdict.verdict,
    )

    def _fmt(e):
        return f"  t={e.event_time}  {e.type}  {e.payload}"

    lines = [header, body]
    # The deciding events are always shown, whatever their age, so the explanation
    # can never omit the evidence that produced the verdict.
    if verdict.deciding_events:
        lines.append("Deciding events:")
        lines += [_fmt(e) for e in verdict.deciding_events]
    # Recent context from the bounded window, minus anything already shown above.
    context = [e for e in verdict.witnessing_trace if e not in verdict.deciding_events]
    if context:
        lines.append("Recent context:")
        lines += [_fmt(e) for e in context]
    return "\n".join(lines)


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
    glyph = _MARKS.get(mark, _DEFAULT_MARK)
    lines = [f"Scenario: {scenario.name}"]
    for i, step in enumerate(scenario.steps):
        bound = bind_text(step.name, bindings)
        body = f"{step.keyword} {bound}"
        if i == failing_step_index:
            lines.append(f"{glyph} {body}   # {mark}")
        else:
            lines.append(f"{_OK_INDENT}  {body}")
    return "\n".join(lines)
