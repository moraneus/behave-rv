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

- **Phase 0** — Fork, license hygiene, behave's parser/model usable as a library.
- **Phase 1** — Event model; in-process emitter + replay sources.
- **Phase 2** — RV step decorators (`trigger`, `scope`, `obligation`) + catalog/signatures.
- **Phase 3** — Temporal vocabulary (`never`, `always`, `before`, `within`); compiler; engine.
- **Phase 4** — Verdicts + explanation renderer.
- **Phase 5** — Garbage collection + bounded explanation retention.
- **Phase 6** — Signature diffing + notification channel.

**First end-to-end target:** one entity type, one correlation key, a handful of
annotated taps, the `never` and `within` operators, replay mode, and verdict
logs with explanations. Prove it on recorded logs before pointing it at live
traffic.

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

Pre-alpha scaffolding. The package tree mirrors the design; modules are stubs
to be filled in following the build sequence above.
