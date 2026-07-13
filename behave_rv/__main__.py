"""The behave_rv command line: run policies over a trace, and check stability.

Run a human-authored .feature policy against a recorded event stream:

    python -m behave_rv --steps <steps.py> --policy <policy.feature> --trace <trace.jsonl>

The steps module is the agent's monitorable surface: importing it registers the
RV steps into the default registry. The policy is compiled against that registry
and run over the recorded trace in replay mode. There is no Python policy
construction in the path -- the policy is the .feature file. Output: the verdict
log as JSON lines, followed by a rendered counterexample for every violation.

The specification-stability workflow (see STABILITY.md):

    python -m behave_rv catalog save --steps <steps.py> --catalog catalog.json \\
        [--app <app.py-or-dir> ...]
    python -m behave_rv catalog diff --steps <steps.py> --catalog catalog.json \\
        --policies <dir-or-.feature> [--app <app.py-or-dir> ...] \\
        [--trace <trace.jsonl>] [--owner <who>] [--fail-on-app-risk]

``save`` computes the current steps' catalog and writes the committed artifact;
with ``--app`` it also fingerprints the application's emit sites into the
catalog's app_surface section. ``diff`` recomputes against the current code,
diffs against the committed catalog, and prints the notifications: breaks
(with the contract diff and the affected policies via their recorded
used_step_ids), suggestions, and -- with ``--app`` -- the emit-site impact
analysis: app-side interface breaks and behavior risks, each scoped to the
policies observing that event type. With a representative ``--trace``,
compile-time liveness warnings (type- and value-level) print in the same
output. Exit codes: 0 clean, 1 breaks found (app behavior risks count only
under ``--fail-on-app-risk``), 2 usage or compile errors -- so ``catalog
diff`` can gate CI.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import uuid
import warnings
from pathlib import Path

from behave_rv.catalog.app_surface import (
    APP_REMOVED,
    BEHAVIOR_RISK,
    INTERFACE_BREAK,
    analyze_app,
    classify_app_changes,
    scope_step_ids,
)
from behave_rv.catalog.diff import classify_changes
from behave_rv.catalog.store import load_app_surface, load_catalog, save_catalog
from behave_rv.compile.compiler import (
    CompileError,
    UncheckablePolicyWarning,
    compile_feature,
)
from behave_rv.engine.loop import Engine
from behave_rv.events.sources.replay import ReplaySource
from behave_rv.notify.channel import notifications, uses_from_policies
from behave_rv.steps import default_registry
from behave_rv.verdict.explain import explain_verdict
from behave_rv.verdict.sinks import JsonSink


def _load_steps_module(path: str, registry=None):
    """Import a steps module so its taps land in ``registry`` (the default
    registry unless given). Two authoring styles are supported: modules that
    register at import time via the module-level decorators, and modules that
    stay side-effect free and expose ``build_registry()`` (the demo style) --
    for the latter, the built registry is copied in."""
    spec = importlib.util.spec_from_file_location(f"rv_steps_{uuid.uuid4().hex}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load steps module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = registry if registry is not None else default_registry
    if not target.entries() and hasattr(module, "build_registry"):
        module.build_registry().copy_into(target)
    return module


# -- run: policies over a recorded trace --------------------------------------


def run_command(args) -> int:
    _load_steps_module(args.steps)
    with open(args.policy, encoding="utf-8") as fh:
        policy_text = fh.read()

    try:
        policies = compile_feature(policy_text, default_registry)
    except CompileError as exc:
        print(f"compile error: {exc}", file=sys.stderr)
        return 2

    engine = Engine(policies)
    verdicts = engine.run(ReplaySource(args.trace), emit_pending=True)
    by_id = {p.policy_id: p for p in policies}

    print("# verdict log")
    sink = JsonSink(sys.stdout)
    for verdict in verdicts:
        sink.emit(verdict)

    # liveness harvest: which catalog steps were never seen in this stream?
    unobserved = default_registry.mark_observed(engine.observed_types)
    if unobserved:
        print("\n# liveness (steps never observed in this stream — possibly dead/wrong)")
        for entry in unobserved:
            print(f"  {entry.step_id}  (event {entry.signature.event_type!r})")

    violations = [v for v in verdicts if v.verdict == "violated"]
    if violations:
        print("\n# explanations (violations)")
        for verdict in violations:
            policy = by_id[verdict.policy_id]
            print()
            print(explain_verdict(verdict, policy.authored_scenario, policy.failing_step_index))

    return 0


# -- catalog: the stability workflow -------------------------------------------


def _policy_texts(path_args: list[str]) -> list[tuple[str, str]]:
    """(filename, text) for every .feature reachable from the given paths."""
    out = []
    for raw in path_args:
        path = Path(raw)
        files = sorted(path.glob("*.feature")) if path.is_dir() else [path]
        for f in files:
            out.append((str(f), f.read_text(encoding="utf-8")))
    return out


def _harvest_trace(trace_path: str) -> tuple[set, set]:
    """Observed event types and (type, field, value) triples from a stream."""
    types: set = set()
    values: set = set()
    for event in ReplaySource(trace_path).events():
        types.add(event.type)
        for field, value in event.payload.items():
            if isinstance(value, (str, int, float, bool)):
                values.add((event.type, field, str(value)))
    return types, values


def catalog_save_command(args) -> int:
    with default_registry.isolated() as registry:
        _load_steps_module(args.steps, registry)
        entries = registry.entries()
        if not entries:
            print(f"no steps registered by {args.steps}", file=sys.stderr)
            return 2
        app_surface = None
        if args.app:
            app_surface = analyze_app(args.app)
            if not app_surface:
                print("warning: no emit sites found under the given --app paths "
                      "(is behave_rv's Event imported and constructed there?)",
                      file=sys.stderr)
        save_catalog(args.catalog, entries, app_surface=app_surface)
    print(f"catalog: {len(entries)} step(s) written to {args.catalog}"
          + (f" ({len(app_surface)} app emit site(s))" if app_surface is not None else ""))
    return 0


def catalog_diff_command(args) -> int:
    committed = load_catalog(args.catalog)

    with default_registry.isolated() as registry:
        _load_steps_module(args.steps, registry)
        current = registry.entries()

        # the real policy-to-step dependency map, from the compiled policies
        uses = []
        deadline_policies: list[tuple[str, frozenset]] = []
        liveness: list[str] = []
        observed = None
        if args.trace:
            observed = _harvest_trace(args.trace)
        uncompilable: list[tuple[str, str]] = []
        for filename, text in _policy_texts(args.policies or []):
            try:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    policies = compile_feature(
                        text, registry,
                        observed_event_types=observed[0] if observed else None,
                        observed_values=observed[1] if observed else None)
                uses.extend(uses_from_policies(policies, owner=args.owner))
                deadline_policies.extend(
                    (p.policy_id, frozenset(p.correlation_key))
                    for p in policies if p.has_deadline)
                liveness += [str(w.message) for w in caught
                             if issubclass(w.category, UncheckablePolicyWarning)]
            except CompileError as exc:
                # a policy that no longer compiles IS a stability failure;
                # report it in the same output rather than dying on it
                uncompilable.append((filename, str(exc).splitlines()[0]))
                liveness.append(f"UNCOMPILABLE {filename}: {exc}")

    changes = classify_changes(committed, current)
    notes = notifications(committed, current, uses)

    # A policy that cannot compile has no used_step_ids to scope by; when the
    # diff also shows contract changes, that combination is a break in itself
    # (the policy demonstrably cannot be checked as written).
    from behave_rv.notify.channel import Break
    moved = [c.step_id for c in changes if c.status in ("changed", "removed")]
    for filename, reason in uncompilable:
        for step_id in moved:
            notes.breaks.append(Break(
                step_id=step_id, policy_id=filename, owner=args.owner,
                detail=f"policy no longer compiles: {reason}"))

    print(f"# catalog diff: {args.catalog} vs {args.steps}")
    for change in changes:
        print(f"  {change.step_id}: {change.status}")

    if notes.breaks:
        print(f"\n# BREAKS ({len(notes.breaks)}) — scoped to the policies that use the step")
        for b in notes.breaks:
            print(f"  ✗ {b.policy_id}  [{b.owner}]  via {b.step_id}")
            print(f"    contract: {b.detail}")
    if notes.suggestions:
        print(f"\n# suggestions ({len(notes.suggestions)})")
        for s in notes.suggestions:
            print(f"  + {s.step_id} ({s.phrasing!r}) — {s.detail}")
    if liveness:
        print(f"\n# liveness ({len(liveness)})"
              + (f" — against {args.trace}" if args.trace else ""))
        for message in liveness:
            print(f"  ! {message}")

    app_breaks, app_risks = _app_surface_diff(args, current, uses, deadline_policies)

    failures = len(notes.breaks) + len(app_breaks)
    if args.fail_on_app_risk:
        failures += len(app_risks)
    if failures:
        print(f"\nFAIL: {failures} break(s)"
              + (f" (of which {len(app_risks)} app behavior risk(s), promoted by "
                 "--fail-on-app-risk)" if args.fail_on_app_risk and app_risks else ""))
        return 1
    trailer = [f"{len(liveness)} liveness warning(s)"] if liveness else []
    trailer += [f"{len(app_risks)} app behavior risk(s)"] if app_risks else []
    print("\nok: no breaks" + (f" ({', '.join(trailer)} above)" if trailer else ""))
    return 0


def _app_surface_diff(args, current_entries, uses,
                      deadline_policies) -> tuple[list[str], list[str]]:
    """Diff the app's emit sites against the committed app surface and print the
    scoped report. Returns (breaks, risks) -- breaks always gate the exit code,
    risks only under --fail-on-app-risk."""
    if not args.app:
        return [], []
    current_sites = analyze_app(args.app)
    if not current_sites:
        print("\n# app surface: no emit sites found under the given --app paths "
              "(is behave_rv's Event imported and constructed there?)")
    committed_sites = load_app_surface(args.catalog)
    if committed_sites is None:
        print("\n# app surface: the committed catalog has no app_surface section; "
              "run 'catalog save --app ...' and commit it to enable this check")
        return [], []

    users_of: dict[str, list[str]] = {}
    for use in uses:
        for step_id in use.step_ids:
            users_of.setdefault(step_id, []).append(use.policy_id)

    changes = classify_app_changes(committed_sites, current_sites)
    print(f"\n# app surface diff ({len(changes)} emit site(s))")
    for change in changes:
        print(f"  {change.site_id}: {change.status}")

    breaks: list[str] = []
    risks: list[str] = []
    for change in changes:
        bucket = {INTERFACE_BREAK: breaks, APP_REMOVED: breaks,
                  BEHAVIOR_RISK: risks}.get(change.status)
        if bucket is None:
            continue
        site = change.new or change.old
        step_ids = scope_step_ids(change, current_entries)   # old AND new sides
        if step_ids is None:
            scope = "cannot scope: dynamic event type — review ALL policies"
        else:
            policies = sorted({p for sid in step_ids for p in users_of.get(sid, [])})
            scope = (f"policies at risk: {', '.join(policies)}" if policies
                     else "no compiled policy observes this event type")
            # deadline coupling: event-time advancement drives 'within' timers,
            # so ANY flagged site of the same entity can move their verdicts
            keys = frozenset(k for k in site.binding_keys if not k.startswith("<"))
            coupled = sorted({pid for pid, key in deadline_policies
                              if key == keys} - set(policies))
            if coupled:
                scope += ("\n      deadline policies on the same entity "
                          f"(event-time coupling): {', '.join(coupled)}")
        bucket.append(f"{change.site_id} (event {site.event_type!r})\n"
                      f"      {change.detail}\n      {scope}")

    if breaks:
        print(f"\n# APP BREAKS ({len(breaks)}) — the emitted interface changed")
        for line in breaks:
            print(f"  ✗ {line}")
    if risks:
        print(f"\n# APP BEHAVIOR RISKS ({len(risks)}) — logic on an emit path changed")
        for line in risks:
            print(f"  ! {line}")
    return breaks, risks


# -- entry ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv[:1] == ["catalog"]:
        parser = argparse.ArgumentParser(prog="behave_rv catalog")
        sub = parser.add_subparsers(dest="action", required=True)
        save = sub.add_parser("save", help="write the committed catalog for a steps module")
        save.add_argument("--steps", required=True, help="path to the RV steps module")
        save.add_argument("--catalog", required=True, help="path of the catalog artifact")
        save.add_argument("--app", nargs="*", default=[],
                          help="application .py files/dirs whose emit sites to fingerprint")
        diff = sub.add_parser("diff", help="diff current steps against the committed catalog")
        diff.add_argument("--steps", required=True, help="path to the RV steps module")
        diff.add_argument("--catalog", required=True, help="the committed catalog artifact")
        diff.add_argument("--policies", nargs="*", default=[],
                          help=".feature files or directories (for break scoping)")
        diff.add_argument("--trace", help="representative JSONL stream for liveness")
        diff.add_argument("--owner", default="policies", help="owner label for breaks")
        diff.add_argument("--app", nargs="*", default=[],
                          help="application .py files/dirs to diff against the committed "
                               "app surface (emit-site impact analysis)")
        diff.add_argument("--fail-on-app-risk", action="store_true",
                          help="exit 1 on app behavior risks too, not only breaks")
        args = parser.parse_args(argv[1:])
        try:
            if args.action == "save":
                return catalog_save_command(args)
            return catalog_diff_command(args)
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    parser = argparse.ArgumentParser(prog="behave_rv")
    parser.add_argument("--steps", required=True, help="path to the RV steps module")
    parser.add_argument("--policy", required=True, help="path to the .feature policy")
    parser.add_argument("--trace", required=True, help="path to the recorded JSONL stream")
    return run_command(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
