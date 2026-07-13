#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-uv run --with pytest --with hypothesis python3}"
mkdir -p experiments/results
# Experiment 6 -- git-history replay: every commit that modified an
# application file, classified by the analyser. Grows with history.
$PY -m tests.measure_app_history | tee experiments/results/history_replay.txt
