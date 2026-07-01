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
