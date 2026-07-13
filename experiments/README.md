# Experiments: one script per experiment, results committed

Each script re-executes one experiment family exactly and writes its results
under `results/` (the runtime-performance matrix keeps its results in
`demo/perf/results/`, where they always lived). Run everything in order with
`../run_experiments.sh`, or one family at a time:

| Script | What it measures | Artifact | Deterministic? |
|---|---|---|---|
| `run_semantic_conformance.sh` | engine vs independent oracle, 1,600 generated checks | `results/semantic_conformance.txt` | yes |
| `run_predicate_stability.sh` | 22 predicate-side revisions + raw-diff baseline | `results/predicate_stability.{txt,json}` | yes (byte-identical) |
| `run_app_curated.sh` | 22 application-side edit categories | `results/app_curated.{txt,json}` | yes (byte-identical) |
| `run_app_mutation.sh` | 619-mutant campaign: detection + policy scoping | `results/app_mutation.{txt,json}` | yes (byte-identical) |
| `run_history_replay.sh` | this repo's own app-file commits, classified | `results/history_replay.txt` | grows with history |
| `run_analysis_cost.sh` | analyser wall time/memory + slice statistics | `results/analysis_cost.txt` | timing varies by machine |
| `run_runtime_performance.sh` | engine throughput/memory matrix (Table 4) | `demo/perf/results/*.jsonl` | timing varies by machine |

The deterministic artifacts double as regression checks: rerunning the script
on the same code must reproduce the committed file byte for byte (`git diff`
stays clean). Timing artifacts are reference measurements: platform and
interpreter are recorded inside them, and a rerun overwrites them with your
machine's numbers. Every table in the published description is generated
from these artifacts; the logic-level expectations are additionally pinned
in the pytest suite, so a regression fails CI even if nobody reruns the
scripts.
