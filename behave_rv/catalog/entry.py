"""The step catalog entry and its signature.

The catalog is the hinge of the whole system: it is what makes human policies
survive the agent's refactoring. Signature equivalence and break scoping work on
the stable ``step_id``, never on the phrasing text, so code renames flow through
untouched; a reworded phrasing stays backward compatible when the previous
wording is retained as an alias. A signature change to a *used* step is a
versioned, reviewable event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepSignature:
    event_type: str                  # which event it observes
    trigger_condition: str           # condition over observable state (not the code path)
    payload_fields: dict[str, str]   # field name -> type, for fields exposed
    referenced_fields: set[str]      # the subset a specification can actually bind or read
    correlation_key: tuple[str, ...]  # one key, possibly a tuple for composite identity
    condition_fingerprint: str = ""  # rename-invariant fingerprint of the match condition
    # call-graph coverage (see docs/STABILITY.md):
    # per-helper normalized-AST hashes for every statically reachable helper
    # (names for diff messages only; the fingerprint hashes body identities),
    # and the call sites the resolver could NOT follow -- the visible boundary
    helper_hashes: dict = field(default_factory=dict)
    unresolved_calls: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # referenced_fields sorted and correlation_key listed so the JSON form is
        # deterministic and diffable (the catalog is a reviewed artifact).
        return {
            "event_type": self.event_type,
            "trigger_condition": self.trigger_condition,
            "payload_fields": dict(self.payload_fields),
            "referenced_fields": sorted(self.referenced_fields),
            "correlation_key": list(self.correlation_key),
            "condition_fingerprint": self.condition_fingerprint,
            "helper_hashes": dict(sorted(self.helper_hashes.items())),
            "unresolved_calls": sorted(self.unresolved_calls),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepSignature":
        return cls(
            event_type=data["event_type"],
            trigger_condition=data["trigger_condition"],
            payload_fields=dict(data["payload_fields"]),
            referenced_fields=set(data["referenced_fields"]),
            correlation_key=tuple(data["correlation_key"]),
            condition_fingerprint=data.get("condition_fingerprint", ""),
            helper_hashes=dict(data.get("helper_hashes", {})),
            unresolved_calls=list(data.get("unresolved_calls", [])),
        )


@dataclass
class CatalogEntry:
    step_id: str                     # stable opaque id, survives renaming, never reused
    phrasing: str                    # the Gherkin text with placeholders
    kind: str                        # "trigger" | "scope" | "obligation"
    signature: StepSignature
    provenance: str                  # "human" | "llm" | "telemetry"
    observed: bool                   # has this event actually been seen in a real stream
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "phrasing": self.phrasing,
            "kind": self.kind,
            "signature": self.signature.to_dict(),
            "provenance": self.provenance,
            "observed": self.observed,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogEntry":
        return cls(
            step_id=data["step_id"],
            phrasing=data["phrasing"],
            kind=data["kind"],
            signature=StepSignature.from_dict(data["signature"]),
            provenance=data["provenance"],
            observed=data["observed"],
            version=data["version"],
        )
