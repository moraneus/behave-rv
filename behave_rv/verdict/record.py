"""The verdict record. Replaces behave's pass/fail tally.

Verdicts are three-valued: ``satisfied``, ``violated``, ``pending``. An
unbounded future property can never be ``violated`` on a finite prefix, which is
why the authorable vocabulary is restricted to the monitorable fragment.
"""

from __future__ import annotations

from dataclasses import dataclass

from behave_rv.events.event import Event


@dataclass
class Verdict:
    policy_id: str
    entity_key: dict[str, str]       # which entity, e.g. {"order_id": "4471"}
    verdict: str                     # "satisfied" | "violated" | "pending"
    trigger_event: Event
    witnessing_trace: list[Event]    # the events that drove this instance to the verdict
    at: float                        # event time of the verdict
