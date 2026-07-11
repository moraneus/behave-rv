"""Measure one experiment cell: (demo, trace, policy count) -> time and memory.

What is measured, stated plainly:

* TIME: wall-clock (time.perf_counter) of ``engine.run`` only. The steps
  module, policy compilation, and trace parsing into memory happen OUTSIDE the
  timer; a fresh Engine and source are constructed per repetition (engine
  construction is inside neither). Verdicts go to a NULL SINK (a no-op
  callable), so sink cost is constant and verdicts are never accumulated in a
  list.
* MEMORY: tracemalloc peak -- Python allocations, not RSS. tracemalloc adds
  real overhead, so time and memory are measured in SEPARATE runs: N timed
  repetitions with tracemalloc off, then one extra run with tracemalloc
  started before engine construction and the peak read after the run.
* DETERMINISM SANITY: the delivered-verdict count must be identical across
  every repetition (timed and memory); the cell fails otherwise.

Output: one JSON line appended to the --out file.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
import warnings
from datetime import datetime, timezone
from pathlib import Path

from behave_rv.compile.compiler import compile_feature
from behave_rv.engine.loop import Engine, NoTerminalConfiguredWarning
from behave_rv.events.event import Event
from behave_rv.events.sources.replay import ReplaySource

# The fixed policy ladder: P<n> always means the first n files of this list,
# forever, so runs stay comparable across time. One line each in the README.
LADDERS = {
    "order": ["01_paid_after_auth.feature",        # before
              "06_refund_window.feature",          # within (timer path)
              "08_never_double_charged.feature",   # self-contained never
              "04_eventually_invoiced.feature",    # once
              "11_flagged_only_reviewed.feature"], # since
    "session": ["01_action_after_login.feature",   # before
                "03_lock_after_fail.feature",      # previously
                "04_locked_never_acts.feature",    # scoped never (latching)
                "06_locked_until_unlocked.feature",  # until-scoped never
                "07_review_window.feature"],       # within (timer path)
    "todo": ["01_complete_after_start.feature",    # before
             "06_due_window.feature",              # within (timer path)
             "08_archived_never_edited.feature",   # scoped never
             "12_sync_historically_ok.feature",    # historically (sync channel)
             "04_eventually_completed.feature"],   # once
}

# Engine configuration mirrors each demo's own test harness: order and
# session retire entities at their terminal events (GC exercised); the todo
# demo has no terminal event by design, so its entities stay live.
TERMINALS = {"order": {"order.done"}, "session": {"session.end"}, "todo": set()}


def _demo_paths(demo: str):
    base = {"order": "order_service", "session": "session_service",
            "todo": "todo_app"}[demo]
    root = Path(__file__).resolve().parents[1] / base
    return root / "policies"


def _build_registry(demo: str):
    if demo == "order":
        from demo.order_service.steps import build_registry
    elif demo == "session":
        from demo.session_service.steps import build_registry
    else:
        from demo.todo_app.steps import build_registry
    return build_registry()


def _load_policies(demo: str, count: int):
    policy_dir = _demo_paths(demo)
    registry = _build_registry(demo)
    policies = []
    for name in LADDERS[demo][:count]:
        policies.extend(compile_feature((policy_dir / name).read_text(), registry))
    return policies


def _null_sink(verdict) -> None:
    return None


def _make_engine(demo: str, policies) -> Engine:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NoTerminalConfiguredWarning)
        return Engine(policies, terminal_event_types=TERMINALS[demo])


class _PreloadedSource:
    """The trace parsed into memory once, outside every timer, so the timed
    region is the engine, not JSON decoding."""

    def __init__(self, events: list[Event]):
        self._events = events

    def events(self):
        return iter(self._events)


def run_cell(demo: str, trace: str, policy_count: int, reps: int) -> dict:
    events = list(ReplaySource(trace).events())
    policies = _load_policies(demo, policy_count)

    times: list[float] = []
    verdict_counts: list[int] = []
    for _ in range(reps):
        engine = _make_engine(demo, policies)
        source = _PreloadedSource(events)
        start = time.perf_counter()
        engine.run(source, sink=_null_sink)
        times.append(time.perf_counter() - start)
        verdict_counts.append(engine.verdicts_delivered)

    # memory: a separate run, tracemalloc started before engine construction
    tracemalloc.start()
    engine = _make_engine(demo, policies)
    source = _PreloadedSource(events)
    engine.run(source, sink=_null_sink)
    peak_bytes = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    verdict_counts.append(engine.verdicts_delivered)

    assert len(set(verdict_counts)) == 1, \
        f"determinism violated: verdict counts {verdict_counts}"

    median = statistics.median(times)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    return {
        "demo": demo,
        "trace": Path(trace).name,
        "trace_size": len(events),
        "policy_count": policy_count,
        "reps": reps,
        "times_s": [round(t, 4) for t in times],
        "median_s": round(median, 4),
        "min_s": round(min(times), 4),
        "max_s": round(max(times), 4),
        "events_per_s": round(len(events) / median),
        "peak_mb": round(peak_bytes / 1_048_576, 2),
        "verdicts": verdict_counts[0],
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "commit": commit,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", required=True, choices=("order", "session", "todo"))
    parser.add_argument("--trace", required=True)
    parser.add_argument("--policies", required=True, type=int, choices=range(6))
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    row = run_cell(args.demo, args.trace, args.policies, args.reps)
    out = Path(args.out)
    out.parent.mkdir(exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    print(f"{row['demo']:8} {row['trace']:20} P{row['policy_count']}  "
          f"median {row['median_s']:8.4f}s  {row['events_per_s']:>8} ev/s  "
          f"peak {row['peak_mb']:7.2f} MB  verdicts {row['verdicts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
