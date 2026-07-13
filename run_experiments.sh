#!/usr/bin/env bash
# Re-run every experiment behind the published numbers, from a fresh clone.
#
#   ./run_experiments.sh                 # all fast experiments (~1 minute)
#   ./run_experiments.sh --with-tests    # also the full pytest suite
#   ./run_experiments.sh --with-perf     # also the time/memory matrices (minutes)
#
# Everything is deterministic: same code -> same tables. The two mutation
# harnesses exit non-zero on any missed detection or unsound scoping, and this
# script stops on the first failure -- so a clean run IS the reproduction.
#
# Works with any environment holding behave_rv's dependencies (pip install -e .)
# via PY=python3, or with uv out of the box (the default below). The one
# long-running campaign NOT included is the engine's mutmut run (~hours; see
# MUTATION.md for how it was produced).
set -euo pipefail
cd "$(dirname "$0")"

PY="${PY:-uv run --with pytest --with hypothesis python3}"
WITH_TESTS=0
WITH_PERF=0
for arg in "$@"; do
  case "$arg" in
    --with-tests) WITH_TESTS=1 ;;
    --with-perf)  WITH_PERF=1 ;;
    *) echo "unknown option: $arg (known: --with-tests --with-perf)"; exit 2 ;;
  esac
done

banner() { printf '\n================ %s ================\n\n' "$1"; }

banner "1/7 step-side stability: 22 code-change cases, verdict-replay ground truth"
$PY -m tests.stability_catalog

banner "2/7 app-side stability (E-series): 17 code-change cases, stream ground truth"
$PY -m tests.stability_app_surface

banner "3/7 adversarial mutation campaign: 83 mutants, detection + scoping (exit 1 on any miss)"
$PY -m tests.exp_app_mutation

banner "4/7 history replay: every app-file change in this repo's git log"
$PY -m tests.measure_app_history

banner "5/7 analysis cost: real files + synthetic apps up to 1400 functions"
$PY -m tests.exp_app_scaling

banner "6/7 fragment coverage and slice tightness on the real services"
$PY -m tests.exp_app_coverage

banner "7/7 the CI stability gates: committed catalogs vs the code (exit 1 on breaks)"
$PY -m behave_rv catalog diff \
  --steps demo/order_service/steps.py \
  --catalog demo/order_service/catalog.json \
  --policies demo/order_service/policies
$PY -m behave_rv catalog diff \
  --steps examples/ticketing/monitoring/steps.py \
  --catalog examples/ticketing/monitoring/catalog.json \
  --policies examples/ticketing/monitoring/policies \
  --app examples/ticketing/app_service.py \
  --fail-on-app-risk

if [ "$WITH_TESTS" -eq 1 ]; then
  banner "extra: the full test suite (the tables above are also pinned in it)"
  $PY -m pytest tests/ demo/ -q
fi

if [ "$WITH_PERF" -eq 1 ]; then
  banner "extra: time/memory matrices (3 demos x 3 trace sizes x P0..P5)"
  PY="$PY" demo/perf/run_all.sh
fi

printf '\nall experiments reproduced cleanly.\n'
