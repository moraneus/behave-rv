"""Build-time messages on separate channels: break, weakening, suggestion.

Kept on separate channels so they never blur. After a code change the agent
recomputes signatures, diffs them against the committed catalog, and classifies
the result:

* Break -- a human policy can no longer be checked as written because a step it
  uses changed signature or was removed. Scoped: only the owners whose policies
  used the affected step are notified.
* Weakening -- an agent-owned behavior test changed what it asserts. The agent
  declaring an intended behavior change, surfaced rather than hidden.
* Suggestion -- new monitorable behavior appeared that no policy covers yet.

A rename (equivalent signature, changed phrasing) and an unchanged step produce
nothing: they are absorbed silently.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from behave_rv.catalog.diff import (
    ADDED,
    CHANGED,
    REMOVED,
    classify_changes,
    describe_signature_change,
)
from behave_rv.catalog.entry import CatalogEntry


@dataclass(frozen=True)
class PolicyUse:
    """Which steps a policy binds, and the human who owns it (for scoping)."""

    policy_id: str
    owner: str
    step_ids: frozenset[str]


@dataclass(frozen=True)
class AgentTest:
    """An agent-owned behavior test and what it asserts (an opaque marker)."""

    test_id: str
    owner: str
    asserts: str


@dataclass(frozen=True)
class Break:
    step_id: str
    policy_id: str
    owner: str
    detail: str


@dataclass(frozen=True)
class Suggestion:
    step_id: str
    phrasing: str
    detail: str


@dataclass(frozen=True)
class Weakening:
    test_id: str
    owner: str
    detail: str


@dataclass
class Notifications:
    breaks: list[Break] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    weakenings: list[Weakening] = field(default_factory=list)


def uses_from_policies(policies, owner: str = "unknown") -> list[PolicyUse]:
    """Build the policy-to-step dependency map from compiled policies.

    Reads ``Policy.used_step_ids`` -- the step_ids each scenario actually
    resolved to at compile time. This is the real map; deriving it from event
    types is wrong in general, because several steps may observe the same
    event type. Policies with an empty ``used_step_ids`` (hand-built, never
    compiled) are skipped: their dependencies are unknown, not empty.
    """
    return [PolicyUse(policy_id=p.policy_id, owner=owner,
                      step_ids=frozenset(p.used_step_ids))
            for p in policies if p.used_step_ids]


def notifications(
    old_catalog: list[CatalogEntry],
    new_catalog: list[CatalogEntry],
    uses: Iterable[PolicyUse],
    *,
    old_tests: Iterable[AgentTest] = (),
    new_tests: Iterable[AgentTest] = (),
) -> Notifications:
    """Diff two catalog versions and produce the three notification channels."""
    users_of: dict[str, list[PolicyUse]] = defaultdict(list)
    for use in uses:
        for step_id in use.step_ids:
            users_of[step_id].append(use)

    notes = Notifications()
    for change in classify_changes(old_catalog, new_catalog):
        if change.status == CHANGED:
            detail = describe_signature_change(change.old.signature, change.new.signature)
            notes.breaks.extend(_breaks(change.step_id, users_of, detail))
        elif change.status == REMOVED:
            notes.breaks.extend(_breaks(change.step_id, users_of, "step removed from catalog"))
        elif change.status == ADDED and not users_of.get(change.step_id):
            notes.suggestions.append(
                Suggestion(
                    step_id=change.step_id,
                    phrasing=change.new.phrasing,
                    detail="new monitorable behavior; no policy covers it yet",
                )
            )
        # RENAMED / UNCHANGED: absorbed silently

    notes.weakenings.extend(_weakenings(old_tests, new_tests))
    return notes


def _breaks(step_id: str, users_of: dict[str, list[PolicyUse]], detail: str) -> list[Break]:
    return [
        Break(step_id=step_id, policy_id=use.policy_id, owner=use.owner, detail=detail)
        for use in users_of.get(step_id, [])
    ]


def _weakenings(
    old_tests: Iterable[AgentTest], new_tests: Iterable[AgentTest]
) -> list[Weakening]:
    old_by = {t.test_id: t for t in old_tests}
    out: list[Weakening] = []
    for test in new_tests:
        previous = old_by.get(test.test_id)
        if previous is not None and previous.asserts != test.asserts:
            out.append(
                Weakening(
                    test_id=test.test_id,
                    owner=test.owner,
                    detail=f"assertion changed: {previous.asserts!r} -> {test.asserts!r}",
                )
            )
    return out
