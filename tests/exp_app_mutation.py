"""Mutation experiment: soundness, precision, and scoping of the app-surface
analyzer, measured adversarially (RQ1-RQ3 of docs/APP_SURFACE_EVALUATION.md).

Every syntactic mutant a deterministic operator set can produce is applied to
each subject application, one at a time. For every mutant:

* ground truth -- the mutant is exec'd (experiment only; the analyzer never
  imports app code) and a fixed scripted traffic runs through it; the emitted
  event stream (type, time, bindings, payload, source) is compared with the
  baseline's. A mutant that raises during traffic counts as stream-changing.
* detection -- ``analyze_app`` on baseline and mutant sources,
  ``classify_app_changes``, reduced to flagged (any break/risk/added) or
  silent.
* scoping (mutants whose policy VERDICTS changed) -- is every policy whose
  verdicts moved contained in the analyzer's reported policies-at-risk set?

Confusion matrix per subject: TP (stream changed, flagged), MISS (stream
changed, silent -- each listed individually), ALARM (stream same on this
traffic, flagged; an upper bound on false alarms, since stream-equivalence
on one traffic script is not semantic equivalence), TN (stream same,
silent).

Run:  python -m tests.exp_app_mutation
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import sys
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path

from behave_rv.catalog.app_surface import (
    APP_ADDED,
    APP_REMOVED,
    BEHAVIOR_RISK,
    INTERFACE_BREAK,
    analyze_app,
    classify_app_changes,
    policies_at_risk,
)
from behave_rv.engine.loop import Engine, NoTerminalConfiguredWarning
from behave_rv.events.sources.inprocess import InProcessSource

from tests.stability_app_surface import (
    APP_BASELINE as JOBS_SOURCE,
    POLICIES as JOBS_POLICIES,
    _FakeClock,
    build_registry as build_jobs_registry,
    run_traffic as run_jobs_traffic,
)
from behave_rv.compile.compiler import compile_feature

ROOT = Path(__file__).resolve().parents[1]
FLAGGING = (BEHAVIOR_RISK, INTERFACE_BREAK, APP_REMOVED, APP_ADDED)


# ---------------------------------------------------------------------------
# subjects


def run_ticketing_traffic(service, clock):
    """Mirrors the six-ticket flow of examples/ticketing/replay_check.py."""
    service.open_ticket("T-1", "printer on fire")
    clock.tick(5.0)
    service.assign("T-1", "dana")
    clock.tick(2.0)
    service.customer_reply("T-1")
    clock.tick(10.0)
    service.agent_reply("T-1")
    clock.tick(3.0)
    service.resolve("T-1")
    clock.tick(0.5)
    service.close("T-1")
    clock.tick(0.5)
    service.open_ticket("T-2", "cannot log in")
    clock.tick(1.0)
    service.resolve("T-2")
    clock.tick(0.5)
    service.open_ticket("T-3", "slow dashboard")
    clock.tick(45.0)
    service.assign("T-3", "omer")
    clock.tick(0.5)
    service.open_ticket("T-4", "database down")
    clock.tick(1.0)
    service.set_priority("T-4", "urgent")
    clock.tick(1.0)
    service.assign("T-4", "oncall")
    clock.tick(1.0)
    service.escalate("T-4")
    clock.tick(2.0)
    service.resolve("T-4")
    clock.tick(0.5)
    service.close("T-4")
    clock.tick(0.5)
    service.open_ticket("T-5", "typo on homepage")
    clock.tick(1.0)
    service.assign("T-5", "oncall")
    clock.tick(0.5)
    service.open_ticket("T-6", "feature question")
    clock.tick(1.0)
    service.assign("T-6", "lee")
    clock.tick(1.0)
    service.customer_reply("T-6")
    clock.tick(90.0)
    service.resolve("T-6")


# a probe DESIGNED to stress the analyzer where we suspect it is weakest:
# a module-level, non-event-type constant that participates in emission logic
PROBE_SOURCE = '''
from behave_rv.events.event import Event

READING = "sensor.reading"
LIMIT = 10


def within_limit(value):
    return value <= LIMIT


class Sensor:
    def __init__(self, emit, clock):
        self._emit = emit
        self._clock = clock

    def report(self, sensor_id, value):
        if within_limit(value):
            self._emit(Event(READING, self._clock(), {"sensor_id": sensor_id},
                             {"value": str(value)}, "sensors"))
'''


def run_probe_traffic(service, clock):
    for value in (5, 10, 11, 15):     # two on each side of LIMIT's boundary
        service.report("S-1", value)
        clock.tick(1.0)


def _ticketing_policies():
    steps_path = ROOT / "examples/ticketing/monitoring/steps.py"
    spec = importlib.util.spec_from_file_location("exp_ticketing_steps", steps_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    registry = module.build_registry()
    return module.load_policies(registry), registry


@dataclass
class Subject:
    name: str
    source: str
    traffic: object
    policies: list
    entries: list
    entity_key: str
    terminal: set


def subjects() -> list[Subject]:
    jobs_registry = build_jobs_registry()
    jobs_policies = compile_feature(JOBS_POLICIES, jobs_registry)
    ticketing_policies, ticketing_registry = _ticketing_policies()
    return [
        Subject("jobs", JOBS_SOURCE, run_jobs_traffic,
                jobs_policies, jobs_registry.entries(), "job_id", {"job.done"}),
        Subject("ticketing",
                (ROOT / "examples/ticketing/app_service.py").read_text(),
                run_ticketing_traffic, ticketing_policies,
                ticketing_registry.entries(), "ticket_id", {"ticket.closed"}),
        Subject("probe", PROBE_SOURCE, run_probe_traffic, [], [], "sensor_id", set()),
    ]


# ---------------------------------------------------------------------------
# the mutation operators (deterministic, exhaustive over applicable nodes)


def _docstring_ids(tree) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            out.add(id(body[0].value))
    return out


_COMPARE_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE,
                 ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt,
                 ast.In: ast.NotIn, ast.NotIn: ast.In}


def generate_mutants(source: str):
    """Yield (label, mutant_source). The baseline for comparison is
    ast.unparse(original), so formatting is identical and ONLY the mutation
    differs between the two sides."""
    tree = ast.parse(source)
    docstrings = {i for i, n in enumerate(ast.walk(tree))
                  if id(n) in _docstring_ids(tree)}

    def clone_nodes():
        clone = copy.deepcopy(tree)
        return clone, list(ast.walk(clone))

    for index, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.Constant) and index not in docstrings:
            if isinstance(node.value, str):
                clone, nodes = clone_nodes()
                nodes[index].value = node.value + "MUT"
                yield f"str@{node.lineno}:{node.value[:14]!r}", ast.unparse(clone)
            elif isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                clone, nodes = clone_nodes()
                nodes[index].value = node.value + 1
                yield f"num@{node.lineno}:{node.value}", ast.unparse(clone)
        elif isinstance(node, ast.Compare):
            swap = _COMPARE_SWAP.get(type(node.ops[0]))
            if swap is not None:
                clone, nodes = clone_nodes()
                nodes[index].ops[0] = swap()
                yield f"cmp@{node.lineno}", ast.unparse(clone)
        elif isinstance(node, ast.BoolOp):
            clone, nodes = clone_nodes()
            nodes[index].op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            yield f"bool@{node.lineno}", ast.unparse(clone)
        elif isinstance(node, ast.If):
            clone, nodes = clone_nodes()
            nodes[index].test = ast.UnaryOp(op=ast.Not(), operand=nodes[index].test)
            yield f"negif@{node.lineno}", ast.unparse(clone)

    # statement deletion (replace with pass), skipping defs/imports/docstrings
    all_nodes = list(ast.walk(tree))
    for parent_index, parent in enumerate(all_nodes):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for position, stmt in enumerate(body):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Import, ast.ImportFrom)):
                continue
            if position == 0 and isinstance(stmt, ast.Expr) \
                    and isinstance(stmt.value, ast.Constant):
                continue
            clone = copy.deepcopy(tree)
            clone_parent = list(ast.walk(clone))[parent_index]
            getattr(clone_parent, "body")[position] = ast.Pass()
            yield f"del@{stmt.lineno}", ast.unparse(clone)


# ---------------------------------------------------------------------------
# ground truth and detection


def observe(subject: Subject, app_source: str):
    """(stream, verdicts_by_policy, crashed). Stream includes ALL Event fields."""
    namespace = {"__name__": "exp_mutant"}
    exec(compile(app_source, "<exp_mutant>", "exec"), namespace)   # noqa: S102
    service_cls = next(v for v in namespace.values()
                       if isinstance(v, type)
                       and getattr(v, "__module__", "") == "exp_mutant")
    clock = _FakeClock()
    events = []
    service = service_cls(events.append, clock=clock)
    crashed = False
    try:
        subject.traffic(service, clock)
    except Exception:
        crashed = True
    stream = [(e.type, e.event_time, tuple(sorted(e.bindings.items())),
               tuple(sorted(e.payload.items())), e.source) for e in events]
    verdicts_by_policy: dict[str, set] = {}
    if subject.policies and not crashed:
        source = InProcessSource()
        for event in events:
            source.emit(event)
        engine = Engine(subject.policies, terminal_event_types=subject.terminal)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NoTerminalConfiguredWarning)
            for v in engine.run(source, emit_pending=True):
                verdicts_by_policy.setdefault(v.policy_id, set()).add(
                    (v.entity_key.get(subject.entity_key, "?"), v.verdict))
    return stream, verdicts_by_policy, crashed


def detect(old_source: str, new_source: str):
    with tempfile.TemporaryDirectory() as tmp:
        old_dir, new_dir = Path(tmp) / "old", Path(tmp) / "new"
        old_dir.mkdir(), new_dir.mkdir()
        (old_dir / "app.py").write_text(old_source)
        (new_dir / "app.py").write_text(new_source)
        return classify_app_changes(analyze_app([old_dir / "app.py"]),
                                    analyze_app([new_dir / "app.py"]))


def reported_policies(changes, subject: Subject):
    """The production scoping (shared by the CLI and the dashboard), reduced
    to the flat set this experiment compares against ground truth."""
    out: set = set()
    for change in changes:
        if change.status not in FLAGGING:
            continue
        scoped = policies_at_risk(change, subject.entries, subject.policies)
        if scoped is None:
            return {p.policy_id for p in subject.policies}   # conservative: all
        direct, coupled = scoped
        out |= set(direct) | set(coupled)
    return out


# ---------------------------------------------------------------------------
# the campaign


@dataclass
class SubjectResult:
    name: str
    total: int = 0
    crashed: int = 0
    tp: int = 0
    misses: list = None
    alarms: int = 0
    tn: int = 0
    scoped_ok: int = 0
    scoped_bad: list = None
    verdict_changing: int = 0


def run_subject(subject: Subject) -> SubjectResult:
    baseline = ast.unparse(ast.parse(subject.source))
    base_stream, base_verdicts, base_crash = observe(subject, baseline)
    assert not base_crash, f"{subject.name}: baseline traffic crashed"
    result = SubjectResult(subject.name, misses=[], scoped_bad=[])
    for label, mutant in generate_mutants(subject.source):
        result.total += 1
        stream, verdicts, crashed = observe(subject, mutant)
        changed = crashed or stream != base_stream
        result.crashed += crashed
        changes = detect(baseline, mutant)
        flagged = any(c.status in FLAGGING for c in changes)
        if changed and flagged:
            result.tp += 1
        elif changed:
            result.misses.append(label)
        elif flagged:
            result.alarms += 1
        else:
            result.tn += 1
        if subject.policies and not crashed and verdicts != base_verdicts:
            result.verdict_changing += 1
            affected = {p for p in set(base_verdicts) | set(verdicts)
                        if base_verdicts.get(p) != verdicts.get(p)}
            reported = reported_policies(changes, subject)
            if affected <= reported:
                result.scoped_ok += 1
            else:
                result.scoped_bad.append((label, sorted(affected - reported)))
    return result


def main() -> int:
    any_miss = False
    for subject in subjects():
        r = run_subject(subject)
        print(f"\n== {r.name}: {r.total} mutants "
              f"({r.crashed} crash-at-traffic, counted as stream-changing)")
        print(f"   stream changed & flagged (TP): {r.tp}")
        print(f"   stream changed & silent (MISS): {len(r.misses)}")
        for label in r.misses:
            print(f"     ✗ {label}")
        print(f"   stream same & flagged (upper-bound alarms): {r.alarms}")
        print(f"   stream same & silent (TN): {r.tn}")
        if r.verdict_changing:
            print(f"   scoping on {r.verdict_changing} verdict-changing mutants: "
                  f"{r.scoped_ok} sound, {len(r.scoped_bad)} unsound")
            for label, missing in r.scoped_bad:
                print(f"     ✗ {label}: affected-but-unreported {missing}")
        any_miss = any_miss or bool(r.misses) or bool(r.scoped_bad)
    return 1 if any_miss else 0


if __name__ == "__main__":
    raise SystemExit(main())
