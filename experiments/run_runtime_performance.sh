#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-uv run --with pytest --with hypothesis python3}"
mkdir -p experiments/results
# Experiment 8 -- engine runtime cost (Table 4): the full 3-demo x 3-size x
# P0..P5 matrix. TIMING VARIES BY MACHINE; results land in demo/perf/results/
# with platform metadata per row. Takes ~5 minutes.
PY="$PY" demo/perf/run_all.sh
