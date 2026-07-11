#!/usr/bin/env bash
# All three demo experiment matrices in sequence.
set -euo pipefail
cd "$(dirname "$0")"
./run_order.sh
./run_session.sh
./run_todo.sh
