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
alpha-normalized AST of the predicate body plus the binding-parameter names.
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
The measured false-alarm rate on the three refactor probes is 3/3, and the
break message says exactly what to do with it (review the step body).

### 6. The helper blind spot (the residual limitation, shown honestly)

The predicate delegates to `_matches(event, status)`; the *helper's*
condition changes. Verdict replay proves the policy goes dormant on the very
fault it used to catch — and nothing speaks:

```
  order.status.is: unchanged        # the step body is literally identical
  liveness: nothing to report       # the stream still carries the old values
```

This is the documented boundary: static fingerprints do not follow
indirection, and the observed stream did not change. The suite asserts this
MISS explicitly (`test_c4_is_the_documented_boundary_not_a_hidden_one`), so
if a future mechanism starts catching it, the documentation must move with
the code. Mitigation: keep matching logic inside the step body, which is
exactly what the conservative D3 false alarm nudges toward.

## The workflow

```bash
# once, committed next to the code (the interface contract)
python -m behave_rv catalog save --steps app/steps.py --catalog catalog.json

# after every code change (or as a CI job; exits 1 on breaks)
python -m behave_rv catalog diff --steps app/steps.py --catalog catalog.json \
    --policies policies/ --trace last_week.jsonl
```

The catalog lives in the repository and is reviewed like any interface
change: an INTENDED contract change is a regenerated `catalog.json` in the
same commit, and the diff output names every policy the change affects.

## The measured table

Eighteen realistic code changes, each with ground truth *verified* by
replaying the same seeded-fault trace through baseline and changed versions
and comparing verdict sets. Reproduce with `python -m tests.stability_catalog`
(asserted permanently in `tests/test_stability_catalog.py`). Measured
2026-07-11:

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
B5   True       removed(11 brk)+refusal 0        CORRECT (diff)             delete the step entirely
B6   True       changed,unchanged(1 brk) 0       CORRECT (diff)             two steps share an event type; change one; scope check
B7   True       changed(11 brk)        0         CORRECT (diff)             rename the placeholder-bound parameter (phrasing kept)
C1   True       unchanged(0 brk)       3         CORRECT (liveness)         app emits "PAID" instead of "paid", step untouched
C2   True       unchanged(0 brk)       11        CORRECT (liveness)         app emits a different event type, step untouched
C3   True       unchanged(0 brk)       17        CORRECT (liveness)         app carries the value under a different field name
C4   True       unchanged(0 brk)       0         MISS (documented)          predicate delegates to a helper; the helper changes
D1   False      changed(11 brk)        0         FALSE ALARM                introduce a temporary variable in the predicate
D2   False      changed(11 brk)        0         FALSE ALARM                reorder commutative boolean operands
D3   False      changed(11 brk)        0         FALSE ALARM                extract unchanged logic into a helper
--------------------------------------------------------------------------------------------------------------
false alarms in conservative probes: 3/3
```

Reading it plainly: every representational change is absorbed (4/4), every
signature-visible break is caught and scoped (7/7 — including B7, a silent
break this very harness discovered and whose fix added binding-parameter
names to the fingerprint), every app-side disconnect visible to a
representative stream is caught (3/3), the one architectural blind spot is
C4 and it is asserted as such, and the conservative probes alarm 3/3 by
design.

## Residual limitations (the honest bottom line)

1. **Helper indirection** (C4): a behavior change inside a called helper
   moves no signature and, when the stream is unchanged, no liveness warning.
   Static fingerprints do not follow indirection.
2. **Stream representativeness**: liveness vouches only for what the observed
   trace shows. A value that never appeared cannot be flagged as newly
   missing; an unrepresentative trace weakens the check to exactly its
   coverage.
3. **Conservatism**: behavior-preserving structural refactors (D1–D3) alarm.
   That is a cost paid deliberately, and the break message routes it to a
   glance rather than an investigation.

See the live demonstration: `python -m demo.order_service.app`, then
`http://127.0.0.1:5001/stability`.
