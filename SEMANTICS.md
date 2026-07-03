# behave_rv operator semantics

The reference definition of the correct verdict for each implemented operator, as
a function of the event **trace**, independent of the implementation. The oracle
in `tests/oracle.py` implements exactly this; the engine is checked against it.

Scope: the operators that exist today — `never`, `before`, `within` — plus the
event-time reordering contract. Nothing else.

## Trace, keys, canonical order

A **trace** is a finite set of events. Each `Event` has `type`, `event_time`
(seconds), `bindings` (the correlation key values), `payload`, `source`.

A policy names a **correlation key** (one field, or a tuple for composite
identity). The trace is partitioned into one independent **instance** per distinct
key value; a policy's verdict for a key is computed from that key's events alone.
The one shared quantity across keys is the **clock horizon** `H` = the maximum
`event_time` over the *whole* trace (see `within`).

Events are evaluated in **canonical order**: sorted by

1. `event_time` ascending, then
2. a content tie-break: `(type, sorted(bindings), sorted(payload), source)`.

The tie-break exists so that events sharing a timestamp have a single, total,
**arrival-independent** order. (An equally valid alternative spec would use strict
event-time precedence and make ties verdict-irrelevant; this implementation uses
the canonical content tie-break. Either is arrival-independent — that is the
property that matters.)

Verdicts are three-valued: `satisfied`, `violated`, `pending`. On a finite trace,
an obligation that has neither passed nor failed is `pending`.

## never

`never(bad)` — a status must never occur. `bad` is a predicate on an event
(here: `payload["status"] == b`).

Authorable form: a self-contained, predicate-first `Then` with no `When`, e.g.
`Then an order is "cancelled" never happens`. The `Then` predicate is `bad`, and the
correlation key comes from it. (A scoped `when X, then Y never happens` form is a
different, more expressive operator and is out of the current fragment; it is
refused at compile time.) The verdict below is unchanged by this surface syntax.

For key `k`, over its events in canonical order:

- **violated** iff some event satisfies `bad`. (Decided at the first such event.)
- otherwise **pending**. *(A terminal lifecycle event with no prior `bad` event
  would make it `satisfied`; terminal events are out of scope here.)*

## before

`before(prior, trigger)` — the trigger must have been preceded by the prior
condition. `prior`, `trigger` are event predicates.

For key `k`, walk its events in canonical order tracking `seen_prior` (initially
false). At each event `e`, in this order within the event:

- if `prior(e)` then `seen_prior := true`;
- if `trigger(e)` then the verdict is decided now: **satisfied** if `seen_prior`,
  else **violated**.

If no trigger event ever occurs: **pending**. (An event that is both `prior` and
`trigger` sets `seen_prior` before the trigger test, so it satisfies itself.)

## within

`within(trigger, response, seconds)` — after a trigger, a response must occur
before the deadline. The deadline is in **event time**.

For key `k`, let `H` be the clock horizon (global max `event_time`). Walk `k`'s
events in canonical order tracking `armed` (false) and `deadline` (none):

- **before handling `e`**: if `armed` and `e.event_time >= deadline`, the deadline
  has elapsed → **violated** (at `deadline`).
- handling `e`: if not `armed` and `trigger(e)` → `armed := true`,
  `deadline := e.event_time + seconds`. Else if `armed` and `response(e)` →
  **satisfied** (at `e.event_time`; here necessarily `e.event_time < deadline`).

After all of `k`'s events:

- if `armed` and `H >= deadline` → **violated** (the global clock reached the
  deadline with no response);
- else if `armed` → **pending** (deadline not yet reached);
- else (never triggered) → **pending**.

**Deadline boundary.** A response is in time iff its `event_time` is *strictly
less* than `deadline` (= `trigger_time + seconds`). A response exactly at the
deadline is too late: the deadline elapses at `deadline`, and the timeout wins the
tie. Only the **first** trigger arms; only the first response after arming decides.

**Global clock.** `within` is the one place a key is not fully isolated: `deadline`
is event time and event time is a single clock advanced by every event, so a
`within` deadline for key `k` can elapse because another key's event advanced the
clock past it. This is intentional — a real-time deadline in event time is a
statement about how far time has advanced, which is global.

## Scoped never (the Given scope)

`never(bad)` restricted to a scope opened by a `Given` predicate. Authorable
forms:

```gherkin
    Given a user is "locked"
    Then a user is "action" never happens

    Given a user is "locked" until a user is "unlocked"
    Then a user is "action" never happens
```

**The scope-lifetime decision.** Two coherent readings exist: (a) a *latching*
scope — once the scope predicate holds it stays open forever for this entity —
and (b) an *interval* scope that closes on some condition. We implement **both,
explicitly**: the two-line form is latching, because a closing rule the author
did not write must not be guessed; the `until` form names the closing predicate
and gives the interval reading. The scope may open and close repeatedly; the
obligation is active exactly inside open intervals. The motivating trial bug (a
locked user acting on a stale token, in a program with no unlock event) is
caught by the latching form alone; real lock/unlock cycles need `until`.

**Verdict, in trace terms.** Per key, over its admitted events in canonical
order, with scope state `open` (initially false):

- On each event `e`, the scope **state update happens before the forbidden
  check** (consistent with `before`'s same-event rule, where the prior is set
  before the trigger is tested): if not `open` and `scope(e)` then `open :=
  true` (recording `e` as the opening event); else if `open` and a closing
  predicate exists and `close(e)` then `open := false`.
- Then: **violated** iff `open` and `bad(e)` (settle; the deciding events are
  the opening event of the current interval and `e`, deduped when one event is
  both). Otherwise **pending** while the trace continues and **satisfied** at a
  terminal event — including the vacuous case where the scope never opened.

**Edge cases (defined and tested).** An event satisfying both the scope and the
forbidden predicate opens the scope first and therefore violates. An event
satisfying both the closing and the forbidden predicate closes the scope first
and therefore does not violate. A forbidden event before the scope has ever
opened does not violate. Closing and forbidden events at the same timestamp are
distinct events ordered by the canonical tie-break and processed fully in that
order, so the outcome is arrival-independent like everything else.

State is two booleans plus one retained event (the current opening event, for
the explanation) — within the bounded-monitor model. `Given` on the other
operators remains unwired and is refused; `When` with `never` is refused
(never takes a scope, not a trigger).

## Past-time LTL fragment: once, historically, previously, since

These four operators extend the fragment with the standard past-time LTL truth
values. Each carries a constant-size recurrence (one or two booleans) and no
lookback, so it fits the bounded-state monitor model. Predicates are
event-occurrence predicates (`phi(e)` holds iff event `e` matches). For key `k`,
over its admitted events in canonical order, each formula's past-time truth value
updates by the recurrence below; the three-valued settle-once verdict is then
mapped as stated.

### once (existential)

Authorable form: `Then <predicate> has happened` (self-contained, no `When`).

Recurrence: one boolean `o`, init false; on each event `o := o or phi(e)`.

Verdict: **pending** while `o` is false; **satisfied** the moment `o` first becomes
true (settle); **violated** at a terminal event if it never became true.

### historically (universal)

Authorable form: `Then <predicate> always holds` (self-contained, no `When`).

Recurrence: one boolean `h`, init true; on each event `h := h and phi(e)`.

Verdict: **pending** while `h` is true; **violated** the moment `h` first becomes
false (settle); **satisfied** at a terminal event if it never became false.

**Decision 1 — the `historically` reading.** Over event-occurrence predicates,
`historically(phi)` literally means *every event so far has been a phi event*. This
is the exact logical dual of `never`: `historically(phi) = never(not phi)`, and it
settles `violated` on the first event that is not a phi event. It is offered mainly
for authoring symmetry (a universal companion to `never`'s safety and `once`'s
existence). It is kept, not dropped, because its meaning is stated cleanly and its
oracle is the dual of `never`'s; authors should read it as "every event for this
entity is a phi event," which is narrow but coherent.

### previously (triggered)

Authorable form: `When <trigger> / Then <predicate> previously`. The
immediate-predecessor companion to `before` (any-predecessor).

Recurrence: one boolean `p_prev`, init false; the value *at* event `e` is `p_prev`
as it stood before `e` (i.e. `phi` of the immediately preceding event), then
`p_prev := phi(e)`.

Verdict: **pending** until the trigger occurs; at the trigger event, **satisfied**
if the immediately preceding event held `phi`, else **violated**. (If the trigger
is the first event, there is no preceding event, so it is violated.) Like `before`,
an untriggered instance yields no terminal verdict.

### since (safety)

Authorable form: `Then <phi> since <psi>` (self-contained, no `When`); binding is
`phi since psi` — `phi` has held since `psi`.

Recurrence: one boolean `s`, init false; on each event `s := psi(e) or (phi(e) and
s)`. (`psi` held at some past point and `phi` has held at every point since.)

**Decision 2 — the `since` verdict mapping.** Two readings exist:
(a) *existential-established*: satisfied the moment `s` first becomes true; but `s`
becomes true exactly when `psi` first occurs (regardless of `phi`), so this
degenerates to "psi has occurred" and ignores `phi`. (b) *safety-maintained*: once
`psi` occurs, `phi` must continue to hold at every subsequent event; a violation is
the concrete point where `phi` fails after `psi` with no re-anchor. **We choose (b),
safety-maintained**, because it is the useful runtime reading and reports a concrete
safety breach the way `never`, `before`, and `within` do; (a) is degenerate.

Verdict under (b): **pending** until settled; **violated** the first event where the
since-chain breaks — `s` was true and becomes false, i.e. `phi` failed after `psi`
without `psi` re-occurring (settle); **satisfied** at a terminal event if it never
broke, including the vacuous case where `psi` never occurred (no obligation was ever
activated, so nothing was violated).

All four are decided over the admitted events in canonical order, exactly like the
existing operators, so reordering invariance, late admission, and the terminal/GC
interaction carry over unchanged.

## The reordering contract (the correctness property)

The verdict for a set of events depends only on their **canonical order**, never
on the order in which they arrived.

The engine enforces this with a watermark and a grace window: events are buffered
and released in canonical order once the watermark (`max event_time seen − grace`)
passes them. An event that arrives after the watermark has already passed its
`event_time` is **late**: it is recorded and flagged (`engine.late_events`), not
silently slotted in at the wrong place.

Therefore arrival order is irrelevant **provided no event is dropped as late**.
That is guaranteed whenever the trace's event-time span (`max − min`) ≤ the grace
window: then the watermark never passes any event's time and every event is
admitted and canonically ordered. The default grace is 5.0s, so a trace spanning
≤ 5s of event time is fully reorder-invariant regardless of arrival order. The
property tests operate inside this regime.

## First event and empty trace

The first event in canonical order starts an instance. A key with no events yields
no instance and no verdict. An empty trace yields no verdicts.

## The late-event regime (admission)

When the trace's event-time span exceeds the grace window, some events arrive
after the watermark has already passed their time. Those events are **dropped as
late**, and the verdict is then defined over the events that were **admitted**.

**Admission** is a single pass in *arrival order*, with a global watermark (there
is one clock; admission is not per key). Let `max_seen` be the greatest
`event_time` among events admitted so far, and `watermark = max_seen − grace`.
Both start at −∞. For each event `e` in arrival order:

- if `e.event_time < watermark`: `e` is **dropped (late)**. It does not change
  `max_seen` or the watermark, and it is recorded on `engine.dropped_late` /
  counted in `engine.late_events`.
- otherwise `e` is **admitted**: set `max_seen := max(max_seen, e.event_time)` and
  `watermark := max_seen − grace`.

Equivalently: `e` is dropped iff some already-admitted, earlier-arriving event has
`event_time > e.event_time + grace`. The first event in arrival order is always
admitted (watermark = −∞). Events sharing an `event_time` are admitted or dropped
together (admission depends only on `event_time`), so drops occur at whole
time-slices.

**Verdict over admitted events.** The verdict is the pure function defined in the
sections above, computed over the **admitted** events in canonical order. A dropped
late event **does not participate** in the verdict at all — not in ordering, not in
the clock horizon `H` (which is the maximum `event_time` among *admitted* events,
since a dropped event's time is always `< watermark ≤ max_seen`). So a verdict in
this regime is a pure function of the *admitted* set (via canonical order).

**Boundary of the reordering guarantee.** Among admitted events, arrival order is
irrelevant — the verdict depends only on their canonical order. But arrival order
*does* decide which events are admitted versus dropped (event-time-ascending
arrival admits everything and drops nothing; a more out-of-order arrival can drop
more). Therefore, across the full regime, the verdict is a function of arrival
order **only to the extent that arrival order changes the admitted set**. Precisely:

- Two arrival orders that admit the **same** set of events produce the **same**
  verdict (the in-span guarantee, restricted to admitted events).
- Two arrival orders can produce **different** verdicts only if they admit
  **different** sets — i.e. only if their dropped-late sets differ, and at least
  one drop occurred.

**Trustworthiness.** A verdict computed over a trace with drops is **degraded and
explicitly flagged**, never silently wrong: any run that dropped events reports
`engine.late_events > 0` and the dropped events on `engine.dropped_late`. A verdict
that changes with arrival order while nothing was flagged as late would be a
fault. The guarantee is therefore: *the verdict is trustworthy as a statement about
the admitted events, and the presence of any excluded (late) event is always
surfaced* — the caller can see that the stream was too out-of-order for the chosen
grace and widen it or treat the verdict as computed-with-drops.

Drops are purely an out-of-order-arrival phenomenon: if events arrive in
event-time (canonical) order, `engine.late_events == 0` for any grace ≥ 0.

## Terminal retirement and quiescence reclamation (lifecycle × reordering)

Terminal events retire an instance; a quiescence TTL reclaims an instance that has
gone silent. Both must be decided on the **same basis as the verdict** — the
admitted events in canonical order — so that lifecycle behaviour never changes a
verdict away from the pure operator semantics over the admitted canonical trace.

**Everything is processed in canonical order.** Retirement and reclamation are
applied at the point the driving event is *released from the reordering buffer* in
canonical order, not when it *arrives*. Concretely, per admitted event `e` in
canonical order, with `now = e.event_time`, in this order: (1) fire `within`
deadlines that have elapsed (`now ≥ deadline`); (2) reclaim instances quiescent for
`ttl` (`now − last_activity ≥ ttl`); (3) dispatch `e` to its instance; (4) if `e`
is a terminal event, retire the instance for its key.

**Terminal retirement.** When a terminal event for key `k` is reached in canonical
order, the instance for `k` is retired: it emits its terminal verdict and its trace
is dropped. Because every canonically-earlier admitted event for `k` precedes the
terminal, they have already been applied — retirement is consistent with the
verdict. Terminal verdicts: `never` → `satisfied` if it had not already been
violated; `within` → `violated` if armed and unresolved; `before` → no verdict.
After retirement the key is **reusable**: a canonically-later admitted event for `k`
starts a fresh instance (a new lifetime).

**A canonically-earlier event arriving after retirement** is necessarily dropped as
late and flagged, never applied to the retired instance and never silently changing
the verdict. This is forced by the watermark: once the terminal for `k` is released,
the watermark has passed the terminal's `event_time`, so any event with an
`event_time ≤ it` arriving afterward is below the watermark and dropped (§ late
regime). No admitted canonically-earlier event can arrive after retirement.

**Quiescence reclamation (best-effort GC — spec decision).** Quiescence TTL is a
**best-effort, timer-driven memory-reclamation** mechanism, not part of the verdict
guarantee. It reclaims an instance that has gone silent for `ttl`, driven by the
canonical clock (`last_activity` from admitted events in canonical order; a
late-dropped event never refreshes it). Two properties are guaranteed and tested:
reclamation is **arrival-invariant** and **deterministic** — two arrival orders that
admit the same set reclaim the same keys and produce the same verdicts.

What is **deliberately not** guaranteed (recorded here because this round surfaced
it, rather than inferring correctness from the code):

- **Exact reclaim timing is implementation-defined.** Reclamation is driven by a
  lazy timer queue: a TTL timer is scheduled at `last_activity + ttl` on each
  dispatch that produces no verdict, and consumed when due; if it fails
  re-validation at that instant it is discarded. Consequently some instances that
  are eligible under a naive "reclaim at `last_activity + ttl`" reading are not
  reclaimed (a memory-efficiency limitation, tracked separately as timer-purge
  robustness — out of scope for correctness). For `never`/`before` this is
  verdict-neutral; the exact reclaimed set is therefore **not** asserted against
  the intended-semantics oracle.
- **TTL can suppress a `within` timeout.** If an entity goes quiescent before its
  `within` deadline, its instance may be reclaimed (no verdict) instead of timing
  out to `violated`. This is arrival-invariant and deterministic but depends on the
  TTL-vs-deadline relationship. **Operational guidance:** set `ttl` larger than any
  `within` deadline so obligations are not reclaimed out from under their deadline.

The verdict guarantee — that the verdict equals the pure operator semantics over
the admitted canonical trace — holds for **terminal retirement** and for the
**no-TTL** case; TTL reclamation is a GC action layered on top, whose *only*
guaranteed properties are arrival-invariance and determinism.

**Consistency requirement (the property tested).** For every trace and arrival
order, the per-key verdict, the set of keys retired, and the set reclaimed are all
decided on the admitted-canonical basis. Adding terminal or TTL behaviour never
changes a verdict from what the operator semantics over the admitted canonical
trace give, and never silently discards a canonically-earlier admitted event —
any excluded event is flagged as late.
