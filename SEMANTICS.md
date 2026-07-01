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
