"""Run a human-authored .feature policy against a recorded event stream.

    python -m behave_rv --steps <steps.py> --policy <policy.feature> --trace <trace.jsonl>

The steps module is the agent's monitorable surface: importing it registers the
RV steps into the default registry. The policy is compiled against that registry
and run over the recorded trace in replay mode. There is no Python policy
construction in the path -- the policy is the .feature file.

Output: the verdict log as JSON lines on stdout, followed by a rendered
counterexample for every violation (the authored scenario with the failing step
marked and the real event values).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import uuid

from behave_rv.compile.compiler import CompileError, compile_feature
from behave_rv.engine.loop import Engine
from behave_rv.events.sources.replay import ReplaySource
from behave_rv.steps import default_registry
from behave_rv.verdict.explain import explain_verdict
from behave_rv.verdict.sinks import JsonSink


def _load_steps_module(path: str) -> None:
    spec = importlib.util.spec_from_file_location(f"rv_steps_{uuid.uuid4().hex}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load steps module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="behave_rv")
    parser.add_argument("--steps", required=True, help="path to the RV steps module")
    parser.add_argument("--policy", required=True, help="path to the .feature policy")
    parser.add_argument("--trace", required=True, help="path to the recorded JSONL stream")
    args = parser.parse_args(argv)

    _load_steps_module(args.steps)
    with open(args.policy, encoding="utf-8") as fh:
        policy_text = fh.read()

    try:
        policies = compile_feature(policy_text, default_registry)
    except CompileError as exc:
        print(f"compile error: {exc}", file=sys.stderr)
        return 2

    verdicts = Engine(policies).run(ReplaySource(args.trace), emit_pending=True)
    by_id = {p.policy_id: p for p in policies}

    print("# verdict log")
    sink = JsonSink(sys.stdout)
    for verdict in verdicts:
        sink.emit(verdict)

    violations = [v for v in verdicts if v.verdict == "violated"]
    if violations:
        print("\n# explanations (violations)")
        for verdict in violations:
            policy = by_id[verdict.policy_id]
            print()
            print(explain_verdict(verdict, policy.authored_scenario, policy.failing_step_index))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
