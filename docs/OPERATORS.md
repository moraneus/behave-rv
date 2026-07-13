# The behave_rv temporal vocabulary — complete reference

Seven `Then` operators plus two `Given` scope forms. `until` is not a
standalone operator: it exists only inside the `Given ... until ...` scope.
This page is the authoring reference; [`SEMANTICS.md`](SEMANTICS.md)
states the same semantics in formal trace terms, and the examples below are
live in the [demos](../demo/README.md).

## The shape of every policy

One scenario = one policy = one entity (a single correlation key, e.g.
`order_id`). Three step slots:

```gherkin
Scenario: <policy name — becomes the policy_id>
  [Given <scope step> [until <closing step>]]     # optional, ONLY with "never happens"
  [When  <trigger step>]                          # required for before/within/previously, forbidden otherwise
  Then   <predicate step> <temporal suffix>       # exactly one, always
```

Every verdict is three-valued: **satisfied**, **violated**, or **pending**
(undecided on the stream so far). A violation is explained by replaying your
own scenario with the failing step marked and the deciding events attached.

## The complete table — all nine authorable forms

| # | Form | Needs | Meaning | Example |
|---|------|-------|---------|---------|
| 1 | `<p> never happens` | — | `<p>` must not occur, ever, in this entity's lifetime | `Then an order is "double_charged" never happens` |
| 2 | `<p> has happened` | — | `<p>` must occur at some point (eventually) | `Then an order is "invoiced" has happened` |
| 3 | `<p> always holds` | — | every event on this entity satisfies `<p>` (an invariant) | `Then a sync is "sync_ok" always holds` |
| 4 | `<p> since <q>` | — | from the moment `<q>` occurs, every later event must satisfy `<p>` | `Then a user is "review" since a user is "flagged"` |
| 5 | `<p> before` | `When` | when the trigger fires, `<p>` must already have happened | `When an order is "paid"` / `Then an order is "authorized" before` |
| 6 | `<p> within "N" seconds` | `When` | after the trigger, `<p>` must arrive within N seconds; the timeout is the violation | `When an order is "cancelled"` / `Then an order is "refunded" within "5" seconds` |
| 7 | `<p> previously` | `When` | the event immediately before the trigger satisfied `<p>` | `When a user is "locked"` / `Then a user is "login_fail" previously` |
| 8 | `Given <s>` + `<p> never happens` | `Given` | **latching scope**: once `<s>` occurs, `<p>` is forbidden forever after | `Given an order is "cancelled"` / `Then an order is "shipped" never happens` |
| 9 | `Given <s> until <c>` + `<p> never happens` | `Given` | **interval scope**: `<p>` is forbidden only while the scope is open; `<c>` closes it, `<s>` can re-open it | `Given a user is "locked" until a user is "unlocked"` / `Then a user is "action" never happens` |

## Each form in detail, with traces

### 1. `never happens` — a standing prohibition

```gherkin
Scenario: an order is never double charged
  Then an order is "double_charged" never happens
```

- ✓ `created → authorized → paid → ... → done` — settles **satisfied** at the
  terminal: nothing forbidden ever occurred.
- ✗ `created → paid → double_charged` — **violated** the instant the forbidden
  event arrives.
- Live, before anything happens: **pending** (holding).

### 2. `has happened` — an eventual obligation

```gherkin
Scenario: every order is eventually invoiced
  Then an order is "invoiced" has happened
```

- ✓ `created → ... → invoiced` — **satisfied** the moment it occurs.
- ⏳ `created → authorized → paid` and the stream continues — **pending**, and
  correctly so: "eventually" can never be false while the entity still lives.
- ✗ `created → authorized → paid → done` — **violated** at the terminal: the
  entity's story ended and it never happened.

### 3. `always holds` — a per-entity invariant

```gherkin
Scenario: sync always succeeds this session
  Then a sync is "sync_ok" always holds
```

- ✓ `sync_ok, sync_ok, sync_ok, ...` — pending/holding, settles satisfied at end.
- ✗ `sync_ok, sync_ok, sync_fail` — **violated** on the third event.
- Design note: it checks **every event routed to this key**, so it belongs on
  a homogeneous stream (the demo keys it on `session_id`, where only sync
  outcomes flow). On a mixed stream like `order.status` it would violate at
  the first event that isn't the named value.

### 4. `since` — a regime that begins at an anchor

```gherkin
Scenario: a flagged order is only reviewed afterwards
  Then an order is "reviewed" since an order is "fraud_flagged"
```

- Inactive (pending) until the anchor `fraud_flagged` is seen; ✓ if it never is.
- ✓ `created → flagged → reviewed` — holding.
- ✗ `created → authorized → flagged → paid` — **violated**: the order
  progressed while quarantined.
- Both steps live in the one `Then` line; there is no `When`.

### 5. `before` — a precondition on ordering

```gherkin
Scenario: an order may only be paid after it was authorized
  When an order is "paid"
  Then an order is "authorized" before
```

- ✓ `created → authorized → paid`.
- ✗ `created → paid` — violated the instant `paid` arrives.
- Direction matters: the `When` is the *later* event you're guarding; the
  `Then` is what must already be in its past. Never fires at all if the
  trigger never occurs.

### 6. `within "N" seconds` — a bounded deadline

The only future-looking operator, deliberately bounded so it always has a
verdict.

```gherkin
Scenario: a cancelled order is refunded within the window
  When an order is "cancelled"
  Then an order is "refunded" within "5" seconds
```

- ✓ `cancelled(t=2.4) → refunded(t=4.1)`.
- ✗ `cancelled(t=2.4)` then **silence** — violated at t=7.4 by the engine's
  timer, on the wall clock, with no event arriving. Absence is the violation.
- Syntax detail: the quotes are optional, decimals are allowed, and `second` /
  `seconds` both parse — `within 2.5 seconds` is valid.

### 7. `previously` — the immediate predecessor, exactly one step back

```gherkin
Scenario: a lockout follows a failed attempt
  When a user is "locked"
  Then a user is "login_fail" previously
```

- ✓ `fail → fail → fail → locked` — the event right before the lock is a failure.
- ✗ `login_ok → locked` — violated even if a failure happened ten minutes
  earlier, because `previously` means *the prior event*, not "sometime in the
  past." When you mean the latter, use `before`.

### 8. `Given` (latching) — once opened, forever forbidden

```gherkin
Scenario: a cancelled order is never shipped
  Given an order is "cancelled"
  Then an order is "shipped" never happens
```

- ✓ `created → cancelled → refunded` — scope open, nothing forbidden.
- ✗ `created → paid → cancelled → refunded → shipped` — violated: cancellation
  is permanent; the shipment being "late" doesn't excuse it.
- Before the scope ever opens, the forbidden event is fine: `shipped` on a
  never-cancelled order triggers nothing here.

### 9. `Given ... until ...` (interval) — forbidden only while open

```gherkin
Scenario: a user must not act while locked, until unlocked
  Given a user is "locked" until a user is "unlocked"
  Then a user is "action" never happens
```

- ✓ `locked → review → unlocked → action` — the scope closed before the
  action. (Note: the *latching* form of this same rule **would** fire here;
  running both side by side is exactly how the session demo shows the
  difference.)
- ✗ `locked → action` — violated inside the open interval.
- ✗ `locked → unlocked → locked → action` — violated: the second lock
  re-opened the scope, and the violation's deciding events anchor at the
  **re**-lock, not the first one.

## What settles a pending verdict

A live entity can hold pending obligations indefinitely (`has happened`
unfulfilled, `since` holding, `never` unbreached). Two things settle them:
the entity's **terminal event** (e.g. `order.done`, `session.end` —
prohibitions settle satisfied, unfulfilled eventualities settle violated), or
the **end of a replay** run with `emit_pending=True`, which reports the honest
pending state.

## What the compiler refuses — on purpose

Everything not in the table above is rejected at compile time with a reason,
never mis-evaluated: `Given` on any operator other than `never happens`;
combining `Given` with a `When`; more than one `Given` or `Then`; a `When` on
the self-contained operators (1–4); a missing `When` on the triggered ones
(5–7); any scenario whose steps span **two different correlation keys**
(cross-entity rules are outside the fragment); aggregates and counting; and
any step text that doesn't resolve — or resolves ambiguously — against the
registered step catalog. The refusal is the guarantee's other half: because
everything expressible has a defined verdict, everything accepted can be
trusted.
