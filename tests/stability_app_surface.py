"""The APP-change catalog: emit-site impact analysis, measured with ground truth.

Fifteen realistic changes to an APPLICATION (not to steps -- that is the A-D
series in stability_catalog.py), each applied one at a time against a fixed
baseline service, each with ground truth declared in advance. For every case
the harness:

1. verifies the ground truth empirically -- the SAME scripted traffic is run
   through the baseline service and the changed one (variants are exec'd; the
   analyzer itself never imports app code). Ground truth is the emitted event
   STREAM -- the app's observable behavior, exactly what the analyzer claims
   to guard -- and the policy verdict set is compared alongside, so a case
   like E10 shows the layering honestly: the stream changed, verdicts did not
   (nothing observes the new event yet);
2. runs the analyzer -- ``analyze_app`` on both sources, ``classify_app_changes``
   on the results -- and reduces the statuses to a detection level:
   break > risk > suggestion > silent;
3. classifies the outcome: CORRECT (flagged a real change, or stayed silent on
   a no-op), FALSE ALARM (flagged a change that preserved behavior; the E13-E15
   family is conservative BY DESIGN and counted, never hidden), or MISS
   (a behavior change nobody flagged -- must never happen).

Run the table:  python -m tests.stability_app_surface
Asserted under pytest in tests/test_stability_app_surface.py.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from behave_rv.catalog.app_surface import (
    APP_ADDED,
    APP_REMOVED,
    BEHAVIOR_RISK,
    INTERFACE_BREAK,
    analyze_app,
    classify_app_changes,
)
from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import compile_feature
from behave_rv.engine.loop import Engine
from behave_rv.events.sources.inprocess import InProcessSource

# ---------------------------------------------------------------------------
# the fixed baseline: a small service, two steps, three policies


APP_BASELINE = '''
from behave_rv.events.event import Event

from jobs_pricing import discount_label

STATUS = "job.status"
DONE = "job.done"
MIN_LEN = 0


def clean_name(name):
    return normalize(name)


def normalize(name):
    return name.strip()


def audited(fn):
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper


def audit_label(count):
    return f"{count} jobs seen"


class JobService:
    def __init__(self, emit, clock):
        self._emit = emit
        self._clock = clock

    def _status(self, job_id, status):
        self._emit(Event(STATUS, self._clock(), {"job_id": job_id},
                         {"status": status}, "jobs"))

    def submit(self, job_id, name):
        title = clean_name(name)
        if len(title) > MIN_LEN:
            self._status(job_id, "queued")

    @audited
    def start(self, job_id):
        self._status(job_id, "started")

    def finish(self, job_id):
        self._status(job_id, "finished")
        self._emit(Event(DONE, self._clock() + 1e-3, {"job_id": job_id}, {}, "jobs"))

    def bulk_close(self, job_ids):
        for job_id in job_ids:
            self._status(job_id, "finished")

    def ingest(self, job_id, raw):
        try:
            size = int(raw)
        except ValueError:
            self._status(job_id, "rejected")
            return
        if size > MIN_LEN:
            self._status(job_id, "queued")

    def classify(self, job_id, size):
        label = "big" if size > 10 else "small"
        self._status(job_id, label)

    def price(self, job_id, amount):
        self._status(job_id, discount_label(amount))
'''

# the second application module: cross-module emission logic (case E22)
APP_HELPER = '''
def discount_label(amount):
    return "discounted" if amount > 100 else "full_price"
'''

POLICIES = """
Feature: job monitoring
  Scenario: started only after queued
    When a job is "started"
    Then a job is "queued" before

  Scenario: finished only after started
    When a job is "finished"
    Then a job is "started" before

  Scenario: done only after finished
    When a job is done
    Then a job is "finished" before
"""


def build_registry() -> StepRegistry:
    registry = StepRegistry()

    @registry.trigger('a job is "{status}"', step_id="e.job.status",
                      event_type="job.status", correlation_key="job_id")
    def job_is(ctx, event, status):
        if event.type == "job.status" and event.payload.get("status") == status:
            ctx.bind(job_id=event.bindings["job_id"])
            return True
        return False

    @registry.trigger('a job is done', step_id="e.job.done",
                      event_type="job.done", correlation_key="job_id")
    def job_done(ctx, event):
        if event.type == "job.done":
            ctx.bind(job_id=event.bindings["job_id"])
            return True
        return False

    return registry


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def tick(self, dt=1.0):
        self.now += dt


def run_traffic(service, clock) -> None:
    """Fixed scripted traffic; several inputs sit exactly on the edges the
    variants move (a blank name, a 2-character name, a tmp- prefixed name)."""
    service.submit("J1", "  alpha  ")
    clock.tick()
    service.start("J1")
    clock.tick()
    service.finish("J1")
    clock.tick()
    service.submit("J2", "   ")          # blank -> never queued
    clock.tick()
    service.start("J2")                  # baseline violation, deliberately
    clock.tick()
    service.submit("J3", "ok")
    clock.tick()
    service.start("J3")
    clock.tick()
    service.submit("J4", "tmp-load")
    clock.tick()
    service.start("J4")
    clock.tick()
    service.finish("J4")
    clock.tick()
    service.submit("J5", "x")            # length 1: on MIN_LEN's boundary (E16)
    clock.tick()
    service.start("J5")
    clock.tick()
    service.bulk_close(["J6", "J7"])     # loop emission (E19)
    clock.tick()
    service.ingest("J8", "not-a-number")  # exception path (E18)
    clock.tick()
    service.ingest("J9", "9")
    clock.tick()
    service.classify("JA", 12)           # ternary boundary (E20)
    clock.tick()
    service.classify("JB", 3)
    clock.tick()
    service.price("JC", 150)             # cross-module helper (E22)
    clock.tick()
    service.price("JD", 80)              # between the old and new thresholds


def observe(app_source: str, helper_source: str = None) -> tuple[list, set]:
    """Ground truth: exec the app source (harness only -- the ANALYZER never
    imports app code), run the scripted traffic, and return both the emitted
    stream (canonical tuples) and the policy verdict set. The pricing helper
    module is materialised in sys.modules so the cross-module import works."""
    import sys
    import types
    helper = types.ModuleType("jobs_pricing")
    exec(compile(helper_source or APP_HELPER, "<e_helper>", "exec"),
         helper.__dict__)   # noqa: S102
    previous = sys.modules.get("jobs_pricing")
    sys.modules["jobs_pricing"] = helper
    try:
        namespace = {"__name__": "e_variant"}
        exec(compile(app_source, "<e_variant>", "exec"), namespace)   # noqa: S102
        service_cls = next(v for v in namespace.values()
                           if isinstance(v, type)
                           and getattr(v, "__module__", "") == "e_variant")
        clock = _FakeClock()
        events = []
        service = service_cls(events.append, clock=clock)
        run_traffic(service, clock)
    finally:
        if previous is not None:
            sys.modules["jobs_pricing"] = previous
        else:
            sys.modules.pop("jobs_pricing", None)
    stream = [(e.type, e.event_time, tuple(sorted(e.bindings.items())),
               tuple(sorted(e.payload.items()))) for e in events]
    source = InProcessSource()
    for event in events:
        source.emit(event)
    policies = compile_feature(POLICIES, build_registry())
    engine = Engine(policies, terminal_event_types={"job.done"})
    verdicts = engine.run(source, emit_pending=True)
    return stream, {(v.policy_id, v.entity_key["job_id"], v.verdict) for v in verdicts}


def change_statuses(old_source: str, new_source: str,
                    old_helper: str = None, new_helper: str = None) -> set:
    """The classifier's per-site statuses across the two versions -- both
    application modules on each side (the app spans two files since E22)."""
    with tempfile.TemporaryDirectory() as tmp:
        old_dir, new_dir = Path(tmp) / "old", Path(tmp) / "new"
        old_dir.mkdir(), new_dir.mkdir()
        (old_dir / "app.py").write_text(old_source)     # same file names: the
        (new_dir / "app.py").write_text(new_source)     # files are edited in place
        (old_dir / "jobs_pricing.py").write_text(old_helper or APP_HELPER)
        (new_dir / "jobs_pricing.py").write_text(new_helper or old_helper or APP_HELPER)
        changes = classify_app_changes(
            analyze_app([old_dir / "app.py", old_dir / "jobs_pricing.py"]),
            analyze_app([new_dir / "app.py", new_dir / "jobs_pricing.py"]))
    return {c.status for c in changes}


def detection_level(old_source: str, new_source: str,
                    old_helper: str = None, new_helper: str = None) -> str:
    """Reduce the per-site statuses: the strongest signal wins
    (break > risk > suggestion > silent)."""
    statuses = change_statuses(old_source, new_source, old_helper, new_helper)
    if statuses & {INTERFACE_BREAK, APP_REMOVED}:
        return "break"
    if BEHAVIOR_RISK in statuses:
        return "risk"
    if APP_ADDED in statuses:
        return "suggestion"
    return "silent"


# ---------------------------------------------------------------------------
# the cases


def _replace(old: str, new: str) -> Callable[[str], str]:
    def transform(source: str) -> str:
        assert old in source, f"transform target not found: {old!r}"
        return source.replace(old, new)
    return transform


@dataclass(frozen=True)
class Case:
    case_id: str
    title: str
    transform: Callable[[str], str] = None      # edit to the main module
    transform_helper: Callable[[str], str] = None  # edit to the pricing module
    expect: str = "silent"         # "silent" | "risk" | "break" | "suggestion"
    stream_changes: bool = False   # ground truth (emitted events), verified per run
    verdicts_change: bool = False  # secondary truth (policy impact), verified too
    by_design: str = ""            # note for expected false alarms


CASES = [
    Case("E1", "comment and docstring edited",
         _replace("    def start(self, job_id):",
                  "    # transition to the running state\n"
                  "    def start(self, job_id):\n"
                  '        """Move the job to the running state."""'),
         expect="silent", stream_changes=False, verdicts_change=False),
    Case("E2", "local variable renamed in an emit path",
         lambda s: s.replace("title = clean_name(name)", "label = clean_name(name)")
                    .replace("if len(title) > MIN_LEN:", "if len(label) > MIN_LEN:"),
         expect="silent", stream_changes=False, verdicts_change=False),
    Case("E3", "the service class renamed",
         _replace("class JobService:", "class JobPipeline:"),
         expect="silent", stream_changes=False, verdicts_change=False),
    Case("E4", "guard before an emission tightened",
         _replace("if len(title) > MIN_LEN:",
                  'if len(title) > MIN_LEN and not title.startswith("tmp-"):'),
         expect="risk", stream_changes=True, verdicts_change=True),
    Case("E5", "helper logic changed two calls deep",
         _replace("return name.strip()",
                  'return name.strip() if len(name.strip()) > 2 else ""'),
         expect="risk", stream_changes=True, verdicts_change=True),
    Case("E6", "an emitted status value renamed (vocabulary drift)",
         _replace('self._status(job_id, "queued")', 'self._status(job_id, "enqueued")'),
         expect="risk", stream_changes=True, verdicts_change=True),
    Case("E7", "a payload field renamed",
         _replace('{"status": status}', '{"state": status}'),
         expect="break", stream_changes=True, verdicts_change=True),
    Case("E8", "the event type constant changed",
         _replace('STATUS = "job.status"', 'STATUS = "job.state"'),
         expect="break", stream_changes=True, verdicts_change=True),
    Case("E9", "an emission deleted",
         _replace('        self._emit(Event(DONE, self._clock() + 1e-3,'
                  ' {"job_id": job_id}, {}, "jobs"))\n', ""),
         expect="break", stream_changes=True, verdicts_change=True),
    Case("E10", "a new emission added inside an existing method",
         _replace('        self._status(job_id, "started")',
                  '        self._status(job_id, "started")\n'
                  '        self._emit(Event("job.audit", self._clock(),'
                  ' {"job_id": job_id}, {}, "jobs"))'),
         expect="risk", stream_changes=True, verdicts_change=False),
    Case("E11", "the terminal emission moved before the status it follows",
         _replace("self._clock() + 1e-3", "self._clock() - 1e-3"),
         expect="risk", stream_changes=True, verdicts_change=True),
    Case("E12", "a function outside every emit slice changed",
         _replace('return f"{count} jobs seen"', 'return f"jobs seen: {count}"'),
         expect="silent", stream_changes=False, verdicts_change=False),
    Case("E13", "a function on an emit path renamed (pure)",
         lambda s: s.replace("def _status(", "def _transition(")
                    .replace("self._status(", "self._transition("),
         expect="silent", stream_changes=False, verdicts_change=False),
    Case("E14", "extract-method refactor inside an emit slice",
         _replace("        title = clean_name(name)\n"
                  "        if len(title) > MIN_LEN:\n"
                  '            self._status(job_id, "queued")',
                  "        if self._admissible(name):\n"
                  '            self._status(job_id, "queued")\n'
                  "\n"
                  "    def _admissible(self, name):\n"
                  "        return len(clean_name(name)) > MIN_LEN"),
         expect="risk", stream_changes=False, verdicts_change=False,
         by_design="slice membership changed; structural fingerprints cannot "
                   "prove the refactor equivalent"),
    Case("E15", "the event type becomes computed instead of a constant",
         lambda s: s.replace('STATUS = "job.status"',
                             'def status_type():\n    return "job.status"')
                    .replace("Event(STATUS,", "Event(status_type(),"),
         expect="break", stream_changes=False, verdicts_change=False,
         by_design="the type is no longer statically analyzable; losing the "
                   "check must surface, not silently degrade"),
    Case("E16", "a module-level constant used in emission logic changes",
         _replace("MIN_LEN = 0", "MIN_LEN = 1"),
         expect="risk", stream_changes=True, verdicts_change=True),
    Case("E17", "a benign attribute added in the constructor",
         _replace("        self._clock = clock",
                  "        self._clock = clock\n        self._audit = []"),
         expect="risk", stream_changes=False, verdicts_change=False,
         by_design="emit-path state flows through instance attributes, so the "
                   "constructor joins every slice of its class; attribute "
                   "dependencies are approximated at method granularity"),
    Case("E18", "the status emitted on an exception path changes",
         _replace('self._status(job_id, "rejected")',
                  'self._status(job_id, "quarantined")'),
         expect="risk", stream_changes=True, verdicts_change=False),
    Case("E19", "a loop emission processes fewer items",
         _replace("for job_id in job_ids:", "for job_id in job_ids[:1]:"),
         expect="risk", stream_changes=True, verdicts_change=True),
    Case("E20", "a ternary guard on the emitted value moves",
         _replace('"big" if size > 10 else "small"',
                  '"big" if size > 20 else "small"'),
         expect="risk", stream_changes=True, verdicts_change=False),
    Case("E21", "a decorator on an emit-path method changes behavior",
         _replace("        return fn(*args, **kwargs)", "        return None"),
         expect="risk", stream_changes=True, verdicts_change=True),
    Case("E22", "emission logic changes in a SECOND module",
         transform_helper=_replace("amount > 100", "amount > 50"),
         expect="risk", stream_changes=True, verdicts_change=False),
]


# ---------------------------------------------------------------------------
# the runner


@dataclass(frozen=True)
class Row:
    case_id: str
    title: str
    expect: str
    detected: str
    stream_changed: bool
    verdicts_changed: bool
    outcome: str                   # "CORRECT" | "FALSE ALARM" | "MISS"
    by_design: str


def run_catalog() -> list[Row]:
    baseline_stream, baseline_verdicts = observe(APP_BASELINE)
    rows: list[Row] = []
    for case in CASES:
        variant = case.transform(APP_BASELINE) if case.transform else APP_BASELINE
        helper = (case.transform_helper(APP_HELPER)
                  if case.transform_helper else APP_HELPER)
        assert (variant, helper) != (APP_BASELINE, APP_HELPER), \
            f"{case.case_id}: transform was a no-op"

        stream, verdicts = observe(variant, helper)
        stream_changed = stream != baseline_stream
        verdicts_changed = verdicts != baseline_verdicts
        for name, declared, measured in (
                ("stream_changes", case.stream_changes, stream_changed),
                ("verdicts_change", case.verdicts_change, verdicts_changed)):
            if declared != measured:
                raise AssertionError(
                    f"{case.case_id}: declared ground truth says {name}="
                    f"{declared}, but the replayed traffic says {measured}")

        detected = detection_level(APP_BASELINE, variant, APP_HELPER, helper)
        if stream_changed and detected == "silent":
            outcome = "MISS"
        elif stream_changed:
            outcome = "CORRECT"
        elif detected == "silent":
            outcome = "CORRECT"
        else:
            outcome = "FALSE ALARM"
        rows.append(Row(case.case_id, case.title, case.expect, detected,
                        stream_changed, verdicts_changed, outcome, case.by_design))
    return rows


def main(argv=None) -> int:
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="write machine-readable results here")
    args = parser.parse_args(argv)
    rows = run_catalog()
    width = max(len(r.title) for r in rows)
    print(f"{'case':5} {'change':{width}} {'stream':8} {'verdicts':9} {'detected':9} outcome")
    for r in rows:
        print(f"{r.case_id:5} {r.title:{width}} "
              f"{('changed' if r.stream_changed else 'same'):8} "
              f"{('changed' if r.verdicts_changed else 'same'):9} "
              f"{r.detected:9} {r.outcome}"
              + (f"  (by design: {r.by_design})" if r.by_design else ""))
    misses = [r for r in rows if r.outcome == "MISS"]
    false_alarms = [r for r in rows if r.outcome == "FALSE ALARM"]
    print(f"\n{len(rows)} cases: {len(rows) - len(misses) - len(false_alarms)} correct, "
          f"{len(false_alarms)} false alarm(s) (all by design), {len(misses)} miss(es)")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({
            "experiment": "app_curated",
            "cases": [{"case": r.case_id, "title": r.title,
                       "stream_changed": r.stream_changed,
                       "verdicts_changed": r.verdicts_changed,
                       "detected": r.detected, "outcome": r.outcome,
                       "by_design": r.by_design} for r in rows],
            "correct": len(rows) - len(misses) - len(false_alarms),
            "false_alarms": len(false_alarms), "misses": len(misses),
        }, indent=1, sort_keys=True) + "\n")
        print(f"results written to {args.out}")
    return 1 if misses else 0


if __name__ == "__main__":
    raise SystemExit(main())
