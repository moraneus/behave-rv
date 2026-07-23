# Experiments: what was measured, how, and the full results

This is the complete record of every experiment behind behave_rv's measured
claims, ordered from semantic correctness through change detection to cost.
Three principles hold throughout:

1. **Ground truth is executed, never assumed.** Whenever an experiment asks
   "did behavior change?", the changed program is actually run under fixed
   traffic and its emitted events are compared with the original's. Whenever
   it asks "was the verdict right?", an independent oracle or a replayed
   verdict set supplies the answer.
2. **Everything is deterministic and committed.** Each experiment has its own
   script under [`experiments/`](../experiments/) and writes its results there.
   The logic-level artifacts are byte-identical across reruns, so
   `git diff` after a rerun is itself the regression check; the expectations
   are additionally pinned in the pytest suite, so a regression fails CI.
3. **Failures are reported, not retrofitted away.** Two experiments found
   real defects in the analyser (five in total). Each was fixed or absorbed
   into a declared conservative rule, pinned with a regression test, and the
   before/after story is part of the record below.

Reproduce everything with `./run_experiments.sh` (add `--with-tests`,
`--with-perf`), or one family at a time with the scripts named per section.

---

## What is a "mutant"? (the idea behind the mutation campaign)

Hand-picked test cases only probe the failures their author imagined. A
**mutant** removes the author: a program mechanically applies one small,
plausible sabotage to the application source - flip a comparison, change a
constant, delete a statement, negate or remove a guard, swap two arguments -
producing one changed program per possible edit. Every mutant is then put
through the same two steps:

1. **Execute it** under the fixed scripted traffic and compare the complete
   emitted event stream (type, time, bindings, payload, source) with the
   original's. This decides, objectively, whether the sabotage changed
   observable behavior.
2. **Analyse it** statically: the emit-site analyser diffs the original and
   mutated source trees, with no knowledge of step 1's outcome.

A mutant that changed the stream but was not flagged is a **miss** - the
failure mode the whole mechanism exists to prevent. A concrete example, a
real mutant from the campaign (`cmp` operator on the jobs subject):

```python
# original                                  # mutant cmp@39
def submit(self, job_id, name):             def submit(self, job_id, name):
    title = clean_name(name)                    title = clean_name(name)
    if len(title) > MIN_LEN:                    if len(title) <= MIN_LEN:
        self._status(job_id, "queued")              self._status(job_id, "queued")
```

What happens to it: executed under traffic, jobs that used to emit
`"queued"` no longer do (and blank names now do), so the streams differ -
ground truth says *behavior changed*. Independently, the analyser sees that
`submit`'s normalized body hash moved, `submit` sits in the dependency slice
of the `job.status` emit site, so the site is classified `behavior-risk`,
naming `submit` and listing the policies observing `job.status`. The mutant
counts as detected. Had the analyser stayed silent, the run would end with
exit code 1 and name the mutant.

Some mutants change code that the fixed traffic never exercises - for
example, sabotaging a UI-driven `act()` method that the scripted flows never
call. Their streams are unchanged, yet the analyser flags them. These are
counted against the tool as "stream-preserving flagged" (an upper bound on
false alarms), but note what they really are: real emission-code changes
that replay *cannot* expose - precisely the gap the static check exists to
cover.

---

## Experiment 1 - semantic conformance

**Question.** When the engine says `satisfied`/`violated`/`pending`, is that
the answer the formal semantics demands?

**Method.** An independent oracle (`tests/oracle.py`) shares only the event
data class with the engine and re-implements trace ordering, deadlines,
lifecycle settlement, and every operator directly from the semantics.
Hypothesis generates random policies and traces; both judges rule; any
disagreement is a counterexample. Four suites: 500 direct engine-vs-oracle
comparisons, 500 adversarial arrival-order permutations with no event late
(the verdict must be arrival-order invariant), 300 determinism runs, 300
per-entity independence runs - 1,600 generated checks per run, covering
every operator family plus targeted lifecycle and predicate-error cases.

**Result:** no counterexample in the committed suite.

```
experiments/run_semantic_conformance.sh   → experiments/results/semantic_conformance.txt
```

The engine implementation was additionally hardened by a separate
1,873-mutant campaign over the engine code itself (89.9% killed, every
survivor classified); the full campaign record, including every
survivor's classification, is [MUTATION.md](MUTATION.md).

## Experiment 2 - predicate-side stability

**Question.** When the *monitoring* code changes, does the catalogue tell
harmless edits apart from policy-breaking ones - and what does the machinery
buy over a naive comparison?

**Method.** 22 controlled revisions to the order-service baseline (11
policies): 6 representational changes, 12 behavior-changing revisions, 4
behavior-preserving refactorings. Ground truth: the same seeded-fault trace
replayed through baseline and revision, verdict sets compared. The
raw-definition baseline (phrasing + dispatch metadata + exact predicate
source, no stable identities, no normalization) takes the same exam.

**Result** (both rows produced by the committed harness):

```
method               detected  missed  silent  false-alarms
raw-definition diff         7       5       2             8
behave_rv                  11       1       6             4
```

The one behave_rv miss is the documented C4b boundary (a helper reached
through a function value), visible per step in `unresolved_calls`. The four
alarms are structural refactorings the fingerprint cannot prove equivalent.

```
experiments/run_predicate_stability.sh    → experiments/results/predicate_stability.{txt,json}
```

## Experiment 3 - application-side curated catalogue

**Question.** For each *category* of realistic application edit, does the
emit-site analyser do the intended thing - including the intended silences
and the intended alarms?

**Method.** 22 hand-constructed edit categories, one case each:
comment/docstring edits, local/class renames, guard and helper-logic changes
(including two calls deep), an emitted value renamed, payload-field and
event-type changes, emission deletion/addition/reordering, module- and
class-level constants, an exception-path emission, a loop emission, a
ternary-guarded value, a behavior-changing decorator, and emission logic in
a second module. Ground truth per run: the emitted stream (with the policy
verdict set verified alongside).

**Result:** 18 correct, 0 misses, 4 false alarms - all four in the declared
by-design conservatism family (emit-path function rename, extract-method,
event type becoming computed, constructor edit), asserted in the test suite
to be exactly that family. Stream-changing cases: 14/14 caught.

Case E21 (decorator) is the record of the **fifth found defect**: decorators
were stripped from app-side hashes as registration boilerplate, so a wrapper
edited to suppress the call was invisible - verified as a genuine pre-fix
miss (detection `silent` before the fix, `risk` after). The fix keeps
decorators in the hash and joins the decorator's body to the decorated
function's slice.

```
experiments/run_app_curated.sh            → experiments/results/app_curated.{txt,json}
```

## Experiment 4 - the adversarial mutation campaign

**Question.** Is there *any* small application edit that changes observable
behavior but slips past the analyser silently?

**Method.** Eight operator families (string/numeric/boolean constant
perturbation, comparison swap, boolean-operator swap, condition negation,
guard removal, positional-argument swap, statement deletion) applied
exhaustively - every mutant the operators generate at every location - to
six subjects: the jobs baseline (two modules), the ticketing example (six
policies), a module-constant probe built to stress a suspected weakness, and
the three demonstration services with their full real policy sets (11, 10,
and 4 policies). 619 mutants, each executed for stream ground truth and
independently analysed.

**Result:**

```
subject     mutants  crash  changed,flagged  missed  same,flagged  same,silent
jobs             64     13               60       0             2            2
ticketing        52      6               52       0             0            0
probe            16      4               16       0             0            0
order           133      6               86       0             6           41
session         179     11              116       0            18           45
todo            175      6              113       0             5           57
total           619     46              443       0            31          145
```

All **443 stream-changing mutants are flagged; zero misses**. Of the 176
stream-preserving mutants, 145 are correctly silent and 31 are flagged -
and on inspection every one of the 31 affects emission behaviour the
scripted traffic does not distinguish: UI-driven `act()`/manual-unlock
entry points the flows never call, the lockout-counter reset, and boundary
constants whose traffic values straddle the change. These are real
emission-code changes that replay cannot expose - the traffic-relativity
caveat made concrete. Each is listed by name in the JSON artifact.

**The hardening record.** The campaign's first run (on its then-83-mutant
corpus) exposed 8 misses and 8 unsound scopings, reducing to four root
causes: module-level constants outside every function body, constructor-
mediated state (`self._emit = emit` was in no slice), one-sided event-type
scoping, and deadline coupling across event types. Three were repaired
directly; the fourth became the conservative event-time coupling rule.
Together with the decorator defect from Experiment 3, five real holes were
found by these experiments, each now pinned by a regression test. The final
campaign is the post-hardening fixed point of these benchmarks, not
independent field evidence.

```
experiments/run_app_mutation.sh           → experiments/results/app_mutation.{txt,json}
```

## Experiment 5 - policy scoping

**Question.** When the analyser warns, does it name every policy whose
verdict could actually move?

**Method.** Among the 619 mutants, **314** changed at least one policy's
verdict set when executed (jobs 39, ticketing 36, order 66, session 83, todo
90). For each, the truly-affected policies (from the executed verdicts) are
checked for containment in the analyser's predicted at-risk set.

**Result: 314 of 314 sound.** Correct scoping requires both the old and new
event types of a changed site and the conservative event-time coupling rule
for deadline policies - the rule this experiment itself forced into
existence.

## Experiment 6 - git-history replay

**Question.** Does the analyser nag on the ordinary commits of real
development?

**Method & result.** Every commit in this repository that modified an
application file is replayed through the analyser: 9 historical changes,
5 flagged, 4 silent, all 9 classifications matching what the commits
actually did. One false alarm (a docstring-only edit) was found by this very
measurement and fixed. Small N, honestly stated; grows with history.

```
experiments/run_history_replay.sh         → experiments/results/history_replay.txt
```

## Experiment 7 - static-analysis cost and coverage

**Result.** The four real services analyse in 2.6-6.5 ms with at most
0.5 MB peak allocation; a synthetic surface of 1,400 functions and 200 emit
sites takes 259.5 ms and 22.9 MB, with approximately linear growth in
function count - a pre-commit-hook cost. Per-site dependency slices on the
real services are tight (mean 3-9 functions of 10-20), every unresolved call
is the declared injected transport, and the union of slices covers each
emission-dense module - so in a dedicated tap module most edits flag
something, and the discriminating value is the per-site scoping, not binary
silence. Timing varies by machine; the artifact records the platform.

```
experiments/run_analysis_cost.sh          → experiments/results/analysis_cost.txt
```

## Experiment 8 - engine runtime performance

**Question.** What does monitoring cost at runtime?

**Method.** Three demonstration services (order: e-commerce lifecycle with a
terminal event; session: authentication with real lockout logic and a
terminal event; todo: task manager with no terminal event, so instances are
never retired). Deterministic traces of 10³-10⁵ events; configuration P_k
registers the first k policies of a fixed per-service ladder. Medians of
five runs, one Apple M1 core, CPython 3.13; `tracemalloc` peaks measured
separately from timing.

**Result** at 10⁵ events:

```
service   P0 (s)  P1 (s)  P3 (s)  P5 (s)  P5 rate     P5 peak  P5 verdicts
order      0.243   0.430   0.761   1.115   90 k ev/s   34.6 MB       61,775
session    0.231   0.415   0.775   1.119   89 k ev/s   26.6 MB       59,225
todo       0.235   0.458   0.873   1.116   90 k ev/s  103.6 MB       52,411
```

Cost grows near-linearly in both policy count (the P columns) and trace
length (order at P5: 0.011 s, 0.107 s, 1.115 s for 10³, 10⁴, 10⁵ events) -
about 11.2 µs per event at five policies. Memory follows entity lifetime:
reclamation keeps order and session below 35 MB; terminal-free todo reaches
103.6 MB.

```
experiments/run_runtime_performance.sh    → demo/perf/results/*.jsonl
```

---

## The one-page summary

| # | Experiment | Scale | Result |
|---|---|---|---|
| 1 | Semantic conformance vs independent oracle | 1,600 generated checks | 0 counterexamples |
| 2 | Predicate-side stability (+ raw baseline) | 22 revisions | 11/12 breaks caught (1 declared miss), 6/6 renames silent, vs 7/5/2/8 for the baseline |
| 3 | App-side curated categories | 22 categories | 14/14 stream changes caught, 4/4 no-ops silent, 4 declared alarms |
| 4 | Adversarial mutation campaign | 619 mutants, 6 subjects | 443/443 stream-changing flagged, **0 misses** |
| 5 | Policy scoping | 314 verdict-changing mutants | 314/314 sound |
| 6 | Git-history replay | 9 real commits | 9/9 classified correctly |
| 7 | Analysis cost | up to 1,400 functions | linear, ~0.19 ms/function |
| 8 | Runtime performance | 3 services × 10⁵ events | ~90 k ev/s at 5 policies, ~11 µs/event |

Five real defects were found by these experiments (four by the mutation
campaign's first run, one by curated case E21) - every one fixed or
conservatively absorbed, pinned by a regression test, and reported above.
