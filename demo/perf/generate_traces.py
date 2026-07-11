"""Deterministic performance-trace generation for all three demos.

Reuses each demo's real mock service so the traces are realistic for the
domain: many entities interleaved, the demo's own status flows, a mix of
completed and still-open entities, and terminal events for a majority of
order/session entities so GC is exercised (the todo demo has no terminal
event by design; its entities stay live, which the report calls out).

Determinism: everything derives from SEED (below). Same seed, byte-identical
file -- the committed CHECKSUMS.sha256 proves regeneration equality.

Event times are monotone per entity with fixed pacing; ARRIVAL order (the
line order in the file) is the true time order perturbed by a small seeded
jitter of +/-0.2s, so the reorder buffer does real work. With the engine's
default grace of 5.0s this produces zero late drops (max displacement 0.4s
<< 5.0s): this is a throughput measurement, not a correctness test.

Usage:
    python -m demo.perf.generate_traces                # all nine files
    python -m demo.perf.generate_traces --only-missing # regenerate gitignored 100k files
"""

from __future__ import annotations

import argparse
import hashlib
import random
from pathlib import Path

from behave_rv.events.event import Event
from behave_rv.events.sources.replay import record_events

SEED = 20260711
JITTER_S = 0.2          # arrival-order perturbation; engine grace is 5.0 (default)
ENTITY_GAP_S = 0.35     # new entity every 0.35s -> roughly a dozen concurrent
SIZES = {"1k": 1_000, "10k": 10_000, "100k": 100_000}
TRACES = Path(__file__).parent / "traces"


class _Clock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.now += dt


def _run_flow(service_cls, flow, entity_id, start, extra_terminal=None):
    events: list[Event] = []
    clock = _Clock(start)
    service = service_cls(events.append, clock=clock, sleep=clock.sleep)
    getattr(service, flow)(entity_id)
    if extra_terminal is not None:
        events.append(extra_terminal(entity_id, clock.now))
    return events


# -- per-demo flow mixes ------------------------------------------------------
# (flow name, weight, append_terminal). Weights are relative; append_terminal
# adds the demo's terminal event to flows that complete an entity's story but
# whose demo flow does not emit one itself, so a majority of entities retire.


def _order_terminal(oid, now):
    return Event("order.done", now, {"order_id": oid}, {}, "perf")


def _session_terminal(uid, now):
    return Event("session.end", now, {"user_id": uid}, {}, "perf")


ORDER_MIX = [
    ("flow_full_lifecycle", 45, None),                  # emits order.done itself
    ("flow_cancel_refund", 15, _order_terminal),
    ("flow_flagged_reviewed", 10, _order_terminal),
    ("bug_pay_without_auth", 6, None),
    ("bug_ship_without_pay", 5, None),
    ("bug_refund_without_cancel", 4, None),
    ("bug_cancel_never_refund", 5, None),
    ("bug_double_charge", 4, _order_terminal),
    ("bug_ship_after_cancel", 3, _order_terminal),
    ("bug_pay_after_flag", 3, None),
]

SESSION_MIX = [
    ("flow_login_work_logout", 45, None),               # emits session.end itself
    ("flow_lock_and_review", 15, _session_terminal),
    ("flow_unlock_contrast", 10, _session_terminal),
    ("flow_flagged_reviewed", 8, _session_terminal),
    ("bug_action_without_login", 5, None),
    ("bug_logout_without_login", 4, None),
    ("bug_stale_token", 5, None),
    ("bug_act_after_logout", 4, _session_terminal),
    ("bug_lock_without_fail", 2, None),
    ("bug_relock_then_act", 2, None),
]

TODO_MIX = [                                            # no terminal event exists
    ("flow_quick_task", 30, None),
    ("flow_edit_and_archive", 20, None),
    ("flow_rework", 12, None),
    ("flow_block_unblock", 10, None),
    ("flow_sync_healthy", 10, None),
    ("bug_complete_unstarted", 4, None),
    ("bug_edit_uncreated", 3, None),
    ("bug_reopen_uncompleted", 3, None),
    ("bug_edit_archived", 3, None),
    ("bug_miss_due_window", 2, None),
    ("bug_blocked_completes", 2, None),
    ("bug_sync_fails", 1, None),
]


def _demo_config(demo: str):
    if demo == "order":
        from demo.order_service.service import OrderService
        return OrderService, ORDER_MIX, "ORD"
    if demo == "session":
        from demo.session_service.service import SessionService
        return SessionService, SESSION_MIX, "USR"
    if demo == "todo":
        from demo.todo_app.service import TodoService
        return TodoService, TODO_MIX, "ENT"
    raise SystemExit(f"unknown demo {demo!r}")


def generate(demo: str, size: int) -> list[Event]:
    service_cls, mix, prefix = _demo_config(demo)
    rng = random.Random(f"{SEED}:{demo}:{size}")
    flows = [(name, terminal) for name, weight, terminal in mix for _ in range(weight)]

    events: list[Event] = []
    entity = 0
    while len(events) < size + 200:                    # headroom before truncation
        flow, terminal = rng.choice(flows)
        start = entity * ENTITY_GAP_S
        events.extend(_run_flow(service_cls, flow, f"{prefix}-{entity + 1}",
                                start, terminal))
        entity += 1

    # arrival order: true time order perturbed by the seeded jitter
    keyed = [(e.event_time + rng.uniform(-JITTER_S, JITTER_S), i, e)
             for i, e in enumerate(events)]
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [e for _, _, e in keyed[:size]]


def write_all(only_missing: bool = False) -> None:
    TRACES.mkdir(exist_ok=True)
    for demo in ("order", "session", "todo"):
        for label, size in SIZES.items():
            path = TRACES / f"{demo}_{label}.jsonl"
            if only_missing and path.exists():
                continue
            record_events(path, generate(demo, size))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            print(f"{path.name:22} {size:>7} events  sha256={digest[:16]}…")


def checksums() -> str:
    lines = []
    for path in sorted(TRACES.glob("*.jsonl")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-missing", action="store_true",
                        help="regenerate only absent files (the gitignored 100k traces)")
    parser.add_argument("--write-checksums", action="store_true")
    args = parser.parse_args()
    write_all(only_missing=args.only_missing)
    if args.write_checksums:
        (TRACES / "CHECKSUMS.sha256").write_text(checksums())
        print("checksums written")
