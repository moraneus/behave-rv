# behave-rv

> Runtime verification whose authoring surface is Gherkin. A modified fork of [`behave`](https://github.com/behave/behave).

`behave-rv` is **not** an offline BDD test runner. It is an always-on monitor
over a live system. An agent writes application code that is *monitorable by
construction*; a human writes verification policies in Gherkin using a fixed
temporal vocabulary plus the steps produced during development; and a
deterministic engine evaluates those policies at runtime against the live event
stream, emitting verdicts that say which policy was satisfied or violated, for
which entity, and why.

The "why" is the human's own scenario, replayed with the real event values, with
the failing step marked.

## The three roles

1. **The guided agent** writes application code and exposes a monitorable
   surface (events, taps, a step catalog) as it goes. It may suggest policies
   but does not own them.
2. **The human** owns the verification policies, written in Gherkin from the
   built-in vocabulary plus the development-time catalog.
3. **The deterministic engine** owns the verdict. The same trace produces the
   same verdict every time.

**The one hard line:** the language model lives at build time and authoring time
only. It is banished from the per-event runtime evaluation path.

## Design principle: specification stability under autonomous code change

When an agent rewrites code continuously, human-authored monitors normally rot
in silence — a renamed event quietly stops matching and the policy goes dormant
while looking healthy. `behave-rv` makes the monitorable surface a **versioned
behavioral interface** between the agent's code and the human's policies:

- Representational change (renames, refactors) is absorbed silently.
- Semantic change (the observable behavior a policy depends on) is surfaced.

When in doubt, the system turns a silent gap into a visible warning.

## Package layout

```
behave_rv/
  catalog/        the step catalog and signature system
  events/         the normalized event model and pluggable sources
  expose/         the library API the agent calls in application code
  steps/          the RV step contract + built-in temporal vocabulary
  compile/        scenario -> per-key monitor automaton
  engine/         the always-on runtime (loop, dispatch, instances, timers, gc)
  verdict/        verdict records, explanation rendering, sinks
  notify/         build-time notifications (break / weakening / suggestion)
  vendor_behave/  behave reused unchanged (Gherkin parser + model)
```

## Build sequence

- **Phase 0** ✅ — Fork, license hygiene, behave's parser/model usable as a library.
- **Phase 1** ✅ — Event model; in-process emitter + replay sources.
- **Phase 2** ✅ — RV step decorators (`trigger`, `scope`, `obligation`) + catalog/signatures.
- **Phase 3** ✅ — Temporal operators (`never`, `within`); engine (loop, dispatch, per-key instance, timer wheel).
- **Phase 4** ✅ — Verdicts + explanation renderer + JSON sink.
- **Phase 5** ✅ — Garbage collection (terminal + quiescence TTL) + bounded explanation retention.
- **Phase 6** ✅ — Signature diffing + notification channel (break / weakening / suggestion).

**First end-to-end target:** one entity type, one correlation key, a handful of
annotated taps, the `never` and `within` operators, replay mode, and verdict
logs with explanations. Prove it on recorded logs before pointing it at live
traffic.

## Authoring a policy in Gherkin (the closed loop)

A human writes a policy as a `.feature` file; the engine compiles and runs it
against a recorded (or live) event stream with **no Python policy construction in
the path**. The agent's only Python is the monitorable surface — the registered
RV steps.

```gherkin
# examples/order_authorized.feature
Feature: payment safety
  Scenario: an order may only be paid after it was authorized
    When an order is "paid"
    Then an order is "authorized" before
```

```bash
python -m behave_rv \
  --steps  examples/order_steps.py \
  --policy examples/order_authorized.feature \
  --trace  examples/order_trace.jsonl
```

This emits a verdict per entity — `satisfied`, `violated` (rendered back as the
authored scenario with the failing step marked and the real event values), and
`pending` — and refuses a scenario that needs more than one independent entity key
at compile time.

**v1 policy grammar.** One scenario = one policy: exactly one `When` (a registered
step, the trigger) and one `Then` (a temporal obligation). The obligations wired
today are `it must never happen` (`never`), `<step> within "<n>" seconds`
(`within`), and `<step> before` (`before`/precedence). Steps resolve by stable
`step_id`, so a rephrasing that maps to the same `step_id` still compiles.
`Given`/scope steps are recognized but **not yet wired** into the operators, and
are refused at compile time with a clear message rather than silently ignored.

## Non-negotiables

1. No language model in the per-event runtime path.
2. The model lives at build and authoring time only.
3. Exposure is additive — never reshape business logic to make it observable.
4. Do not extend behave's runner; wrap and replace it. Reuse only its parser and model.
5. RV steps are pure: observe and return a boolean, no side effects.
6. Monitorable fragment only — every authorable sentence has a defined verdict.
7. One correlation key per scenario in v1 (possibly a tuple for composite identity).
8. The catalog is a committed, versioned interface; signature changes to used steps surface.
9. Event time, not receipt time, for ordering and deadlines.
10. Keep the human's policies and the agent's tests separate.

## License

BSD 2-Clause. Derives from `behave` (Copyright Benno Rice, Richard Jones, Jens
Engel and others); see [`NOTICE`](NOTICE) and [`LICENSE`](LICENSE).

## Status

The full build sequence (Phases 0–6) is implemented and tested end to end: a
recorded trace replays through the deterministic engine and produces per-entity
`never`/`within` verdicts with authored-scenario explanations, instances are
reclaimed on terminal events and quiescence, and a catalog signature diff surfaces
breaks, weakenings, and suggestions.

The authoring surface and the runtime are connected: a `.feature` policy compiles
to the same `compile.Policy` the engine runs (see "Authoring a policy in Gherkin"
above and `examples/`).

Deferred (per the design's "defer until the core loop is proven"): the
OpenTelemetry and structured-log sources beyond their stubs, the remaining
temporal operators (`always`, `since`, `previously`), `Given`/scope wiring,
multi-step (`And`/`But`) scenarios, and cross-entity / aggregate policies (the
first-order backend slot).

Run the tests with `pytest` (or `uv run --with pytest python -m pytest`).
