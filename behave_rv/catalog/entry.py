"""The step catalog entry and its signature.

The catalog is the hinge of the whole system: it is what makes human policies
survive the agent's refactoring. Human policies bind to ``step_id``, not to
phrasing text, so renames flow through untouched. A signature change to a *used*
step is a versioned, reviewable event.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StepSignature:
    event_type: str                  # which event it observes
    trigger_condition: str           # condition over observable state (abstracted, not the code path)
    payload_fields: dict[str, str]   # field name -> type, for fields exposed
    referenced_fields: set[str]      # the subset a specification can actually bind or read
    correlation_key: tuple[str, ...]  # one key, possibly a tuple for composite identity


@dataclass
class CatalogEntry:
    step_id: str                     # stable opaque id, survives renaming, never reused
    phrasing: str                    # the Gherkin text with placeholders
    kind: str                        # "trigger" | "scope" | "obligation"
    signature: StepSignature
    provenance: str                  # "human" | "llm" | "telemetry"
    observed: bool                   # has this event actually been seen in a real stream
    version: int
