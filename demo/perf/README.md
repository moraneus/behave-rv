# Performance experiments: time and memory, per demo

Reproducible measurement of engine throughput and peak memory over traces of
1K / 10K / 100K events, for each demo, at policy counts P0..P5.

## Run

```bash
./demo/perf/run_all.sh          # everything (about 5 minutes)
./demo/perf/run_order.sh        # one demo's full 3x6 matrix
```

Setup from a fresh clone: any environment with behave_rv's dependencies
(`uv run python3` just works; or `pip install -e .` and `PY=python3`). The
perf experiments do not need Flask or any demo-webapp dependency. Each script
exits nonzero if any cell fails or a determinism check fails, prints a summary
table, and leaves machine-readable JSONL in `results/`.

## The traces

Generated once by `generate_traces.py`, seed **20260711** (in the file),
reusing each demo's real mock service: interleaved entities (a new entity
every 0.35s, flows lasting a few seconds each), the demo's own status flows
with a realistic normal/bug mix, and terminal events for roughly 75% of
order/session entities so GC is exercised. The todo demo has no terminal
event by design; its entities stay live (visible in the memory numbers).

Event times are monotone per entity; arrival order is perturbed with a seeded
jitter of ±0.2s so the reorder buffer does real work, against the engine's
default grace of 5.0s — zero late drops (throughput measurement, not a
correctness test).

Committed: the 1K and 10K files (~5 MB total) plus `CHECKSUMS.sha256` over
all nine. The three 100K files (~45 MB total) are gitignored; each run script
regenerates any missing file deterministically and verifies it against the
committed checksum, so reproducibility is byte-exact either way. Regenerate
everything with `python -m demo.perf.generate_traces --write-checksums`.

## The policy ladder (fixed forever; P3 always means the same three)

| | order | session | todo |
|---|---|---|---|
| P1 | 01 paid-after-auth (`before`) | 01 action-after-login (`before`) | 01 complete-after-start (`before`) |
| P2 | 06 refund-window (`within`) | 03 lock-after-fail (`previously`) | 06 due-window (`within`) |
| P3 | 08 never-double-charged (`never`) | 04 locked-never-acts (scoped `never`) | 08 archived-never-edited (scoped `never`) |
| P4 | 04 eventually-invoiced (`once`) | 06 locked-until-unlocked (`until`) | 12 sync-historically-ok (`historically`) |
| P5 | 11 flagged-only-reviewed (`since`) | 07 review-window (`within`) | 04 eventually-completed (`once`) |

P0 is the baseline: no policies registered — events flow through the source,
the reorder buffer, and dispatch with nothing to match, isolating pipeline
cost from evaluation cost.

## What is measured (and what is not)

- **Time**: `time.perf_counter` around `engine.run` only. Trace parsing,
  policy compilation, and engine construction are outside the timer; a fresh
  engine and pre-parsed in-memory source per repetition. Verdicts go to a
  null sink (no-op callable), so sink cost is constant and verdicts are never
  accumulated in a list. 5 timed repetitions per cell; median and min/max
  reported.
- **Memory**: `tracemalloc` peak — Python allocations, **not RSS** — from a
  SEPARATE single run per cell (tracemalloc adds real overhead; timing and
  memory runs are never mixed). tracemalloc starts before engine
  construction, so the pre-parsed trace itself is not counted; the peak is
  the engine's working set.
- **Determinism sanity**: the delivered-verdict count must be identical
  across all six runs of a cell (5 timed + 1 memory), or the cell fails.
- Engine configuration mirrors each demo's own harness: `order.done` /
  `session.end` as terminals; todo runs without terminals. Default grace
  (5.0s).

Each cell appends one JSON line to `results/<demo>.jsonl` with times, median,
events/s, peak MB, verdict count, timestamp, Python version, platform, and
the repo commit. `results/` is **committed** as the canonical reference run
(the report's numbers are these files); re-running overwrites locally.
