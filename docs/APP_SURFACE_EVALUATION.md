# Emit-Site Impact Analysis: Statically Scoping Application Changes to Runtime-Verification Policies

*Capability description and experimental evaluation. Prepared for inclusion in
the paper; every number below is produced by a committed, rerunnable script
(section 8), measured 2026-07-13 on this repository.*

## 1. Summary

behave_rv monitors a running application against human-authored Gherkin
policies. The runtime monitor verifies *executions, not programs*: it detects
an application change only when the change's effects reach the event stream,
some policy constrains them, and traffic exercises the changed path. This
work adds the missing build-time complement: a static **change-impact
analysis from application source code to the runtime-verification policies at
risk**, computed before anything runs.

The key structural observation is that in-process runtime verification makes
the application's output ports *syntactically identifiable*: every emission
is a construction of the framework's `Event` type. Anchoring backward
dependency slices at these construction sites — rather than at test
executions (regression selection) or at arbitrary program points (general
slicing) — yields an analysis that is (i) cheap enough for a pre-commit hook,
(ii) empirically complete on three adversarial ground-truth benchmarks after
an iterative hardening process that the evaluation itself drove, and (iii)
precise enough that its per-change reports name the specific functions,
constants, and policies involved.

The evaluation is reported honestly, including its initial failures: the
mutation campaign's **first run found 8 missed detections and 8 unsound
scopings** across 83 mutants, reducing to four distinct root causes; three
were fixed and one absorbed into a conservative scoping rule, after which all
83 mutants are handled correctly. We consider the found-then-fixed trajectory
itself part of the result: it demonstrates what the method's blind spots look
like and that they are discoverable by systematic testing rather than by
production incidents.

## 2. Problem

A runtime monitor dies silently under code change. The monitored
application's author renames a payload field, tightens a guard before an
emission, or reworks a helper two calls deep; no step definition changed, no
test fails, and the policy that depended on the old behavior either fires a
late violation in production or — worse — goes quiet while looking deployed
and healthy. The step-side catalog (the companion capability, STABILITY.md
paths A–C) protects the *monitoring* code; it truthfully reports `unchanged`
when only the *application* moved. Prior to this work, application drift was
caught by exactly two nets, both requiring execution: replaying scripted
traffic and comparing verdicts, or waiting for live traffic to exercise the
changed path.

The question this capability answers at commit time, with no execution:
**"can this application change affect what the monitor observes, and if so,
which policies should be re-examined?"**

## 3. The capability, precisely

**Definitions.** An *emit site* is an `Event(...)` construction in the
application source, where `Event` is behave_rv's event type under a
recognizable import. Its *emitted interface* is the triple (event type,
binding keys, payload keys), each component resolved from literals, module
constants, or dictionary literals, with declared markers (`<dynamic>`,
`<splat>`) where resolution fails. Its *slice* is the least set of functions
closed under: the emitting function; transitive callers (they decide when the
emission executes and with what arguments); transitive callees of all members
(they compute emitted values); and methods assigning any instance attribute
some member reads (emit-path state flows through attributes — `self._emit`
itself is one). The slice additionally carries the values of module-level and
class-level constants referenced by any member. Each function is hashed under
an alpha-normalization that canonicalizes local and parameter names, strips
docstrings, formatting, and type annotations, and **preserves called-function
names** (section 4). The committed catalog stores, per site, the interface
and a fingerprint over the member-hash set plus referenced constants.

**Classification.** Diffing the recomputed surface against the committed one
yields, per site: `unchanged`; `renamed` (identical interface and
fingerprint under a moved identity, e.g. a class rename — absorbed silently);
`behavior-risk` (interface intact, slice fingerprint moved); `interface-break`
(the emitted type, keys, or fields changed, or the site was deleted); or
`added`. Breaks gate CI (exit 1); risks warn by default and gate under a
flag; additions surface on the suggestion channel.

**Scoping.** A flagged site maps to policies through event type → catalog
step signatures → the `used_step_ids` each compiled policy recorded — over
*both* the old and new interface (a changed type must alert the policies
observing the old one). Additionally, policies carrying a bounded-response
deadline and sharing the site's correlation key are conservatively included:
deadline firing is driven by event-time advancement, so any change to an
entity's event flow — including events of types the policy never binds — can
move a deadline verdict (section 6, a discovery of the scoping experiment).

**Claimed:** within the declared resolvable fragment, every change that can
alter the emitted stream flags, and every policy whose verdicts can move is
in the reported at-risk set. This is a *may-affect* analysis: it says
"review these", never "violated".

**Explicitly not claimed:** semantic equivalence checking (structure-
preserving refactors flag — measured, section 6); coverage of behavior driven
by runtime data (configuration, database contents, request payloads);
coverage of emissions not constructed as a recognizable `Event` (a target
yielding zero anchors warns rather than passing); coverage of dynamic
constructs (`getattr`, `**kwargs` splat, calls through values), which degrade
to declared markers rather than to silence; timing effects originating
outside the code (network latency breaking a deadline has no static
signature).

## 4. Algorithm and lineage

Three classical results, each deliberately modified; the modifications are
where the engineering content lies.

**Interprocedural slicing** (Ferrante–Ottenstein–Warren program dependence
graphs, TOPLAS 1987; Horwitz–Reps–Binkley system-dependence-graph slicing,
PLDI 1988). Classical backward slicing from an output statement is the exact
conceptual fit: the slice of an emission is the set of program parts whose
change may change that observable output. We reduce it from statement-level
slices with context-sensitive summary edges to **function-granularity,
context-insensitive closure over an assignment-based call graph**, extended
with an instance-attribute def-use fixpoint and constant capture. The
motivation is Python: dynamic dispatch, duck typing, and `**kwargs` defeat
the precision summary edges pay for, and the reduction loses precision only
toward over-approximation — extra warnings, never missed ones — which is the
one direction a trust-carrying tool is permitted to be wrong in. The
per-function hash granularity also makes the committed artifact reviewable:
a risk report names the changed functions, not opaque statement sets.

**Change impact analysis** (Ren et al., *Chianti*, OOPSLA 2004). Chianti
decomposes an edit into atomic changes and maps them through call graphs to
the *regression tests* affected. We put runtime-verification policies in the
role of the tests: the emit anchor plus the event-type vocabulary gives a
static join point between code and specifications that generic CIA lacks.
This is, to our knowledge, the novel composition: impact analysis whose
impact set is a set of runtime monitors, made possible because the monitoring
surface is compiled into the application.

**PDG-based semantic differencing** (Horwitz, PLDI 1990), in a deliberately
cheap form: differences are computed on alpha-normalized ASTs, so purely
representational edits (renames of locals, parameters, and classes;
formatting; annotations; docstrings; comments) produce identical hashes and
absorb silently, while structural and constant changes flag. One asymmetry
against the step-side catalog is deliberate: **called-function names are
preserved** in app-side hashes. Occurrence-order canonicalization would
assign two distinct calls the same canonical names regardless of their order,
silently absorbing a *reorder of two emitting calls* — and emission order is
contract (a `before` policy hangs on it). The measured price is that renaming
a function on an emit path cannot be proven representational and flags as a
risk (case E13); the classifier pairs such orphaned sites by interface so a
rename never escalates to a removal-level break.

## 5. Implementation

555 lines of Python (`behave_rv/catalog/app_surface.py`), standard library
only, pure AST: **application code is never imported** by the analyzer (it
may have side effects; the experiment harnesses that execute code for ground
truth are test-side only). Complexity is linear parsing plus linear
graph construction plus per-site closures; measured cost is ~0.19 ms per
function end to end (section 6, RQ5). The artifact is committed JSON
(catalog format v3, an `app_surface` section beside the step entries),
diffable in review, stable across CPython 3.10–3.14 via a version-stable AST
serialization shared with the step-side fingerprint. CLI: `catalog save
--app`, `catalog diff --app [--fail-on-app-risk]`, exit-coded for CI.

## 6. Experimental evaluation

Five experiments; all scripts committed and rerunnable (section 8). Two
ground-truth notions are used, both *executed*, never assumed: the **emitted
stream** (every field of every event under a fixed scripted traffic — the
app's observable behavior, exactly what the analysis claims to guard) and the
**policy verdict set** (for scoping). Stream equivalence under one traffic
script is not semantic equivalence; we count every flagged-but-stream-
preserving mutant against the analysis as an alarm, making the reported
false-alarm figures upper bounds in spirit, while acknowledging (section 7)
that a cleverer traffic script could reclassify some "alarms" as true
positives.

### RQ1 — Detection on curated realistic changes (E-series)

Seventeen hand-constructed changes spanning the absorb/flag/break space,
each with ground truth verified per run by execution
(`python -m tests.stability_app_surface`, asserted in pytest):

```
case  change                                                    stream   verdicts  detected  outcome
E1    comment and docstring edited                              same     same      silent    CORRECT
E2    local variable renamed in an emit path                    same     same      silent    CORRECT
E3    the service class renamed                                 same     same      silent    CORRECT
E4    guard before an emission tightened                        changed  changed   risk      CORRECT
E5    helper logic changed two calls deep                       changed  changed   risk      CORRECT
E6    an emitted status value renamed (vocabulary drift)        changed  changed   risk      CORRECT
E7    a payload field renamed                                   changed  changed   break     CORRECT
E8    the event type constant changed                           changed  changed   break     CORRECT
E9    an emission deleted                                       changed  changed   break     CORRECT
E10   a new emission added inside an existing method            changed  same      risk      CORRECT
E11   the terminal emission moved before the status it follows  changed  changed   risk      CORRECT
E12   a function outside every emit slice changed               same     same      silent    CORRECT
E13   a function on an emit path renamed (pure)                 same     same      risk      FALSE ALARM (by design)
E14   extract-method refactor inside an emit slice              same     same      risk      FALSE ALARM (by design)
E15   the event type becomes computed instead of a constant     same     same      break     FALSE ALARM (by design)
E16   a module-level constant used in emission logic changes    changed  changed   risk      CORRECT
E17   a benign attribute added in the constructor               same     same      risk      FALSE ALARM (by design)
```

13/17 correct, 0 misses, 4 false alarms — all four in the *declared*
conservatism family, asserted in the test suite to be exactly that family
and nothing more. E10 exhibits the layering the design intends: a new
emission changes the stream but no verdict (nothing observes it yet), and it
is still surfaced. E15 is a false alarm we defend as policy: when the event
type stops being statically analyzable, losing the check must itself
surface.

### RQ2 — Adversarial detection completeness (mutation campaign)

`python -m tests.exp_app_mutation` applies every mutant a deterministic
operator set can produce — string and numeric constant perturbation,
comparison operator swaps, boolean operator swaps, if-condition negation,
statement deletion — to three subjects: the E-series baseline service (27
mutants), the ticketing example application (42), and a probe *designed to
stress the suspected weakest point* — a module-level, non-event-type constant
participating in emission logic (14). 83 mutants total; each is executed
under fixed traffic for stream ground truth (a mutant that raises counts as
stream-changing) and independently analyzed.

**First run: 8 misses and 8 unsound scopings.** Root-cause analysis reduced
them to four defects, which we report as findings, not embarrassments —
each is a hole any reader would want to know the shape of:

1. **Module-level constants** (predicted; the probe existed to confirm it).
   `LIMIT = 10 → 11` changes emissions while no function body changes; the
   constant's value lived outside every hash. *Fixed*: constants referenced
   by slice members join the fingerprint, and the risk report names them.
2. **Constructor state.** Deleting `self._emit = emit` in `__init__`
   crashes every emission, yet `__init__` sat in no slice: emit-path state
   flows through instance attributes, invisible to a call graph. *Fixed*: a
   def-use fixpoint over `self.<attr>` adds attribute-writing methods to the
   slices of their readers. Measured cost: any constructor edit now flags
   the class's sites (declared as case E17).
3. **One-sided scoping.** A mutated event-type constant was caught as an
   interface break but scoped by the *new* type name — which no step
   observes — alerting nobody. *Fixed*: scoping covers both sides of a
   change.
4. **Deadline coupling across event types** (the subtle one). Deleting
   `ticket.status` emissions moved the verdicts of the *reply-SLA* policy,
   which never binds `ticket.status`: deadline firing is driven by
   event-time advancement, and removing events changed which event pushed
   time past the deadline. Event-type-based scoping is inherently blind to
   this. *Absorbed conservatively*: the compiler marks deadline-carrying
   policies, and scoping includes those sharing the flagged site's
   correlation key, labeled as event-time coupling.

**After hardening — final run, all three subjects:**

```
subject     mutants  crash-at-traffic  stream-changed & flagged  MISSED  stream-same & flagged  stream-same & silent
jobs        27       5                 25                        0       0                      2
ticketing   42       6                 42                        0       0                      0
probe       14       4                 14                        0       0                      0
```

83/83 correct: zero misses, and — notable — **zero alarms on
stream-preserving mutants**; the only two stream-preserving mutants in the
corpus (edits to functions outside every slice) were correctly silent. The
class-level-constant sibling of defect 1 (`self.LIMIT` reading a class
attribute) was closed symmetrically before any benchmark demanded it, with a
regression test.

### RQ3 — Scoping soundness

For the 43 mutants whose policy verdict sets changed (17 jobs, 26
ticketing): is every policy whose verdicts moved contained in the reported
at-risk set? **43/43 sound** after the two scoping fixes (35/43 before).
Tightness is not free: the deadline-coupling rule adds the deadline policies
of the entity to every flagged site's report (in the ticketing subject, 2 of
6 policies), clearly labeled so a reviewer can discount them when timing is
known unaffected.

### RQ4 — Flag rate on real history

`python -m tests.measure_app_history` replays every historical change to an
application file in this repository (9 change-pairs across 5 files):
5 flagged, 4 silent, and on inspection **all 9 classifications match what
the commits actually did**. The flags: three commits adding real emissions,
and the one genuine app-behavior bug fix in the repository's history (a
terminal-event timestamp correction that changed 10 verdicts in the
committed example) — the case the capability exists for. The silents: a
lint reflow, a docstring-only edit, and two changes to a file containing no
emit sites. The docstring case *initially flagged*; that false alarm was
found by this measurement and fixed (docstrings stripped from
normalization). N=9 is small and the history is our own; we present this as
a sanity check on developer experience, not as a field study.

### RQ5 — Cost

`python -m tests.exp_app_scaling`, median of 5 runs, CPython 3.13, Apple
Silicon:

```
subject                    functions  sites  median ms  peak MB
ticketing/app_service.py       10       5       2.6       0.2
order_service/service.py       15       3       4.0       0.4
session_service/service.py     20       3       6.5       0.5
todo_app/service.py            18       3       5.0       0.5
synthetic  C=5   D=4           35       5       6.7       0.5
synthetic  C=20  D=4          140      20      25.9       2.2
synthetic  C=50  D=4          350      50      65.0       5.7
synthetic  C=100 D=4          700     100     129.4      11.4
synthetic  C=200 D=4         1400     200     259.5      22.9
synthetic  C=50  D=2          250      50      44.4       4.1
synthetic  C=50  D=8          550      50     106.1       8.8
synthetic  C=50  D=16         950      50     182.6      15.0
```

Time and peak allocation are linear in function count (~0.19 ms and ~16 KB
per function) and insensitive to call depth at fixed size. A 1,400-function
emission surface analyzes in ~260 ms — a pre-commit-hook cost, run cold with
no caching.

### RQ6 — Fragment coverage and slice tightness

`python -m tests.exp_app_coverage` on the four real services:

```
file                        funcs  sites  slice min/mean/max  union-coverage  unresolved calls (all declared)
ticketing/app_service.py      10     5        2 / 3.2 / 7          100%       self._clock, self._emit
order_service/service.py      15     3        2 / 6.7 / 13         100%       self._clock, self._emit, self._sleep
session_service/service.py    20     3        4 / 8.7 / 17         100%       <dynamic>, self._clock, self._emit, self._sleep
todo_app/service.py           18     3        2 / 6.7 / 14         100%       self._clock, self._emit, self._sleep
```

Two readings, one favorable and one cautionary. Favorable: individual slices
are tight (mean 3–9 functions of 10–20), so a flag names a small, reviewable
set, and every unresolved call on these files is the injected transport —
declared, expected, and harmless (the construction, not the transport,
carries the contract). Cautionary: the *union* of slices covers 100% of
these modules — in an emission-dense service module, nearly any edit flags
*something*. The discriminating value there is the per-site scoping and the
named functions, not binary silence; silence discrimination (case E12)
matters in mixed modules where emission logic coexists with utility code. We
report this rather than hide it because it bounds the "quiet on refactors"
claim to its true scope.

## 7. Threats to validity, stated bluntly

- **Subjects are small and self-authored.** Three services of 10–20
  functions, written by the same project that wrote the analyzer, following
  the project's own exposure conventions. Real applications are larger,
  messier, and may construct events through wrappers the anchor recognizer
  misses (it warns on zero anchors, but a *partially* anchored file
  degrades per-site, visibly only in the unresolved/marker statistics).
  Nothing here is evidence about foreign codebases.
- **Ground truth is traffic-relative.** Stream equivalence under one
  scripted traffic is weaker than semantic equivalence. For *soundness*
  this direction is safe (a stream change under any traffic proves a real
  change); for the *alarm* figures it means "no observed behavioral
  difference on this traffic", not "provably equivalent". The E13/E14/E17
  alarms are argued equivalent by construction, not proven.
- **The evaluation drove the hardening.** The mutation campaign found the
  holes and the same authors fixed them and re-ran; the final 83/83 is
  therefore a fixed-point of this benchmark, not an independent test. New
  operator families (e.g. reordering statements across functions, mutating
  mutable module state, decorator changes) could find new holes; mutable
  module-level state in particular is a known-untested surface.
- **Mutation operators are syntactic and local.** Single-node perturbations
  approximate real edits imperfectly; the history replay (RQ4) partially
  compensates with real commits, but N=9.
- **No baseline comparison.** We did not run Chianti-class CIA tools or
  test-selection tools on the same subjects — partly because none scope to
  RV policies (the point of the work), but a comparison on the shared
  sub-problem (change → affected code regions) would still calibrate
  precision claims and is future work.
- **Single language, single convention.** Everything depends on Python AST
  semantics and the in-process `Event(...)` idiom. OpenTelemetry-style or
  log-based exposure has no syntactic anchor; the approach does not
  transfer there without a declaration mechanism.

## 8. Reproducibility

All experiments run from the repository root, standard library plus the
package itself, deterministic (no randomness, no timestamps):

```bash
python -m tests.stability_app_surface     # RQ1: the 17-case E-series
python -m tests.exp_app_mutation          # RQ2/RQ3: 83 mutants, exit 1 on any miss
python -m tests.measure_app_history       # RQ4: this repo's own commits
python -m tests.exp_app_scaling           # RQ5: cost vs size
python -m tests.exp_app_coverage          # RQ6: resolvability and tightness
python -m pytest tests/ demo/ -q          # 383 tests; E-series + campaign pinned
```

The E-series expectations, the zero-miss property, and the exact by-design
false-alarm family are asserted in the permanent test suite, so any
regression in the analyzer fails CI, not just a rerun of this document.

## 9. What the paper may and may not claim

May claim: a static, sound-up-to-declared-fragment change-impact analysis
from application code to runtime-verification policies, anchored at
syntactically identifiable emission sites; empirically zero misses on 83
adversarial mutants and 17 curated cases with executed ground truth, zero
false alarms outside a declared four-case conservatism family, sound
scoping on all 43 verdict-affecting mutants including a non-obvious
event-time coupling rule; linear cost (~0.2 ms/function); and a committed,
reviewable contract artifact integrating with CI. May also claim the
methodological point: the blind spots were found by the evaluation protocol
itself (four defects from the first mutation run, one from the history
replay), each converted into either a fix with a regression test or a
declared, measured conservatism.

Must not claim: semantic refactoring-equivalence, coverage of data-driven or
environment-timing behavior, applicability beyond the in-process anchored
convention, field-scale precision evidence, or independence of the
evaluation from the implementers.
