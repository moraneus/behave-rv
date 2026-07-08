# behave_rv

**Runtime verification driven by human readable Gherkin.**

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-BSD%202--Clause-green)
![Status](https://img.shields.io/badge/status-pre--alpha-orange)

behave_rv turns Gherkin into runtime monitors. A human writes a policy in the same
`Feature` / `Scenario` / `When` / `Then` language used for behaviour driven
development, and a deterministic engine evaluates that policy against a live or
recorded stream of events, emitting a verdict for every entity it observes and
explaining each violation in the author's own words.

## Table of contents

- [Introduction](#introduction)
- [Key features](#key-features)
- [The vocabulary and reserved words](#the-vocabulary-and-reserved-words)
- [Installation](#installation)
- [Project structure](#project-structure)
- [Writing a policy](#writing-a-policy)
- [Implementing the monitorable steps](#implementing-the-monitorable-steps)
- [Running the monitor](#running-the-monitor)
- [How stability across code change works](#how-stability-across-code-change-works)
- [Semantics and correctness](#semantics-and-correctness)
- [Limitations and scope](#limitations-and-scope)
- [License](#license)
- [Acknowledgements](#acknowledgements)

## Introduction

behave_rv is a runtime verification framework, not a testing tool. This distinction
is the whole point, so it is worth stating plainly and early.

In behaviour driven development you write Gherkin scenarios and a runner executes
them once against your code, then reports pass or fail. In behave_rv the Gherkin
does not drive a test run. It describes a **monitor**: a property that should hold
over the events a running system emits. The engine watches an event stream,
maintains one independent monitor instance per entity, and produces a three valued
verdict (`satisfied`, `violated`, or `pending`) for each entity, together with an
explanation for every violation.

Three roles keep this honest and separate:

- **The code** exposes a monitorable surface. Small, side effect free predicates
  observe events and bind a correlation key. These are the taps the monitor reads.
- **A human** writes the policies in Gherkin, using a fixed temporal vocabulary
  plus the predicates the code exposes.
- **A deterministic engine** evaluates the policies. There is no language model
  anywhere in the evaluation path. The same trace always produces the same verdict.

behave_rv reuses the Gherkin parser and model from
[`behave`](https://github.com/behave/behave) as a dependency and replaces
everything downstream of parsing with a runtime engine.

## Key features

- **Gherkin authoring surface.** Policies are ordinary `.feature` files. No Python
  is written to define a policy; the code only exposes the predicates.
- **Deterministic per key engine.** One monitor instance per correlation key value.
  No language model in the per event path.
- **Three valued verdicts.** `satisfied`, `violated`, and `pending`, so an
  obligation that has not yet resolved on a finite prefix is reported honestly.
- **Event time reordering by default.** Verdicts are decided on event time, not
  arrival order, using a watermark with a grace window.
- **Specification stability across code change.** Steps carry behavioural
  signatures and policies bind to stable step identities, so renames are absorbed
  silently while a genuine change in behaviour surfaces as a scoped notification.
- **Explanations as the authored scenario.** A violation is rendered as the human's
  own scenario with the failing step marked and the real event values shown.

## The vocabulary and reserved words

A policy is written with a small, fixed vocabulary. This section walks through each
reserved word with a short definition and a small example. All phrasings below are
the ones the compiler actually accepts today.

### Feature and Scenario

The two structural keywords carried unchanged from Gherkin. A `Feature` groups
related policies; each `Scenario` is exactly one policy (one monitor).

```gherkin
Feature: payment safety

  Scenario: an order may only be paid after it was authorized
    When an order is "paid"
    Then an order is "authorized" before
```

### When (the trigger)

`When` names the event that triggers the obligation. It refers to a registered
predicate and binds the correlation key for the entity being monitored. In the
example above, `When an order is "paid"` fires whenever a payment event is observed
for an order.

### Then (the obligation)

`Then` states the property that must hold. Every operator is predicate-first with a
temporal suffix. `before` and `within` also take a `When` trigger; `never` is
self-contained and takes no `When` (its own predicate is the forbidden event).

### Temporal operators

The complete authoring reference, with satisfying and violating traces for
every form, is [`docs/OPERATORS.md`](docs/OPERATORS.md).

| Operator | Authorable phrasing | Meaning (see `SEMANTICS.md`) |
| --- | --- | --- |
| `never` | `Then <predicate> never happens` (no `When`) | The named event must never occur for this entity. Violated the first time it does; otherwise pending. |
| scoped `never` | `Given <predicate>` (latching) or `Given <predicate> until <predicate>` (interval), then `Then <predicate> never happens` | The obligation is active only while the `Given` scope is open. The two-line form latches (once open, open forever); the `until` form closes the scope at the named event and may reopen. Violated at the first forbidden event inside an open scope; satisfied at a terminal event otherwise, including when the scope never opened. |
| `once` | `Then <predicate> has happened` (no `When`) | The named event must occur at some point (existential). Satisfied the first time it does; pending until then; violated at a terminal event if it never did. |
| `historically` | `Then <predicate> always holds` (no `When`) | Every event for this entity must be a `<predicate>` event (universal, the dual of `never`). Violated the first event that is not; pending until then; satisfied at a terminal event if none. |
| `previously` | `Then <predicate> previously` | The event immediately before the `When` trigger must have been a `<predicate>` event (immediate predecessor, companion to `before`). Satisfied or violated at the trigger; pending until it occurs. |
| `since` | `Then <phi> since <psi>` (no `When`) | After `<psi>` occurs, `<phi>` must hold at every event thereafter (safety). Violated the first event where `<phi>` fails after `<psi>`; pending until then; satisfied at a terminal event if never broken. |
| `before` | `Then <predicate> before` | The `When` event must have been preceded by the named condition for this entity. Satisfied if it was, violated at the trigger if it was not, pending until the trigger occurs. |
| `within` | `Then <predicate> within "<n>" seconds` | After the `When` event, the named response must occur strictly before the deadline `trigger_time + n`. Satisfied if it does, violated when the deadline elapses with no response, pending before either. |

Small examples:

```gherkin
    Then an order is "cancelled" never happens
    Then an order is "audited" has happened
    Then an order is "valid" always holds
    Then an order is "paid" since an order is "authorized"
```

```gherkin
    Given a user is "locked" until a user is "unlocked"
    Then a user is "action" never happens
```

```gherkin
    When an order is "paid"
    Then an order is "authorized" before

    When an order is "paid"
    Then an order is "authorized" previously
```

```gherkin
    When a delivery is "requested"
    Then a delivery is "fulfilled" within "30" seconds
```

### The correlation key

Every registered predicate declares a correlation key (for example `order_id`). The
engine shards the stream by that key, so each order is monitored independently. A
scenario uses exactly one correlation key, which may be a composite tuple (for
example an order plus a line item as a single identity). The key is taken from the
predicates the scenario uses; you do not write it in the `.feature` file.

### Recognized but not yet supported

The following are recognized and refused at compile time with a clear message,
rather than silently accepted. This keeps a policy author from writing something
that will not run.

| Construct | What happens |
| --- | --- |
| `Given` on operators other than `never` | Refused: `Given/scope steps are only wired for 'never' so far; other operators do not take a scope yet: ... Express the property with When/Then for now.` |
| `When` with `never` | Refused: `a 'never' policy takes a Given scope, not a When trigger (...). To restrict the obligation to a scope, write 'Given <predicate>' (or 'Given <predicate> until <predicate>') before 'Then <predicate> never happens'.` |
| `And` / `But` multi step scenarios | Refused: `a policy needs exactly one Then step, found N`, or `a '<operator>' policy needs exactly one When step, found N`. |
| A scoped self-contained operator with a `When` (for example `never`, `once`, `historically`, `since`) | Refused: `a '<operator>' policy is self-contained and must not have a When step (...); write the property as a single Then.` |
| Any unrecognized `Then` obligation | Refused: `unrecognized temporal obligation: ... Supported forms: '<step> never happens', '<step> has happened', '<step> always holds', '<step> previously', '<step> since <step>', '<step> within "<n>" seconds', '<step> before'.` |
| Cross entity policies (two independent keys) | Refused: `scenario '...' references more than one entity key [...]; the fragment is one correlation key per scenario.` |

## Installation

behave_rv requires Python 3.10 or newer and depends on `behave`. It is pre alpha
and not yet published to PyPI, so install it from source.

```bash
git clone https://github.com/moraneus/behave-rv.git
cd behave-rv
pip install .
```

For development, including the test and lint tools:

```bash
pip install -e ".[dev]"
```

If you use [uv](https://github.com/astral-sh/uv), the equivalents are
`uv pip install .` and `uv pip install -e ".[dev]"`, and you can run any command in
this README by prefixing it with `uv run`.

## Project structure

The runtime lives in the `behave_rv` package. A working policy needs three things:
a steps module (the taps), a `.feature` policy, and an event stream. The `examples`
directory holds a complete, runnable set.

```text
behave-rv/
  behave_rv/                the runtime package
    steps/                  the RV step decorators (trigger, scope, obligation)
    catalog/                step registry, behavioural signatures, catalog, diff
    compile/                Gherkin to automaton compiler and parser bridge
    engine/                 event loop, dispatch, timers, garbage collection
    events/                 event model, sources (in-process, replay), watermark
    verdict/                verdict record, explanation renderer, sinks
    notify/                 break, weakening, and suggestion channels
    expose/                 exposure API (currently a stub, see Limitations)
    __main__.py             the command line entry point
  examples/
    order_steps.py          the monitorable steps for the order example
    order_authorized.feature a policy
    order_trace.jsonl       a recorded event stream
  tests/                    unit and property based tests
  SEMANTICS.md              the operator semantics, in trace terms
  LICENSE
  NOTICE
  pyproject.toml
```

## Writing a policy

A policy is a `.feature` file. Here is the committed order example,
`examples/order_authorized.feature`:

```gherkin
Feature: payment safety

  Scenario: an order may only be paid after it was authorized
    When an order is "paid"
    Then an order is "authorized" before
```

Reading it line by line:

- `When an order is "paid"` is the **trigger**. It fires on a payment event and
  binds `order_id`, the correlation key for the predicate it resolves to.
- `Then an order is "authorized" before` is the **obligation**. The operator is
  `before`, and its operand is the predicate `an order is "authorized"`. Together
  they require that, for the same order, an authorization was observed before the
  payment.

The verdict for an order is `satisfied` if it was authorized before it was paid,
`violated` if it was paid with no prior authorization, and `pending` if it has not
yet been paid.

## Implementing the monitorable steps

The predicates a policy uses are registered in a small Python module. Here is the
committed `examples/order_steps.py`:

```python
"""The agent's monitorable surface for the order example.

Importing this module registers the RV steps into the default registry. This is
the only Python in the policy path; the policy itself is authored in Gherkin.
"""

from behave_rv.steps import default_registry, trigger


@trigger('an order is "{status}"', step_id="order.status.is",
         event_type="order.status", correlation_key="order_id")
def order_status_is(ctx, event, status):
    """Matches an order.status event carrying the given status, binding order_id."""
    if event.type == "order.status" and event.payload.get("status") == status:
        ctx.bind(order_id=event.bindings["order_id"])
        return True
    return False


# A second wording for the SAME step_id. A policy may use either phrasing; both
# bind to order.status.is, so a rephrasing flows through untouched.
default_registry.alias("order.status.is", 'the order reaches "{status}"')
```

The step contract is **inverted** relative to classic behave, and the difference
matters. In behave, a `@when` step performs an action and a `@then` step asserts a
result, both with side effects, run once. In behave_rv a step is a **pure
predicate**: it observes an event, returns a boolean, and mutates nothing outside
itself. It is evaluated continuously over the stream rather than executed once.

Three decorators are available: `trigger` (a `When`), `scope` (a `Given`), and
`obligation` (a `Then`). Today the compiler wires `trigger` predicates into `When`
steps and into the operands of `before` and `within`; `scope` is recognized but
refused (see the vocabulary table). A trigger also binds the correlation key by
calling `ctx.bind(...)`, which is how the engine knows which entity an event
belongs to.

## Running the monitor

Point the command line at a steps module, a policy, and a recorded trace:

```bash
python -m behave_rv \
  --steps  examples/order_steps.py \
  --policy examples/order_authorized.feature \
  --trace  examples/order_trace.jsonl
```

The recorded trace `examples/order_trace.jsonl` contains three orders: `A` is
authorized then paid, `B` is paid with no authorization, and `C` is authorized but
never paid. Running the command above produces the following, verbatim.

The verdict log, one JSON object per verdict:

```json
{"policy_id": "an order may only be paid after it was authorized", "entity_key": {"order_id": "A"}, "verdict": "satisfied", "trigger_event": {"type": "order.status", "event_time": 2.0, "bindings": {"order_id": "A"}, "payload": {"status": "paid"}, "source": "recorded"}, "witnessing_trace": [{"type": "order.status", "event_time": 1.0, "bindings": {"order_id": "A"}, "payload": {"status": "authorized"}, "source": "recorded"}, {"type": "order.status", "event_time": 2.0, "bindings": {"order_id": "A"}, "payload": {"status": "paid"}, "source": "recorded"}], "at": 2.0}
{"policy_id": "an order may only be paid after it was authorized", "entity_key": {"order_id": "B"}, "verdict": "violated", "trigger_event": {"type": "order.status", "event_time": 3.0, "bindings": {"order_id": "B"}, "payload": {"status": "paid"}, "source": "recorded"}, "witnessing_trace": [{"type": "order.status", "event_time": 3.0, "bindings": {"order_id": "B"}, "payload": {"status": "paid"}, "source": "recorded"}], "at": 3.0}
{"policy_id": "an order may only be paid after it was authorized", "entity_key": {"order_id": "C"}, "verdict": "pending", "trigger_event": null, "witnessing_trace": [{"type": "order.status", "event_time": 4.0, "bindings": {"order_id": "C"}, "payload": {"status": "authorized"}, "source": "recorded"}], "at": 4.0}
```

A liveness note for any registered step whose event never appeared in the stream:

```text
# liveness (steps never observed in this stream — possibly dead/wrong)
  customer.tier.is  (event 'customer.status')
```

And the explanation for the violation, rendered as the authored scenario with the
failing step marked and the real event trace shown:

```text
POLICY 'an order may only be paid after it was authorized'  ENTITY order_id=B  VERDICT violated @ t=3.0
Scenario: an order may only be paid after it was authorized
    When an order is "paid"
✗ Then an order is "authorized" before   # violated
Trace:
  t=3.0  order.status  {'status': 'paid'}
```

Verdicts are decided on event time, using a reordering window with a default grace
of 5.0 seconds, so correct ordering is the default even when events arrive out of
order within that window. A scenario that steps outside the single key fragment is
refused before it runs:

```bash
python -m behave_rv \
  --steps  examples/order_steps.py \
  --policy examples/cross_entity.feature \
  --trace  examples/order_trace.jsonl
```

```text
compile error: scenario 'an order may only be paid after the customer is gold' references more than one entity key [('customer_id',), ('order_id',)]; the fragment is one correlation key per scenario
```

The process exits with a non zero status in this case.

## Live monitoring

For a running service, use the subscription source and a sink. The service
pushes events from its own thread; the engine blocks while the stream is quiet
and delivers each verdict the moment it is decided. `close()` ends the stream,
flushing the reorder buffer so armed deadlines the horizon has passed resolve
instead of being lost.

```python
from behave_rv.engine.loop import Engine
from behave_rv.events.sources.subscription import QueueSource
from behave_rv.verdict.sinks import PrintSink

source = QueueSource()            # service calls source.push(event) as it runs
engine = Engine(policies, terminal_event_types={"order.done"})
engine.run(source, sink=PrintSink(policies))   # violations print as they happen
```

With a sink supplied, `run()` does not also accumulate the verdict list (it
returns an empty list; `engine.verdicts_delivered` counts deliveries), so a long
run's verdicts live on disk or in your handler rather than in memory. A sink
that raises is recorded on `engine.sink_errors` and evaluation continues.

## How stability across code change works

This is the distinctive feature. When the code that exposes predicates changes,
human policies should not rot in silence. behave_rv keeps them aligned with a
behavioural signature.

Each registered step has a stable `step_id` and a **behavioural signature**: the
event type it observes, the fields a policy can reference, the correlation key, the
exposed payload fields, and a rename invariant fingerprint of the predicate body.
Policies bind to the `step_id`, not to the phrasing text.

After a change, the new catalog is diffed against the committed one:

- A **rename** of a function, a variable, or the phrasing, with no change to the
  event, its fields, or the matching condition, leaves the signature equal. It is
  absorbed silently. No notification is produced.
- A **semantic change**, such as renaming a referenced field, changing the event
  type, widening the correlation key, or adding a guard inside the predicate body,
  changes the signature. It surfaces as a **break** notification, scoped only to the
  policies that used that step, carrying a human readable contract diff.

The notification channel has three separate streams so they never blur together:
**break** (a step a policy uses changed signature or was removed), **weakening** (an
agent owned behaviour test changed what it asserts), and **suggestion** (new
monitorable behaviour that no policy covers yet). The alias in the example steps
module demonstrates the binding to identity: two different wordings resolve to the
same `step_id` and run identically.

## Semantics and correctness

The meaning of each operator is specified in trace terms in
[`SEMANTICS.md`](SEMANTICS.md), independently of the implementation. That
specification covers `never`, `before`, and `within`, the correlation key scoping,
the event time reordering contract, the late event admission rules, and the
interaction of terminal retirement and quiescence reclamation with reordering.

The implementation is validated against that specification by property based tests
(Hypothesis) that check the engine's verdict against an independent oracle across
large generated input spaces, including adversarial event orderings. The tested
fragment is the implemented one: the `never`, `before`, and `within` operators over
a single correlation key, with event time reordering, late event drops, terminal
events, and quiescence. The full test suite is 121 tests at the time of writing.
This is strong evidence over the tested space, not a proof, and the space it covers
is stated plainly rather than overclaimed.

## Limitations and scope

behave_rv is a correct, honestly scoped first version. Its boundaries:

- **Single key fragment.** One correlation key per scenario (a composite tuple is
  allowed). Policies that quantify over two independent entities are refused at
  compile time.
- **Operator set.** `never` (plain and `Given`-scoped, latching or `until`),
  `before`, `within`, and the past-time LTL fragment `once`, `historically`,
  `previously`, and `since` are implemented. Future-time liveness beyond the
  bounded `within` is deliberately out of the monitorable fragment (an unbounded
  future property has no defined verdict on a finite prefix).
- **Grammar subset.** Exactly one `Then` per scenario, plus one `When` for
  `before`, `within`, and `previously`, and at most one `Given` (wired for
  `never` only). `Given` on the other operators, `When` with `never`, and
  `And` / `But` multi step scenarios are recognized but refused.
- **Exposure API is a stub.** The design envisions a dedicated `@emits`, `@entity`,
  and `@terminal` exposure API. That module is currently a stub. The real
  monitorable taps today are the step decorators (`trigger`, `scope`, `obligation`).
- **Liveness boundary.** Liveness is two level: a compile time warning fires when
  a policy's event type never appeared in the observed stream, and also when the
  type appeared but a concrete value the policy binds (for example
  `status="locked"`) never did, so a renamed status value no longer disconnects a
  policy silently. The honest boundary: liveness warns against a representative
  stream (a replay or a live sample); it cannot predict that a value which has
  never yet appeared never will.
- **Robustness items not yet built.** Timer heaps are not purged, and the engine
  loop is single threaded rather than sharded per key. On live sources
  (`QueueSource`) a `within` deadline also fires on wall time while the stream is
  quiet (the verdict's `at` stays the deadline's event time); on replay sources
  only event time drives deadlines, exactly as before.
- **Event sources.** In process, replay, and subscription (queue) sources are
  implemented. The OpenTelemetry and structured log sources are stubs.

## License

behave_rv is licensed under the BSD 2-Clause License. It derives from `behave`,
which is also BSD 2-Clause, and reuses only its Gherkin parser and model. The
original copyright notice is retained. See [`LICENSE`](LICENSE) for the full text
and [`NOTICE`](NOTICE) for the derivation.

## Acknowledgements

behave_rv stands on the Gherkin parser and model of the
[`behave`](https://github.com/behave/behave) project. The runtime verification
design, the temporal engine, the signature based stability mechanism, and the
verdict explanations are new work built alongside it.
