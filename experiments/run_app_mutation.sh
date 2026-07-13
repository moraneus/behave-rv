#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-uv run --with pytest --with hypothesis python3}"
mkdir -p experiments/results
# Experiment 4+5 -- the adversarial mutation campaign (detection + scoping):
# every mutant the operator set generates on six subjects, executed for
# ground truth. Exits 1 on any miss or unsound scoping. Deterministic.
$PY -m tests.exp_app_mutation --out experiments/results/app_mutation.json \
  | tee experiments/results/app_mutation.txt
