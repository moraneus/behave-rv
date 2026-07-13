#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-uv run --with pytest --with hypothesis python3}"
mkdir -p experiments/results
# Experiment 1 -- semantic conformance: 1,600 generated engine-vs-oracle
# comparisons (500 direct, 500 adversarially reordered, 300 determinism,
# 300 entity independence). Deterministic given the pinned Hypothesis seeds
# in the test file; any counterexample fails the run.
$PY -m pytest tests/test_properties.py tests/test_end_to_end.py -q \
  | tee experiments/results/semantic_conformance.txt
