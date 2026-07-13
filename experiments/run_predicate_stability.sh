#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-uv run --with pytest --with hypothesis python3}"
mkdir -p experiments/results
# Experiment 2 -- predicate-side stability: 22 controlled revisions with
# replayed-verdict ground truth, plus the raw-definition baseline row.
# Output is deterministic: a rerun must produce a byte-identical artifact.
$PY -m tests.stability_catalog --out experiments/results/predicate_stability.json \
  | tee experiments/results/predicate_stability.txt
