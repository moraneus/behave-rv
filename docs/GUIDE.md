# Using behave_rv in your code — the complete guide

behave_rv watches your running application and tells you, deterministically
and with evidence, whether the rules you wrote in plain Gherkin hold — per
entity, live or over a recorded trace. This guide covers everything a user
needs: exposing events, writing steps and policies, running the engine, every
option, and how to watch what the monitor is doing while your app runs.

Setup: `pip install -e .` from a clone (dependencies: `behave`, `parse`).
Nothing else is needed — the live dashboard below is standard library only.

## 1. The five-minute quickstart

One complete, runnable program (committed as `examples/quickstart.py`):

```python
from behave_rv.catalog.registry import StepRegistry
from behave_rv.compile.compiler import compile_feature
from behave_rv.engine.loop import Engine
from behave_rv.events.event import Event
from behave_rv.events.sources.inprocess import InProcessSource
from behave_rv.verdict.explain import explain_verdict

# -- 1. your app emits events at its state changes (additive: one call) ------
source = InProcessSource()

def set_status(order_id: str, status: str, at: float) -> None:
    # ... your real business logic here ...
    source.emit(Event("order.status", at, {"order_id": order_id},
                      {"status": status}, "my-app"))

# -- 2. one registered step: the vocabulary policies are written in ----------
registry = StepRegistry()

@registry.trigger('an order is "{status}"', step_id="order.status.is",
                  event_type="order.status", correlation_key="order_id")
def order_is(ctx, event, status):
    if event.type == "order.status" and event.payload.get("status") == status:
        ctx.bind(order_id=event.bindings["order_id"])
        return True
    return False

# -- 3. the policy, in plain Gherkin ------------------------------------------
policies = compile_feature("""
Feature: payment safety
  Scenario: an order may only be paid after it was authorized
    When an order is "paid"
    Then an order is "authorized" before
""", registry)

# -- 4. run the app, then the monitor -----------------------------------------
set_status("A-1", "authorized", at=1.0)
set_status("A-1", "paid", at=2.0)          # fine
set_status("B-7", "paid", at=3.0)          # never authorized!

for verdict in Engine(policies, grace=0).run(source, emit_pending=True):
    print(verdict.entity_key, verdict.verdict)
    if verdict.verdict == "violated":
        print(explain_verdict(verdict, policies[0].authored_scenario,
                              policies[0].failing_step_index))
```

Output: `A-1` satisfied, `B-7` violated — and the violation is your own
scenario replayed with the failing step marked `✗` and the real events that
decided it. Run it: `python examples/quickstart.py`.

## 2. The pieces, one by one

### Events — what the monitor sees

Everything is a normalized `Event(type, event_time, bindings, payload, source)`:

- `type` — a stable event-type identity (`"order.status"`), not a display name.
- `event_time` — seconds, **from the event itself**, never receipt time. For
  live monitoring use small, service-relative times (e.g. seconds since app
  start), not Unix epoch — see Gotchas below.
- `bindings` — the correlation key values (`{"order_id": "A-1"}`): how the
  engine separates one entity from another. One key per policy (a tuple for
  composite identity is fine).
- `payload` — the observable fields your steps read.

### Steps — the vocabulary you expose

A step is a **pure predicate**: it reads an event, returns a boolean, mutates
nothing. This is required, not enforced — impurity silently breaks
reproducibility. Register into your own `StepRegistry` (as above, best for
tests and tools) or use the module-level decorators
(`from behave_rv.steps import trigger, scope, obligation`) which register into
the process default registry — the style the CLI expects from a steps module.

Contract details that matter:

- the phrasing's `{placeholders}` are bound BY NAME to the function's
  parameters after `(ctx, event, ...)` — those parameter names are contract;
  renaming one breaks every dependent policy (and the catalog diff will tell
  you so).
- `ctx.bind(...)` is an optional readability declaration; dispatch actually
  reads `event.bindings` via the decorator's declared `correlation_key`.
- `step_id` is the stable identity that survives renames; keep it forever.

### Policies — plain Gherkin over your steps

One scenario = one policy = one entity type. The complete operator reference
with satisfying/violating traces for all nine forms is
[`docs/OPERATORS.md`](OPERATORS.md); the short version:

| You want to say | You write |
|---|---|
| X must have happened before Y | `When ... "Y"` / `Then ... "X" before` |
| respond within N seconds | `Then ... within "5" seconds` |
| this must never happen | `Then ... never happens` (plus `Given` scopes, incl. `until`) |
| eventually happens | `Then ... has happened` |
| always true / regime rules | `always holds`, `previously`, `since` |

Anything outside the fragment (cross-entity rules, aggregates) is refused at
compile time with a clear message — refusal is what makes accepted policies
trustworthy.

### The engine — options, all of them

```python
engine = Engine(
    policies,
    terminal_event_types={"order.done"},  # events ending an entity's life:
                                          # settles pending obligations, frees memory
    grace=5.0,                            # reorder window (event-time seconds);
                                          # out-of-order events within it are sorted
                                          # back; grace=0 = strict arrival order
    quiescence_ttl=3600.0,                # optional: reclaim entities silent this long
)
verdicts = engine.run(
    source,
    emit_pending=True,   # at end of a bounded run, report still-open
                         # obligations honestly as "pending"
    sink=my_callable,    # OR: deliver each verdict the moment it is decided
                         # (run then returns []); a raising sink is recorded
                         # and never kills the monitor
)
```

Verdicts are three-valued: `satisfied`, `violated`, `pending`. A `Verdict`
carries `policy_id`, `entity_key`, `verdict`, `trigger_event`,
`deciding_events` (the evidence), `witnessing_trace` (recent context), `at`.
If you run `once`/`historically`/`since` policies with no terminal event
configured, the engine warns at startup (`NoTerminalConfiguredWarning`) that
they may stay pending forever — that is a prompt, not an error.

## 3. Feeding events: the three sources

- **`InProcessSource`** — your code calls `source.emit(event)` (or the
  `emit_event(...)` convenience); the engine drains it in one bounded run.
  Best for tests and batch checks.
- **`QueueSource`** — the live mode. `push()` is thread-safe: your app's
  threads push while the engine consumes on its own thread; `within`
  deadlines fire on the wall clock even when the stream goes quiet;
  `close()` ends the run cleanly.

  ```python
  source = QueueSource()
  threading.Thread(target=lambda: engine.run(source, sink=on_verdict),
                   daemon=True).start()
  source.push(event)        # from anywhere in your app
  ```
- **`ReplaySource("trace.jsonl")`** — re-run policies over a recorded stream
  (write one with `record_events(path, events)`). Same pipeline as live, so
  a policy can be tested against last week's traffic before deploying it.

Any object with an `.events()` iterator works as a source; add `live = True`
and a `next_event(timeout)` only if its timestamps advance at wall rate.

## 4. Watching the monitor while your app runs

### The built-in web dashboard

```python
from behave_rv.dashboard import Dashboard

dashboard = Dashboard(policies)               # stdlib only, no extra deps
print("monitor at", dashboard.start(port=7007))

engine.run(source, sink=dashboard.sink)       # verdicts flow to the page
```

Open `http://127.0.0.1:7007` while the app runs: every policy with live
per-entity verdict badges (green satisfied / red violated / "no verdicts yet"
= pending), a violations panel where each entry is the authored scenario
rendered with the failing step marked and the deciding events listed, and
running counts. Optionally wrap your emits with `dashboard.tap(event)` to see
the raw event feed alongside. Have your own sink too? Chain it:
`Dashboard(policies, forward=my_sink)`. The page polls; nothing blocks your
app (the sink only records under a lock, the HTTP server runs on a daemon
thread).

### Programmatic status

After (or during, from your sink) a run, the engine exposes counters:
`verdicts_delivered`, `live_instances`, `late_events` / `dropped_late`,
`invalid_events`, `observed_types` / `observed_values` (the liveness harvest),
`retired_keys`, and two error logs that never kill the run:
`sink_errors` / `first_sink_error` and `predicate_errors` /
`first_predicate_error` / `predicate_error_sources` (which policy, which step).

### Sinks you can use out of the box

`JsonSink(stream)` (one JSON line per verdict), `JsonFileSink(path)`
(appended + flushed, tail-able), `PrintSink(policies)` (violations printed as
full explanations, everything else one compact line), or any callable.

## 5. The command line

```bash
# run a policy over a recorded trace (verdict log + explanations + liveness)
python -m behave_rv --steps steps.py --policy policy.feature --trace trace.jsonl

# the stability workflow: keep policies alive as code changes (STABILITY.md)
python -m behave_rv catalog save --steps steps.py --catalog catalog.json
python -m behave_rv catalog diff --steps steps.py --catalog catalog.json \
    --policies policies/ --trace last_week.jsonl     # exit 1 on breaks: CI gate
```

## 6. Keeping policies alive when the code changes

The committed `catalog.json` is the contract between your code and your
policies: renames absorb, contract changes break loudly against exactly the
affected policies, app-side renames are caught by liveness against a
representative trace, and each signature's `unresolved_calls` shows where the
fingerprint's protection ends. Full mechanism, worked examples, and the
measured 22-case detection table: [`STABILITY.md`](../STABILITY.md).

## 7. Gotchas, honestly

- **Event time is the clock.** Ordering, deadlines, and verdict timestamps
  use `event_time`, never arrival time.
- **Live mode wants small timestamps.** A known open issue makes wall-clock
  deadline firing unreliable at Unix-epoch magnitudes (~1.8e9: the live
  loop's absolute epsilon falls below float precision). Until fixed, emit
  service-relative times in live mode — `time.time() - START` — as all three
  demos do. Replay/batch runs are unaffected.
- **Purity is on you.** Steps must be deterministic and side-effect free;
  the framework expects and does not enforce it.
- **`pending` is honest, not stuck.** Unbounded obligations settle at the
  entity's terminal event or report as pending at end of a bounded run.
- **One correlation key per policy.** Cross-entity rules are refused, by
  design; composite keys (tuples) are supported.

## 8. Where to go deeper

`docs/OPERATORS.md` (every operator, with traces) · `SEMANTICS.md` (formal
trace semantics) · `STABILITY.md` (the code-change defense mechanism) ·
`MUTATION.md` (how the suite itself is validated) · `demo/README.md` (three
complete live demo apps with interactive boards).
