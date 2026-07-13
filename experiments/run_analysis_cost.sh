#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-uv run --with pytest --with hypothesis python3}"
mkdir -p experiments/results
# Experiment 7 -- static-analysis cost and fragment coverage. TIMING VARIES
# BY MACHINE: the committed artifact is the reference measurement; reruns
# overwrite it with your machine's numbers (platform is recorded below).
{
  echo "# platform: $(uname -sm), $($PY -c 'import platform; print("CPython", platform.python_version())')"
  $PY -m tests.exp_app_scaling
  echo
  $PY -m tests.exp_app_coverage
} | tee experiments/results/analysis_cost.txt
