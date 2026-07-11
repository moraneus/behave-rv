#!/usr/bin/env bash
# The full todo-demo experiment matrix: 3 trace sizes x P0..P5.
# Setup (from a fresh clone): uv run python3 ... just works, or activate any
# env with behave_rv's dependencies (pip install -e .). No demo webapp deps
# (Flask) are needed for the perf experiments.
set -euo pipefail
cd "$(dirname "$0")/../.."

PY="${PY:-uv run python3}"
OUT="demo/perf/results/todo.jsonl"
: > "$OUT" 2>/dev/null || mkdir -p demo/perf/results && : > "$OUT"

# the gitignored 100k trace regenerates deterministically (see CHECKSUMS.sha256)
$PY -m demo.perf.generate_traces --only-missing
grep "todo_100k" demo/perf/traces/CHECKSUMS.sha256 | (cd demo/perf/traces && shasum -a 256 -c -) \
  || { echo "checksum mismatch on regenerated trace"; exit 1; }

for size in 1k 10k 100k; do
  for p in 0 1 2 3 4 5; do
    $PY -m demo.perf.run_experiment --demo todo \
      --trace "demo/perf/traces/todo_${size}.jsonl" \
      --policies "$p" --reps 5 --out "$OUT"
  done
done

echo
echo "== todo: summary (median s / events per s / peak MB) =="
$PY - <<'PYEOF'
import json
rows = [json.loads(l) for l in open("demo/perf/results/todo.jsonl")]
sizes = sorted({r["trace_size"] for r in rows})
print(f"{'events':>8} " + "".join(f"{'P'+str(p):>26}" for p in range(6)))
for s in sizes:
    cells = []
    for p in range(6):
        r = next(x for x in rows if x["trace_size"] == s and x["policy_count"] == p)
        cells.append(f"{r['median_s']:.3f}s/{r['events_per_s']}/{r['peak_mb']}MB".rjust(26))
    print(f"{s:>8} " + "".join(cells))
PYEOF
