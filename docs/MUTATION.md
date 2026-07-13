# Mutation-testing campaign

Date: 2026-07-11. Tool: `mutmut` 3.6.0. Target: the `behave_rv` package;
the kill signal is the full core test suite (`tests/`, 231 tests including
the 62-plus added by this campaign's triage).

## Reproduce

```
uv run --with mutmut --with pytest --with hypothesis --with toml mutmut run
uv run --with mutmut --with toml mutmut results
```

Configuration is committed in `pyproject.toml` under `[tool.mutmut]`:
`behave_rv/` is mutated; `behave_rv/expose/*` (an unimplemented design stub)
and `behave_rv/vendor_behave/*` (a docstring-only boundary marker) are not;
`examples/` is copied into the sandbox because the CLI tests read it.

## Result

**Final full run: 1873 mutants — 1673 killed, 189 survived, 2 timeouts,
9 skipped. Kill rate 89.9% (killed + timeouts, over the 1864 checked).**

Every one of the 189 survivors is classified below: 42 CLI glue, 68
diagnostic strings, 79 argued equivalents. The final run's survivor set was
reconciled against this classification name-by-name: zero unexplained.

The campaign took four full runs plus named-mutant confirmation runs:

| Run | Killed | Survived | Between runs |
| --- | --- | --- | --- |
| 1 (baseline) | 1514 | 347 | triage -> 39 tests for the engine, live loop, compiler messages |
| 2 | 1596 | 265 | 28 more tests for the smaller modules; 4 missed targets fixed |
| 3 | 1650 | 212 | seed-flip pins (see below) + rendering/condition contract tests |
| 4 (final) | 1673 | 189 | — |

**Seed-dependent kills.** A handful of mutants flipped between killed and
survived across runs because the property-based tests draw randomized
examples: their kills were luck of the seed. Each flip was treated as a
finding — the behavior only chance was checking (`since`/`historically`
settling satisfied at a terminal, the satisfied `previously` verdict's
deciding evidence) got a deterministic pinning test. Stopping rule: a
post-final flip gets pinned or classified with a note, not another full
cycle.

Triage produced 62 new tests in `tests/test_mutation_gaps.py` plus
assertion extensions in `tests/test_notifications.py`, each naming the
mutant ids it kills.

## Survivor triage

Every surviving mutant was classified one of three ways: **killed** by a new
test added during triage, **equivalent** (no observable behavior difference,
with the argument), or **out-of-scope** (code whose fidelity is not part of
any verdict contract, stated plainly). No classification weakens a test or a
spec; where a new test contradicted the engine, the spec won (see the
`within` tie rule below).

### Killed by new tests (the real gaps the campaign exposed)

| Area | Gap the mutants exposed | Killing test |
| --- | --- | --- |
| dispatch loop | a policy missing its key could halt dispatch to later policies (`continue`->`break`) | `test_dispatch_continues_past_a_policy_missing_its_key` |
| terminal retirement | keyless / never-instantiated policies ahead in order could stop settlement of the rest | `test_terminal_settles_every_policy_for_the_entity` |
| verdict record | `Verdict.trigger_event` was asserted on NO settlement path (event, timer, terminal, emit_pending) | `test_verdicts_carry_the_trigger_event` |
| timer wheel | a stale deadline (retired instance) ahead in the heap could stop later due deadlines | `test_stale_deadline_timers_do_not_stop_later_due_deadlines` |
| quiescence TTL | refresh validation, exact-boundary reclaim, reclaim counters and keys were all unpinned | the three `test_ttl_*` tests |
| error plumbing | collector restoration after `run()`, sink-error sources, predicate-error policy attribution, `PredicateError.original` | `test_engine_run_restores_the_predicate_error_collector`, `test_sink_error_log_records_the_policy_source`, `test_monitor_internal_error_is_logged_with_policy_and_original`, `test_report_predicate_error_without_collector_reports_uncollected` |
| monitor semantics | `since` anchoring on non-anchor events; early `on_timeout` guard | `test_since_stays_inactive_before_its_anchor`, `test_within_on_timeout_before_the_deadline_is_not_due` |
| grace = 0 contract | arrival order (not canonical order) for equal timestamps; late/invalid bookkeeping on the bufferless path | `test_grace_zero_keeps_arrival_order_and_its_bookkeeping` |
| live-path gating | a batch source carrying a `next_event` attribute must not be pulled as live | `test_batch_sources_with_a_next_event_attribute_stay_batch` |
| live loop | out-of-order events regressing the wall anchor; premature/never-maturing wall fires; virtual-clock overshoot flagging on-time events late | the two `test_live_*` tests |
| programmatic API | composite (tuple) correlation keys and constructor field wiring were never exercised | `test_programmatic_constructors_carry_their_fields` |
| compiler messages | the refusal texts quoted verbatim in the README, and the role labels in unresolved-step errors, were never asserted | the four `test_refusal/unresolved/ambiguous_*` tests |
| value liveness | warnings with only `observed_values` supplied; multi-step policies checked past the first step | the two `test_value_liveness_*` tests |

Plus assertion extensions in `tests/test_notifications.py` (suggestion
semantics, owners, details) and the small contract tests for sinks, replay,
subscription, registry entries, catalog store round-trip, explanation marks
and `safe_value` boundaries described below — see the file for the mutant ids.

### Equivalent (unkillable, with the argument)

| Mutants | Why no test can observe them |
| --- | --- |
| `Engine.__init__` counter/log initializers (18) | dead stores: `run()` re-initializes every one of them before any read; `live_instances` is assigned at the end of `run()` |
| `run` `getattr(source, "live", None)` | the default only matters when the attribute is missing, and `None` and `False` are equally falsy |
| `_handle_event` `_reschedule(instance, None, ...)` | `_reschedule` never reads its `instances` parameter |
| `_run_live` anchor init `None` -> `""` | unreachable: targets cannot be non-empty before the first event, and the first event overwrites the anchor |
| `_run_live` wait/anchor skews (5: estimate minus-elapsed, wait floor 1.0, `>=` anchor refreshes) | they shift a wall fire by a bounded sub-second amount; the wall contract is "fires after the deadline matures", with no exact-instant promise a deterministic test could pin without being flaky |
| monitor `on_terminal` `settled = None/False` (12) | the instance is popped from the table before `on_terminal` is called; `settled` is never read again |
| monitor falsy-state swaps (`False` -> `None`, 7) | the fields are read only for truthiness |
| monitor sentinel swaps (`None` -> `""`, 5) | the `""` is overwritten (or the reading path is unreachable) before any decision that could expose it |
| `SinceMonitor` `_started` premature set (2: the `and` -> `or`, the init `True`) | `_started` is implied by `_s`: whenever the chain is live, correct code has already set it, so the violation condition (`_started and _s and not new_s`) cannot diverge |
| `WithinMonitor.on_event` `<=` -> `<` at the deadline | engine-unreachable: due timers fire before the event dispatches, so `on_event` never sees a response with `event_time >= deadline`; the documented tie rule (SEMANTICS.md "Deadline boundary": the timeout wins) is enforced by the timer path and now pinned by `test_within_response_exactly_at_the_deadline_loses_the_tie` |
| `_reschedule` TTL at `last_activity - ttl` | the reclaim guard re-validates `now - last_activity >= ttl` against live state, which subsumes the schedule time; the only effect is heap churn |
| watermark/timers sequence-counter tweaks (7) | the counter only breaks exact heap ties, and a tie requires identical `(time, type, bindings, payload, source)` — value-equal events whose mutual order is unobservable; `Event.__eq__` resolves the heap comparison without ordering, so not even a crash distinguishes them |
| watermark `_tiebreak` without bindings | the bindings component only orders same-time events of *different* entities, and per-key isolation makes that order unobservable |
| `advance_clock` `>` -> `>=` | equality re-assigns the same value |
| `flush` watermark `None` | the buffer's lifecycle ends at flush; no admission decision follows |
| file-encoding defaults (11) | `encoding="utf-8"` vs. the platform default is untestable on a UTF-8 host; noted as a portability caveat, not a covered behavior (the `indent`/`sort_keys` format mutants, by contrast, ARE killed: the diffable-artifact byte format is asserted) |
| warning `stacklevel` tweaks (4) | warning-attribution cosmetics |
| `QueueSource.__init__` `_closed = None` | falsy-for-falsy |
| `float("-inf")` case tweak | `float()` parses case-insensitively; the value is identical |

### Out-of-scope (stated plainly)

| Mutants | Why |
| --- | --- |
| `behave_rv/__main__.py` glue (25 logic + its strings) | CLI argument wiring and console formatting; the CLI's *behavior* (load, run, verdict output, exit code) is covered by `test_cli.py`, and the surviving mutants alter only help/print cosmetics |
| human-facing diagnostic strings (most of the 122 string-only survivors) | log labels, error prose, and message fragments that no verdict or documented refusal depends on; the message content that IS documented (the README's refusal table, unresolved-step role labels, explanation marks) is now asserted and those mutants are killed |

## What triage changed in the spec's favor

One triage test initially asserted that a `within` response arriving exactly
at the deadline is satisfied. SEMANTICS.md says the opposite ("the timeout
wins the tie") and the independent oracle agrees, so the TEST was corrected,
the boundary is now pinned by a regression test, and the corresponding
monitor-internal mutant is classified equivalent (engine-unreachable). The
spec was not weakened.
