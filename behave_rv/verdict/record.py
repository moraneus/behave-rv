"""The verdict record. Replaces behave's pass/fail tally.

Verdicts are three-valued: ``satisfied``, ``violated``, ``pending``. An
unbounded future property can never be ``violated`` on a finite prefix, which is
why the authorable vocabulary is restricted to the monitorable fragment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from behave_rv.events.event import Event


@dataclass
class Verdict:
    policy_id: str
    entity_key: dict[str, str]       # which entity, e.g. {"order_id": "4471"}
    verdict: str                     # "satisfied" | "violated" | "pending"
    trigger_event: Optional[Event]   # None when a verdict has no triggering event
    witnessing_trace: list[Event]    # the events that drove this instance to the verdict
    at: float                        # event time of the verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "entity_key": dict(self.entity_key),
            "verdict": self.verdict,
            "trigger_event": self.trigger_event.to_dict() if self.trigger_event else None,
            "witnessing_trace": [e.to_dict() for e in self.witnessing_trace],
            "at": self.at,
        }
