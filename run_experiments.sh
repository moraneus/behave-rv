#!/usr/bin/env bash
# Re-run every experiment behind the published numbers, from a fresh clone.
#
#   ./run_experiments.sh                 # all fast experiments (~2 minutes)
#   ./run_experiments.sh --with-tests    # also the full pytest suite
#   ./run_experiments.sh --with-perf     # also the runtime matrices (minutes)
#
# Each experiment family has its own script under experiments/ and writes its
# results there (see experiments/README.md). Deterministic experiments must
# reproduce their committed artifacts byte for byte; this script stops on the
# first failure, so a clean run IS the reproduction. The one long campaign
# NOT included is the engine's mutmut run (~hours; see MUTATION.md).
set -euo pipefail
cd "$(dirname "$0")"

export PY="${PY:-uv run --with pytest --with hypothesis python3}"
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

banner "1/7 semantic conformance: 1,600 generated engine-vs-oracle checks"
experiments/run_semantic_conformance.sh

banner "2/7 predicate-side stability: 22 revisions + raw-definition baseline"
experiments/run_predicate_stability.sh

banner "3/7 app-side curated catalogue: 22 edit categories, stream ground truth"
experiments/run_app_curated.sh

banner "4/7 adversarial mutation campaign: 619 mutants on 6 subjects (exit 1 on any miss)"
experiments/run_app_mutation.sh

banner "5/7 history replay: every app-file change in this repo's git log"
experiments/run_history_replay.sh

banner "6/7 analysis cost and fragment coverage"
experiments/run_analysis_cost.sh

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
  banner "extra: runtime performance matrices (3 demos x 3 trace sizes x P0..P5)"
  experiments/run_runtime_performance.sh
fi

printf '\nall experiments reproduced cleanly.\n'
