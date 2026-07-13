"""Scaling experiment: analysis cost versus application size (RQ5 of
docs/APP_SURFACE_EVALUATION.md).

Synthetic applications are generated with N service classes, each with a
constructor, an emit helper, D layers of business methods calling downward
into the helper, and one pure non-emitting function per class -- the shape of
the real subjects, scaled. For each size: wall time of ``analyze_app``
(median of 5 runs) and peak allocation (``tracemalloc``, separate run so
instrumentation does not pollute the timing). Real repository files are
measured the same way for grounding.

Run:  python -m tests.exp_app_scaling
"""

from __future__ import annotations

import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path

from behave_rv.catalog.app_surface import analyze_app

ROOT = Path(__file__).resolve().parents[1]

REAL_FILES = [
    "examples/ticketing/app_service.py",
    "demo/order_service/service.py",
    "demo/session_service/service.py",
    "demo/todo_app/service.py",
]


def synthetic_app(classes: int, depth: int) -> str:
    """One module, ``classes`` services, each with ``depth`` call layers above
    its emit helper. Functions per class: depth + 3 (init, helper, pure)."""
    lines = ["from behave_rv.events.event import Event", ""]
    for c in range(classes):
        lines += [f'TYPE_{c} = "svc{c}.status"', f"LIMIT_{c} = {c}"]
    for c in range(classes):
        lines += [
            "",
            f"class Service{c}:",
            "    def __init__(self, emit, clock):",
            "        self._emit = emit",
            "        self._clock = clock",
            "",
            "    def _tap(self, key, status):",
            f"        self._emit(Event(TYPE_{c}, self._clock(),"
            " {'entity_id': key}, {'status': status}, 'synthetic'))",
        ]
        for d in range(depth):
            callee = "_tap" if d == 0 else f"layer_{d - 1}"
            lines += [
                "",
                f"    def layer_{d}(self, key, value):",
                f"        if value > LIMIT_{c}:",
                f"            self.{callee}(key, 'level_{d}')",
            ]
        lines += [
            "",
            f"    def summary_{c}(self, count):",
            f"        return f'{{count}} items in svc{c}'",
        ]
    return "\n".join(lines) + "\n"


def measure(source_or_path, label: str, functions: int | None = None):
    if isinstance(source_or_path, Path):
        target = source_or_path
        run = lambda: analyze_app([target])   # noqa: E731
        times = [_timed(run) for _ in range(5)]
        sites = len(analyze_app([target]))
        peak = _peak(run)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "app.py"
            target.write_text(source_or_path)
            run = lambda: analyze_app([target])   # noqa: E731
            times = [_timed(run) for _ in range(5)]
            sites = len(analyze_app([target]))
            peak = _peak(run)
    print(f"{label:34} {functions if functions is not None else '':>6} "
          f"{sites:>6} {statistics.median(times) * 1000:>10.1f} {peak / 1e6:>9.1f}")


def _timed(run) -> float:
    start = time.perf_counter()
    run()
    return time.perf_counter() - start


def _peak(run) -> int:
    tracemalloc.start()
    run()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def main() -> int:
    print(f"{'subject':34} {'funcs':>6} {'sites':>6} {'median ms':>10} {'peak MB':>9}")
    for path in REAL_FILES:
        measure(ROOT / path, path.split('/')[-2] + "/" + path.split('/')[-1])
    for classes, depth in [(5, 4), (20, 4), (50, 4), (100, 4), (200, 4),
                           (50, 2), (50, 8), (50, 16)]:
        functions = classes * (depth + 3)
        measure(synthetic_app(classes, depth),
                f"synthetic C={classes} D={depth}", functions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
