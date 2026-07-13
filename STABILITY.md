# Specification stability: how a policy survives the code changing under it

## The problem

A runtime monitor dies silently. Rename a payload field, a status value, or an
event type in the monitored application, and the policy that depends on it
stops matching — no error, no failing test, no verdict. It sits in the
dashboard looking deployed and healthy precisely *because* it stopped working:
a monitor that matches nothing violates nothing. When an agent rewrites code
continuously, this is the default outcome, not the edge case. behave_rv's
central claim is that this cannot happen silently. This document explains the
mechanism, shows real input and output for every path, states the measured
detection rates, and names the residual limitations next to the features they
bound.

## The three defense paths and their division of labor

**Path A — the signature diff (build time, static).** Every registered step
carries a behavioral signature: the declared event type, the correlation key,
the referenced fields (the phrasing's placeholders), the exposed payload
fields read in the body, and a *fingerprint* of the matching contract — the
alpha-normalized AST of the predicate body, the binding-parameter names, and
the normalized bodies of every helper the predicate statically reaches
(same-module and same-package calls, transitively). Call sites the resolver cannot
follow — dynamic dispatch, functions passed as values, object methods,
builtins — are recorded in the signature's `unresolved_calls`, so the
protection boundary is visible per step.
The committed `catalog.json` is the contract; after a code change, the diff
classifies every step: equal signature with new wording is a **rename**
(absorbed silently), a moved signature is a **break**, reported against
exactly the policies whose compiled `used_step_ids` include that step. The
fingerprint is a conservative, rename-invariant *approximation* — NOT a
semantic-equivalence check. It is invariant to identifier names and
formatting, sensitive to structure and constants, and blind to
helper-function internals.

**Path B — liveness (build time, against a representative stream).** What the
static diff cannot see is the application changing *around* an untouched
step. Compiling policies against an observed stream (a replay file, a live
sample) warns when a policy depends on an event type never observed
(type-level) or on a concrete bound value never observed on its field
(value-level). Its boundary is the stream's representativeness: it can only
vouch for what a representative trace would show, and it cannot promise that
a value never yet seen never will be.

**Path C — compile-time resolution (refusal).** A step line that no longer
resolves against any registered phrasing refuses to compile, loudly. The
`catalog diff` command reports an uncompilable policy alongside the diff, and
when the diff also shows changed or removed steps, that combination is itself
a break.

**Path D — the app surface (build time, static, over the APPLICATION).**
Paths A–C guard the monitoring code and the stream. Path D guards the other
side of the event boundary: `catalog save --app` fingerprints every
`Event(...)` construction site in the application's source — its emitted
interface (event type, binding keys, payload fields) and a
function-granularity slice of everything that can participate in reaching it
— and `catalog diff --app` classifies every later change: an interface break
gates CI, a behavior risk warns (or gates under `--fail-on-app-risk`), both
scoped through event type → steps → `used_step_ids` to the exact policies at
risk. App code is never imported. Full mechanism, measured table, and
boundaries below ("The app side of the boundary").

## Worked examples — real input, real output

All output below is pasted from the tool (paths shortened). The step under
change:

```python
@trigger('an order is "{status}"', step_id="order.status.is",
         event_type="order.status", correlation_key="order_id")
def order_is(ctx, event, status):
    if event.type == "order.status" and event.payload.get("status") == status:
        ctx.bind(order_id=event.bindings["order_id"])
        return True
    return False
```

with the policy:

```gherkin
Scenario: paid after authorized
  When an order is "paid"
  Then an order is "authorized" before
```

### 1. An absorbed rename (Path A, silent by design)

The function becomes `order_status_predicate`, every internal identifier is
renamed, the body reformatted. `python -m behave_rv catalog diff`:

```
# catalog diff: catalog.json vs steps_renamed.py
  order.status.is: unchanged

ok: no breaks
```

Exit code 0. Nothing to review: the contract did not move. (A rewording of
the *phrasing* is also absorbed when the previous wording is retained as an
alias — `registry.alias("order.status.is", 'an order is "{status}"')`.)

### 2. A caught field rename (Path A, the break)

The predicate now reads `event.payload.get("state")`:

```
# catalog diff: catalog.json vs steps_field.py
  order.status.is: changed

# BREAKS (1) — scoped to the policies that use the step
  ✗ paid after authorized  [payments-team]  via order.status.is
    contract: payload_fields {'status': 'any'} -> {'state': 'any'}; trigger
    condition changed (step body or binding parameters; the structural
    fingerprint is conservative -- a behavior-preserving refactor such as a
    temporary variable, reordered operands, or an extracted helper also trips
    it, so review the step body)

FAIL: 1 break(s)
```

Exit code 1 — this is the CI gate. The break fires from the catalog alone,
before any event is processed.

### 3. A caught value rename (Path B, where the diff is honestly silent)

The *application* now emits `"PAID"`; the step is untouched, so the diff
correctly reports `unchanged` — and the stream tells the rest:

```
  order.status.is: unchanged

# liveness (2) — against trace.jsonl
  ! policy 'paid after authorized' depends on step 'order.status.is' with
    status='paid', but no 'order.status' event carrying that value has been
    observed in the available stream; the policy may be uncheckable.
  ! policy 'paid after authorized' depends on step 'order.status.is' with
    status='authorized', but no 'order.status' event carrying that value has
    been observed in the available stream; the policy may be uncheckable.

ok: no breaks (2 liveness warning(s) above)
```

### 4. A scoped notification (two steps, one event type)

`order.status.is` and `order.amount.exceeds` both observe `order.status`;
only the amount step's contract moves. The dependency map comes from each
compiled policy's `used_step_ids`, not from event types, so only the true
user is notified:

```
B6 breaks: [('no oversized order', 'order.amount.exceeds')]
```

The `paid after authorized` policy, sharing the event type but not the step,
hears nothing.

### 5. A conservative false positive (the design, stated)

Introducing a temporary variable in the predicate — behavior verified
unchanged by verdict replay — still trips the structural fingerprint and
produces the break above with the same conservative-fingerprint message.
This is deliberate: the fingerprint cannot prove two structures equivalent,
and a false alarm costs a glance where a missed alarm costs a dormant policy.
The measured false-alarm rate on the four refactor probes is 4/4 (D4 —
splitting a helper in two — joins D1–D3: the reachable set changes, so the
conservative alarm fires), and the break message says exactly what to do
with it (review the step body).

### 6. A helper change — now detected (the call-graph fingerprint)

The predicate delegates to `_matches(event, status)`; the *helper's*
condition changes while the step body stays byte-identical. This was the
mechanism's one documented MISS (old case C4) until the interprocedural
fingerprint closed it: the fingerprint covers the normalized bodies of every
statically reachable helper, so the diff breaks and names the helper:

```
  order.status.is: changed

  contract: reachable helper set changed
    (-['..._helper_matches_v1'] +['..._helper_matches_v2']);
    trigger condition changed (...)
```

Helper NAMES are not hashed — renaming a helper (A5) or reordering helper
definitions (A6) absorbs like any other rename; only body identities count.

### 7. The NEW boundary: a helper behind a value (C4b), shown honestly

Pass the helper as a value (`def order_is(ctx, event, status, _check=_v1)`)
and change what the value refers to: behavior changes, the step's AST is
identical, and static resolution deliberately does not follow values —
nothing speaks. This is the new documented boundary, asserted xfail-style
(`test_c4b_is_the_new_documented_boundary`) under the same protocol old C4
carried. What makes it honest rather than silent: the signature records the
call site the resolver could not follow —

```
  unresolved_calls: ['_check', 'ctx.bind']
```

— in the committed catalog, per step, so a reviewer can see exactly where the
fingerprint's protection ends. Mitigation: call helpers by name, which is
what the resolver covers.

## The workflow

```bash
# once, committed next to the code (the interface contract, BOTH sides)
python -m behave_rv catalog save --steps app/steps.py --catalog catalog.json \
    --app app/service.py

# after every code change (or as a CI job; exits 1 on breaks)
python -m behave_rv catalog diff --steps app/steps.py --catalog catalog.json \
    --policies policies/ --app app/service.py --trace last_week.jsonl
```

(`last_week.jsonl` is a stream you recorded earlier — a `TraceRecorder` tee
on a live app's emit chain, or `record_events` from a script; see the usage
guide's "Recording traces".)

The catalog lives in the repository and is reviewed like any interface
change: an INTENDED contract change is a regenerated `catalog.json` in the
same commit, and the diff output names every policy the change affects.
The artifact is stable across interpreter versions (the fingerprint uses a
version-stable AST serialization; verified byte-identical under CPython
3.10, 3.13, and 3.14) and across processes, so a catalog written on one
machine gates truthfully on another -- this task's fresh-clone check caught
and fixed the version dependence.

## The measured table

Twenty-two realistic code changes, each with ground truth *verified* by
replaying the same seeded-fault trace through baseline and changed versions
and comparing verdict sets. Reproduce with `python -m tests.stability_catalog`
(asserted permanently in `tests/test_stability_catalog.py`). Measured
2026-07-12 (extended to 22 cases with the call-graph fingerprint):

```
case behavior?  diff                   liveness  classification             description
--------------------------------------------------------------------------------------------------------------
A1   False      unchanged(0 brk)       0         CORRECT (silent)           rename the step function
A2   False      unchanged(0 brk)       0         CORRECT (silent)           rename internal variables in the predicate
A3   False      renamed(0 brk)         0         CORRECT (silent)           change the phrasing, old wording retained as alias
A4   False      unchanged(0 brk)       0         CORRECT (silent)           reformat the predicate body
B1   True       changed(11 brk)        0         CORRECT (diff)             rename the payload field the predicate reads
B2   True       changed(11 brk)        11        CORRECT (diff)             change the declared event type
B3   True       changed(11 brk)        0         CORRECT (diff)             change the correlation key
B4   True       changed(11 brk)        0         CORRECT (diff)             tighten the guard inside the predicate body
B5   True       removed(11 brk)+refusal 0         CORRECT (diff)             delete the step entirely
B6   True       changed,unchanged(1 brk) 0         CORRECT (diff)             two steps share an event type; change one; scope check
B7   True       changed(11 brk)        0         CORRECT (diff)             rename the placeholder-bound parameter (phrasing kept)
C1   True       unchanged(0 brk)       3         CORRECT (liveness)         app emits "PAID" instead of "paid", step untouched
C2   True       unchanged(0 brk)       11        CORRECT (liveness)         app emits a different event type, step untouched
C3   True       unchanged(0 brk)       17        CORRECT (liveness)         app carries the value under a different field name
C4   True       changed(11 brk)        0         CORRECT (diff)             predicate delegates to a helper; the helper changes
A5   False      unchanged(0 brk)       0         CORRECT (silent)           rename a helper (call site updated), body identical
A6   False      unchanged(0 brk)       0         CORRECT (silent)           reorder two helper definitions, no call/body changes
C4b  True       unchanged(0 brk)       0         MISS (documented)          helper change behind an unresolvable (value) call
D4   False      changed(11 brk)        0         FALSE ALARM                split one helper into two, behavior preserved
D1   False      changed(11 brk)        0         FALSE ALARM                introduce a temporary variable in the predicate
D2   False      changed(11 brk)        0         FALSE ALARM                reorder commutative boolean operands
D3   False      changed(11 brk)        0         FALSE ALARM                extract unchanged logic into a helper
--------------------------------------------------------------------------------------------------------------
false alarms in conservative probes: 4/4
```

Reading it plainly: every representational change is absorbed (6/6 — now
including helper renames and helper definition reordering), every
signature-visible break is caught and scoped (7/7), every app-side
disconnect visible to a representative stream is caught (3/3), helper body
changes behind static calls are caught (C4, formerly the documented miss),
the one remaining blind spot is C4b (calls through values) and it is
asserted as such with its weaker protection visible in `unresolved_calls`,
and the conservative probes alarm 4/4 by design.

## The app side of the boundary: emit-site impact analysis (Path D)

Everything above protects against the MONITORING code drifting. The
symmetric failure is the APPLICATION drifting: a guard added before an
emission, a helper reworked two calls deep, an emitted value renamed — the
steps are untouched, `catalog diff` truthfully says `unchanged`, and the
first signal is a runtime violation (if traffic exercises the path) or
silence (if it does not). Path D closes that window statically.

### The algorithm, and where it comes from

The in-process exposure convention makes every emission a syntactically
visible anchor: an `Event(...)` construction whose type is a literal or a
module constant. For each anchor the analyzer (pure AST — application code
is never imported) extracts the **emitted interface** (event type, binding
keys, payload keys; `<dynamic>`/`<splat>` markers where the source cannot be
resolved) and computes a **function-granularity backward slice**: the
emitting function, its transitive callers (they decide when it runs and with
what arguments), and the transitive callees of all of those (they compute
the values). Each slice member is hashed with the same version-stable
alpha-normalization the step fingerprint uses, and the committed catalog's
`app_surface` section is the reference the next diff compares against.

The lineage is three classic results, each deliberately modified:

- **Interprocedural program slicing** (Ferrante–Ottenstein–Warren PDGs;
  Horwitz–Reps–Binkley SDG slicing), reduced from statement-level slices
  with context-sensitive summary edges to function-granularity,
  context-insensitive closure over the call graph. Precision is lost only
  toward over-approximation — extra warnings, never missed ones — which is
  the direction this tool is allowed to be wrong in.
- **Change impact analysis** (Ren et al.'s Chianti), with runtime
  verification policies in the role Chianti gives regression tests: a
  changed slice maps through event type → catalog steps → `used_step_ids`
  to the policies at risk.
- **The catalog's own rename-vs-break discipline**, with one app-side
  tightening: called-function names are preserved in the hash (occurrence-
  order canonicalization would absorb a REORDER of two emitting calls, and
  emission order is contract — a `before` policy hangs on it). Locals,
  parameters, class names, comments, and docstrings absorb; renaming a
  function on an emit path flags conservatively as a risk, never as a
  removal-level break.

### Worked example 8 — an app logic change, caught statically and scoped

The steps are untouched. Someone "cleans up" `assign()` in the ticketing
example:

```python
def assign(self, ticket_id: str, agent: str) -> None:
    if agent != "oncall":                                # new guard
        self._status(ticket_id, "assigned", agent=agent)
```

`catalog diff --app app_service.py` (real output; the step diff above it
says `unchanged` five times):

```
# app surface diff (5 emit site(s))
  app_service.TicketService._status#1: behavior-risk
  app_service.TicketService.agent_reply#1: unchanged
  app_service.TicketService.close#1: unchanged
  app_service.TicketService.customer_reply#1: unchanged
  app_service.TicketService.set_priority#1: unchanged

# APP BEHAVIOR RISKS (1) — logic on an emit path changed
  ! app_service.TicketService._status#1 (event 'ticket.status')
      function(s) in the emit slice changed: app_service.TicketService.assign
      policies at risk: a ticket may only be resolved after it was assigned,
      an escalated ticket must not be closed until resolved, an opened
      ticket is assigned within the window, every ticket is eventually
      resolved, the on-call agent only receives urgent tickets

ok: no breaks (1 app behavior risk(s) above)
```

A risk warns and exits 0 by default; `--fail-on-app-risk` promotes it to a
CI gate. Note the precision: only the `ticket.status` site flags, and only
the changed function is named.

### Worked example 9 — the app renames a payload field (the break level)

The same edit as worked example 2, but on the APP side:
`{"status": status, ...}` becomes `{"state": status, ...}` in the service.
Real output:

```
# APP BREAKS (1) — the emitted interface changed
  ✗ app_service.TicketService._status#1 (event 'ticket.status')
      payload_fields ['<splat>', 'status'] -> ['<splat>', 'state']
      policies at risk: a ticket may only be resolved after it was assigned,
      an escalated ticket must not be closed until resolved, an opened
      ticket is assigned within the window, every ticket is eventually
      resolved, the on-call agent only receives urgent tickets

FAIL: 1 break(s)
```

Exit code 1 — the emitted interface is contract, exactly like a step
signature. (`<splat>` is the declared marker for `**payload` forwarding:
keys added by callers flow through caller hashes in the slice instead.)

### The measured table (E-series)

Fifteen realistic APP changes against a fixed baseline service. Ground truth
is verified per run by executing the same scripted traffic through baseline
and variant and comparing the emitted event STREAM (with the policy verdict
set checked alongside — E10 shows the layering: the stream changed, verdicts
did not, and it is still caught). Reproduce with
`python -m tests.stability_app_surface` (asserted permanently in
`tests/test_stability_app_surface.py`). Measured 2026-07-13:

```
case  change                                                   stream   verdicts  detected  outcome
E1    comment and docstring edited                             same     same      silent    CORRECT
E2    local variable renamed in an emit path                   same     same      silent    CORRECT
E3    the service class renamed                                same     same      silent    CORRECT
E4    guard before an emission tightened                       changed  changed   risk      CORRECT
E5    helper logic changed two calls deep                      changed  changed   risk      CORRECT
E6    an emitted status value renamed (vocabulary drift)       changed  changed   risk      CORRECT
E7    a payload field renamed                                  changed  changed   break     CORRECT
E8    the event type constant changed                          changed  changed   break     CORRECT
E9    an emission deleted                                      changed  changed   break     CORRECT
E10   a new emission added inside an existing method           changed  same      risk      CORRECT
E11   the terminal emission moved before the status it follows changed  changed   risk      CORRECT
E12   a function outside every emit slice changed              same     same      silent    CORRECT
E13   a function on an emit path renamed (pure)                same     same      risk      FALSE ALARM  (by design: callable identity is emission-order contract; cannot be proven representational, so it flags)
E14   extract-method refactor inside an emit slice             same     same      risk      FALSE ALARM  (by design: slice membership changed; structural fingerprints cannot prove the refactor equivalent)
E15   the event type becomes computed instead of a constant    same     same      break     FALSE ALARM  (by design: the type is no longer statically analyzable; losing the check must surface, not silently degrade)

15 cases: 12 correct, 3 false alarm(s) (all by design), 0 miss(es)
```

Every stream change is caught (0 misses), every no-op stays silent except
the three DECLARED conservatisms (E13–E15), which are asserted to be exactly
that family and nothing more.

### Replayed against this repository's own history

The honest flag-rate question — "will this nag me on every commit?" —
measured on every historical change to an app file in this repo
(`python -m tests.measure_app_history`, 2026-07-13):

```
service.py       c7d8355  added,behavior-risk    Interactive boards for the order and session demos
service.py       19a1ebe  silent                 GitHub Actions CI: test matrix py3.10-3.14 …
service.py       2049b69  silent                 Documentation accuracy pass: seven reviewer-identified …
service.py       c7d8355  added,behavior-risk    Interactive boards for the order and session demos
service.py       376dc44  added                  Interactive todo board: user-driven events …
app.py           4b22945  silent                 Redesign the todo board as a modern app …
app.py           376dc44  silent                 Interactive todo board: user-driven events …
app_service.py   b0dbf4f  behavior-risk          Guide: what happens when the APP's logic changes …
app_service.py   45f8b7a  added                  Ticketing example grown to five steps …

9 historical app-file changes: 5 flagged, 4 silent
```

All nine classifications match what the commits actually did: the five flags
are real emission changes (new sites, and the `close()` timestamp fix — the
one genuine app-behavior bug fix in this repo's history, which this check
would have surfaced before runtime); the four silents are a lint reflow, a
docstring edit, and two changes to a file with no emit sites. The docstring
case initially flagged — a false alarm this measurement itself found, fixed
by stripping docstrings from the normalization (they cannot change emission
behavior). Small N, honestly stated; the method is committed and rerunnable
as history grows.

## Residual limitations (the honest bottom line)

1. **Calls through values** (C4b): resolution covers same-module and
   same-package calls BY NAME, transitively. A helper reached through a
   value (a parameter default, a callback, dynamic dispatch, an object
   method) is not followed — deliberately: a resolver clever enough to chase
   values is a resolver that can err in the quiet direction, which inverts
   the trust model: the tool's whole value is that silence means safe. The
   boundary is visible per step in the signature's `unresolved_calls`.
2. **Stream representativeness**: liveness vouches only for what the observed
   trace shows. A value that never appeared cannot be flagged as newly
   missing; an unrepresentative trace weakens the check to exactly its
   coverage.
3. **Conservatism**: behavior-preserving structural refactors (D1–D4,
   including splitting a helper) alarm. That is a cost paid deliberately,
   and the break message routes it to a glance rather than an
   investigation.
4. **App-side granularity** (Path D): the slice is function-granularity, so
   ANY edit to a function in an emit slice flags, including refactors that
   preserve behavior (E13–E14) — the same deliberate cost as (3). Statement-
   level slicing would tighten this and can be added behind the same
   interface.
5. **App-side staticness** (Path D): the analyzer reads code structure only.
   Behavior driven by runtime data (config, database contents, request
   payloads) has no static signature; emissions not constructed as a
   recognizable `behave_rv` `Event` are outside the analysis (a target
   yielding zero anchors warns rather than passing silently); dynamic
   constructs degrade to declared `<dynamic>`/`unresolved` markers, and a
   type becoming dynamic is itself surfaced as a break (E15).

See the live demonstration: `python -m demo.order_service.app`, then
`http://127.0.0.1:5001/stability`.
