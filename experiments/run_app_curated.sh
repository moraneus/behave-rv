#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-uv run --with pytest --with hypothesis python3}"
mkdir -p experiments/results
# Experiment 3 -- application-side curated catalogue: 22 edit categories with
# executed stream + verdict ground truth. Deterministic artifact.
$PY -m tests.stability_app_surface --out experiments/results/app_curated.json \
  | tee experiments/results/app_curated.txt
