"""Coverage experiment: fragment resolvability and slice tightness on real
code (RQ6 of docs/APP_SURFACE_EVALUATION.md).

For every real application file in the repository: how many functions and
emit sites; how large the slices are relative to the module (a slice close
to 100% of the code would make every warning uninformative -- the precision
argument depends on this number); and how much of the call surface the
resolver declares unresolvable (the visible boundary of the analysis).

Run:  python -m tests.exp_app_coverage
"""

from __future__ import annotations

import ast
from pathlib import Path

from behave_rv.catalog.app_surface import DYNAMIC, SPLAT, analyze_app

ROOT = Path(__file__).resolve().parents[1]

REAL_FILES = [
    "examples/ticketing/app_service.py",
    "demo/order_service/service.py",
    "demo/session_service/service.py",
    "demo/todo_app/service.py",
]


def _function_count(path: Path) -> int:
    return sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               for node in ast.walk(ast.parse(path.read_text())))


def main() -> int:
    print(f"{'file':34} {'funcs':>5} {'sites':>5} {'slice min/mean/max':>19} "
          f"{'in-slice%':>9} {'unresolved':>10} {'markers':>7}")
    for rel in REAL_FILES:
        path = ROOT / rel
        sites = analyze_app([path])
        functions = _function_count(path)
        sizes = [len(s.slice_functions) for s in sites]
        in_any = len(set().union(*(s.slice_functions.keys() for s in sites)))
        unresolved = sorted(set().union(*(s.unresolved_calls for s in sites)))
        markers = sum(DYNAMIC in (s.binding_keys + s.payload_fields
                                  + [s.event_type])
                      or SPLAT in s.payload_fields for s in sites)
        mean = sum(sizes) / len(sizes)
        print(f"{rel.split('/', 1)[1]:34} {functions:>5} {len(sites):>5} "
              f"{min(sizes):>7}/{mean:>4.1f}/{max(sizes):<4} "
              f"{100 * in_any / functions:>8.0f}% {len(unresolved):>10} "
              f"{markers:>7}")
        print(f"{'':34}   unresolved: {', '.join(unresolved) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
