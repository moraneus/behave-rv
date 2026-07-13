# Using behave_rv in your code — the complete guide

behave_rv watches your running application and tells you, deterministically
and with evidence, whether the rules you wrote in plain Gherkin hold — per
entity, live or over a recorded trace. This guide assumes no familiarity with
the library: it covers the files a monitored project has and how each is
written, every concept and option, how to watch the monitor while your app
runs, and three complete working examples that are committed to this repo and
verified to run.

Setup: `pip install -e .` from a clone (dependencies: `behave`, `parse`).
Nothing else is needed — even the live web dashboard is standard library only.

---

## 1. The files of a monitored project

A project using behave_rv conventionally has this shape (this exact layout is
committed and runnable at [`examples/ticketing/`](../examples/ticketing/)):

```
your_app/
  app_service.py            your business logic, emitting events (a .py file)
  monitoring/
    steps.py                the monitorable surface: registered step predicates
    policies/
      01_resolve_after_assign.feature      one policy per .feature file
      02_assignment_sla.feature
      03_escalation_blocks_closing.feature
      04_every_ticket_resolved.feature
      05_reply_sla.feature
      06_oncall_gets_urgent_only.feature
    catalog.json            the committed step contract (CLI-generated; see §3)
  traces/
    last_week.jsonl         recorded event streams (optional, for replay)
```

**Important, stated up front: there is no magic path discovery.** Unlike
classic `behave` (which auto-discovers a `features/` directory), behave_rv
reads exactly the files you point it at — your code calls
`compile_feature(path.read_text(), registry)` per file, and the CLI takes
`--steps`, `--policies`, `--catalog`, `--trace` arguments. The layout above
is a convention that makes those one-liners, not a requirement the engine
enforces. Nothing breaks if you place files elsewhere; you just pass the
paths you chose.

### Every file type, its rules, and who reads it

| File | Extension / format | Where (convention) | Written by | Read by | Rules |
|---|---|---|---|---|---|
| Policy | `.feature`, Gherkin | `monitoring/policies/`, ONE file per policy, numbered (`01_...`) | a human | `compile_feature(...)` in your code; CLI `--policies` | exactly **one `Feature:` block per file** (the parser refuses multiple); one `Scenario:` = one policy; **scenario names are the policy ids** — unique across all files, and they appear verbatim in verdicts, logs, and dashboards, so write them as readable sentences |
| Steps module | `.py` | `monitoring/steps.py`, next to `policies/` | the developer (or coding agent) | your code (`import`); CLI `--steps` | expose a side-effect-free `build_registry()` factory (the CLI auto-detects it), or register at import via the module decorators; details in §3 |
| Step catalog | `catalog.json`, versioned JSON | next to `steps.py`, **committed to git** | `python -m behave_rv catalog save` | `catalog diff` (the stability check) | never hand-edited; regenerate + commit when a contract change is intended; see [`STABILITY.md`](../STABILITY.md) |
| Trace | `.jsonl`, one JSON event per line | `traces/`, or wherever you record | `record_events(path, events)` or your own pipeline | `ReplaySource(path)`; CLI `--trace` | the exact `Event` fields (`type`, `event_time`, `bindings`, `payload`, `source`); event times in seconds |
| Your app | `.py` (any structure) | anywhere | you | — | the ONLY integration is calling an injected `emit(event)` at observable state changes; the app never imports the engine |

Naming conventions that pay off later:

- **`step_id`**: `<domain>.<event>.<what>` (e.g. `ticket.status.is`) — this is
  the stable identity policies bind to across renames; choose it once, never
  reuse it.
- **event `type`**: `<domain>.<noun>` (e.g. `ticket.status`), a stable
  identity, not a display string.
- **feature files**: numbered prefixes (`01_`, `02_`) keep listings and diffs
  in a stable, readable order.

---

## 2. The five-minute quickstart (single file)

Committed as [`examples/quickstart.py`](../examples/quickstart.py); run it
with `python examples/quickstart.py`.

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

Real output:

```
{'order_id': 'A-1'} satisfied
{'order_id': 'B-7'} violated
POLICY 'an order may only be paid after it was authorized'  ENTITY order_id=B-7  VERDICT violated @ t=3.0
Scenario: an order may only be paid after it was authorized
    When an order is "paid"
✗ Then an order is "authorized" before   # violated
Deciding events:
  t=3.0  order.status  {'status': 'paid'}
```

The violation is your own scenario replayed with the failing step marked and
the real events that decided it — that is the library's reporting model
everywhere (logs, CLI, dashboard).

---

## 3. The complete example, file by file: a ticketing app

Everything in this section is committed under
[`examples/ticketing/`](../examples/ticketing/) and runs as shown.

### `app_service.py` — your business logic, with taps

```python
EVENT_TYPE = "ticket.status"      # one stable type for the ticket lifecycle
TERMINAL_TYPE = "ticket.closed"   # ends a ticket's life: settles its policies

class TicketService:
    def __init__(self, emit, clock=time.time):
        self._emit = emit          # injected: tests pass a list.append,
        self._clock = clock        # live passes the real queue and clock

    def _status(self, ticket_id, status, **payload):
        self._emit(Event(EVENT_TYPE, self._clock(), {"ticket_id": ticket_id},
                         {"status": status, **payload}, "ticketing"))

    def open_ticket(self, tid, title): self._status(tid, "opened", title=title)
    def assign(self, tid, agent):      self._status(tid, "assigned", agent=agent)
    def escalate(self, tid):           self._status(tid, "escalated")
    def resolve(self, tid):            self._status(tid, "resolved")
    def close(self, tid):
        self._status(tid, "closed")                       # the observable change
        self._emit(Event(TERMINAL_TYPE, ...))             # then the terminal
```

What to copy from it: `emit` and `clock` are **injected** (same service runs
live and in deterministic tests); one `_status` tap per state change and
nothing else; `close()` emits the observable `"closed"` status *and then* the
terminal event — policies talk about the status, the engine uses the terminal
to settle pending obligations and free the ticket's state.

### `monitoring/steps.py` — the vocabulary (five steps)

```python
POLICY_DIR = Path(__file__).parent / "policies"

def build_registry() -> StepRegistry:
    registry = StepRegistry()

    # 1. the lifecycle step: matches any status by value
    @registry.trigger('a ticket is "{status}"', step_id="ticket.status.is",
                      event_type="ticket.status", correlation_key="ticket_id")
    def ticket_is(ctx, event, status):
        if event.type == "ticket.status" and event.payload.get("status") == status:
            ctx.bind(ticket_id=event.bindings["ticket_id"])
            return True
        return False

    # 2. a SECOND step over the SAME event type, reading a different field
    @registry.trigger('a ticket is assigned to "{agent}"',
                      step_id="ticket.assigned.to",
                      event_type="ticket.status", correlation_key="ticket_id")
    def ticket_assigned_to(ctx, event, agent):
        if event.type == "ticket.status" \
                and event.payload.get("status") == "assigned" \
                and event.payload.get("agent") == agent:
            ctx.bind(ticket_id=event.bindings["ticket_id"])
            return True
        return False

    # 3. a step over its own event type
    @registry.trigger('a ticket priority is "{level}"',
                      step_id="ticket.priority.is",
                      event_type="ticket.priority", correlation_key="ticket_id")
    def ticket_priority_is(ctx, event, level): ...

    # 4 + 5. steps with NO placeholder: the phrasing IS the whole condition
    @registry.trigger('a customer reply arrives', step_id="ticket.reply.inbound",
                      event_type="ticket.reply", correlation_key="ticket_id")
    def customer_reply_arrives(ctx, event): ...

    @registry.trigger('an agent reply is sent', step_id="ticket.reply.outbound",
                      event_type="ticket.reply", correlation_key="ticket_id")
    def agent_reply_sent(ctx, event): ...

    return registry

def load_policies(registry):
    return [p for path in sorted(POLICY_DIR.glob("*.feature"))
            for p in compile_feature(path.read_text(), registry)]
```

(Step bodies 3–5 abbreviated here; the committed file has them in full.)
Notice step 2: several steps may observe the SAME event type with different
conditions — steps are predicates, not one-per-event. Steps 4 and 5 have no
placeholder at all: the phrasing is the entire condition.

The rules a step must obey (each is load-bearing):

- **Pure predicate**: read the event, return `True`/`False`, change nothing.
  Required, not enforced — impurity silently breaks reproducibility.
- The phrasing's `{status}` placeholder binds **by name** to the third
  parameter, so that parameter must be named `status`. It is contract:
  renaming it disconnects every policy (the catalog diff will break loudly
  if you try). The first two parameters `(ctx, event)` are positional — name
  them anything.
- `ctx.bind(...)` is a readability declaration; entity identity actually
  comes from `event.bindings` via the decorator's declared `correlation_key`.
- `build_registry()` as a side-effect-free factory is the recommended style:
  tests get fresh isolated registries, and the CLI detects and calls it
  automatically. (The alternative — module-level
  `from behave_rv.steps import trigger` decorators that register at import —
  also works and is what `examples/order_steps.py` shows.)

### `monitoring/policies/*.feature` — one policy per file

```gherkin
# 01_resolve_after_assign.feature
Feature: assignment discipline
  Scenario: a ticket may only be resolved after it was assigned
    When a ticket is "resolved"
    Then a ticket is "assigned" before
```

```gherkin
# 02_assignment_sla.feature — a wall-clock deadline
Feature: assignment SLA
  Scenario: an opened ticket is assigned within the window
    When a ticket is "opened"
    Then a ticket is "assigned" within "30" seconds
```

```gherkin
# 03_escalation_blocks_closing.feature — an interval scope
Feature: escalation handling
  Scenario: an escalated ticket must not be closed until resolved
    Given a ticket is "escalated" until a ticket is "resolved"
    Then a ticket is "closed" never happens
```

```gherkin
# 04_every_ticket_resolved.feature — settles at the terminal event
Feature: resolution completeness
  Scenario: every ticket is eventually resolved
    Then a ticket is "resolved" has happened
```

```gherkin
# 05_reply_sla.feature — a deadline between the two no-placeholder steps
Feature: conversation SLA
  Scenario: a customer reply is answered within the window
    When a customer reply arrives
    Then an agent reply is sent within "60" seconds
```

```gherkin
# 06_oncall_gets_urgent_only.feature — mixes two steps and two event types
Feature: on-call discipline
  Scenario: the on-call agent only receives urgent tickets
    When a ticket is assigned to "oncall"
    Then a ticket priority is "urgent" before
```

The complete operator reference — all nine forms with satisfying and
violating traces — is [`docs/OPERATORS.md`](OPERATORS.md). Anything outside
the fragment (cross-entity rules, aggregates) is **refused at compile time**
with a clear message; refusal is what makes accepted policies trustworthy.

### `live_monitor.py` — run it live, with the dashboard

Run: `python examples/ticketing/live_monitor.py` and open the printed URL.

```python
policies = load_policies(build_registry())

start = time.time()
source = QueueSource()                       # live: push() is thread-safe
dashboard = Dashboard(policies)
service = TicketService(lambda e: source.push(dashboard.tap(e)),
                        clock=lambda: time.time() - start)   # relative times!

print("monitor:", dashboard.start(port=7007))
engine = Engine(policies, terminal_event_types={TERMINAL_TYPE}, grace=0.5)
threading.Thread(target=lambda: engine.run(source, sink=dashboard.sink),
                 daemon=True).start()

service.open_ticket("T-1", "printer on fire")   # from any app thread
```

The wiring, in words: your app threads push into the `QueueSource`; the
engine consumes on its own single thread; every decided verdict goes to the
dashboard's sink (which only records under a lock); the dashboard's HTTP
server serves snapshots from a daemon thread. Nothing blocks the app. The
seeded traffic in the example produces, live on the page: a healthy ticket
with a full answered conversation (all green), a resolved-without-assignment
violation, an escalated-then-closed violation, and the on-call agent handed
a never-urgent ticket — each rendered as the authored scenario with the
failing step marked.

### `replay_check.py` — the CI shape

Run: `python examples/ticketing/replay_check.py` (exits 1 on violations).
It records a deterministic trace (`FakeClock` — same input, same trace,
byte for byte), replays it through the engine with `emit_pending=True`, and
prints every verdict plus full explanations for violations. Real output ends:

```
41 verdicts, 5 violation(s)
```

Six tickets: the two healthy ones (T-1 with a full answered conversation,
T-4 the urgent/on-call/escalation path) end fully satisfied, and exactly the
five seeded faults are caught — resolve-before-assign, two assignment-SLA
timer firings, the on-call agent given a never-urgent ticket, and a customer
reply never answered (the 60s reply SLA) — with still-open obligations
reported honestly as pending. The suite pins this output
(`tests/test_examples.py`), so the guide cannot drift from reality.

### `monitoring/catalog.json` — what it is, who creates it, when you need it

You will not find this file until you create it, because **it is generated,
not written**: it is NOT part of your code and the monitor does NOT need it
to run. Plainly:

- **What it is**: a JSON snapshot of every registered step's *contract* — the
  event type it observes, its correlation key, the fields it reads, and a
  structural fingerprint of its matching condition (helpers included). Open
  the committed example
  ([`examples/ticketing/monitoring/catalog.json`](../examples/ticketing/monitoring/catalog.json))
  and read it: five entries, one per step.
- **Who creates it**: you (or CI), by running
  `python -m behave_rv catalog save --steps monitoring/steps.py --catalog monitoring/catalog.json`
  once, and committing the result. Never hand-edit it.
- **Its role**: it is the frozen reference that `catalog diff` compares the
  CURRENT code against after every change — so a refactor that silently
  changes what a step matches (and would leave your policies dormant) is
  reported as a break naming the affected policies, while harmless renames
  pass silently. It exists purely for that safety check; skip it and
  everything still runs, you just lose the drift protection.
- **When it changes**: only when you intend a contract change — regenerate
  with the same `save` command and commit the diff alongside the code
  change, like any interface change. Full mechanism:
  [`STABILITY.md`](../STABILITY.md).

### The same project through the CLI

```bash
# run one policy file over a recorded trace
python -m behave_rv --steps examples/ticketing/monitoring/steps.py \
    --policy examples/ticketing/monitoring/policies/01_resolve_after_assign.feature \
    --trace examples/ticketing/trace.jsonl

# the stability check against the COMMITTED catalog (exit 1 on breaks = CI gate)
python -m behave_rv catalog diff --steps examples/ticketing/monitoring/steps.py \
    --catalog examples/ticketing/monitoring/catalog.json \
    --policies examples/ticketing/monitoring/policies
```

---

## 4. The concepts, precisely

### Events

`Event(type, event_time, bindings, payload, source)`:

- `type` — stable event-type identity (`"ticket.status"`).
- `event_time` — seconds, **from the event, never receipt time**. All
  ordering, deadlines, and verdict timestamps use it.
- `bindings` — the correlation key values (`{"ticket_id": "T-1"}`); how the
  engine separates entities. One key per policy; a tuple for composite
  identity is fine; cross-entity policies are refused by design.
- `payload` — the observable fields steps read.
- `source` — provenance label, free-form.

### The engine and every option

```python
engine = Engine(
    policies,
    terminal_event_types={"ticket.closed"},   # entity end-of-life: settles
                                              # pending obligations, frees state
    grace=5.0,             # reorder window in event-time seconds: late events
                           # within it are sorted back into place; grace=0 means
                           # strict arrival order (fast path, no tolerance)
    quiescence_ttl=3600.0, # optional: reclaim entities silent this long
)
verdicts = engine.run(
    source,
    emit_pending=True,     # bounded runs: report still-open obligations as
                           # honest "pending" verdicts at the end
    sink=callable_or_None, # live delivery: each verdict the moment it is
                           # decided (run() then returns []); a raising sink is
                           # recorded and never kills the monitor
)
```

Verdicts are three-valued — `satisfied`, `violated`, `pending` — and carry
`policy_id`, `entity_key`, `verdict`, `trigger_event`, `deciding_events`
(the evidence), `witnessing_trace` (recent context), and `at` (event time of
the decision). If you run `has happened` / `always holds` / `since` policies
with no terminal configured, the engine warns once at startup
(`NoTerminalConfiguredWarning`): they may stay pending forever — a prompt to
configure a terminal, not an error.

### The three sources

| Source | Use for | Notes |
|---|---|---|
| `InProcessSource` | tests, batch checks | `emit(event)`; the run drains it and ends |
| `QueueSource` | **live monitoring** | `push()` thread-safe; `within` deadlines fire on the wall clock during quiet periods; `close()` ends the run |
| `ReplaySource(path)` | recorded `.jsonl` traces | same pipeline as live; write traces with `record_events(path, events)` |

Any object with an `.events()` iterator is a source; add `live = True` plus
`next_event(timeout)` only if its timestamps advance at wall rate.

---

## 5. Watching the monitor while your app runs

### The built-in web dashboard (stdlib, three lines)

```python
from behave_rv.dashboard import Dashboard

dashboard = Dashboard(policies)              # optionally: forward=my_sink
print("monitor at", dashboard.start(port=7007))
engine.run(source, sink=dashboard.sink)
```

Open the URL while the app runs: every policy with live per-entity verdict
badges (green satisfied / red violated / "no verdicts yet" = pending),
running counts, and a violations panel where each entry is the authored
scenario rendered with the failing step marked and the deciding events
listed. Wrap your emits with `dashboard.tap(event)` to also see the raw
event feed. The page polls every 1.5s; the sink only records under a lock;
nothing blocks your app.

### Programmatic status

On the engine, during (from your sink) or after a run: `verdicts_delivered`,
`live_instances`, `late_events` / `dropped_late`, `invalid_events`,
`observed_types` / `observed_values` (the liveness harvest), `retired_keys`,
and two never-fatal error logs — `sink_errors` / `first_sink_error` and
`predicate_errors` / `first_predicate_error` / `predicate_error_sources`
(which policy, which step).

### Stock sinks

`JsonSink(stream)` — one JSON line per verdict; `JsonFileSink(path)` —
appended and flushed, tail-able; `PrintSink(policies)` — violations printed
as full explanations, everything else compact; or any callable.

---

## 6. Keeping policies alive when the code changes

The committed `catalog.json` is the contract between your code and your
policies: renames absorb silently, contract changes break loudly against
exactly the affected policies (including helper-function changes, via the
call-graph fingerprint), app-side renames are caught by liveness against a
representative trace, and each signature's `unresolved_calls` shows where
the protection ends. Run `catalog diff` in CI (exit 1 gates the merge).
Full mechanism with worked examples and the measured detection table:
[`STABILITY.md`](../STABILITY.md).

---

## 7. Gotchas, honestly

- **Event time is the clock.** Ordering, deadlines, and verdicts use
  `event_time`, never arrival time.
- **Ordered actions need distinct timestamps.** With `grace > 0`, events with
  EQUAL times are ordered canonically (by content), not by arrival — so two
  actions whose order matters must not share a timestamp. Tick your clock
  between them (the ticketing example shows this; its first draft had the
  bug and produced spurious verdicts).
- **Live mode wants small timestamps.** A known open issue makes wall-clock
  deadline firing unreliable at Unix-epoch magnitudes; in live mode emit
  service-relative times (`time.time() - START`), as the examples and demos
  do. Replay and batch runs are unaffected.
- **Purity is on you.** Steps must be deterministic and side-effect free;
  the framework expects but cannot enforce it.
- **`pending` is honest, not stuck.** Unbounded obligations settle at the
  entity's terminal event, or report as `pending` when a bounded run ends.
- **Scenario names are ids.** Duplicate names are refused at compile time;
  keep them unique and readable — they are what you'll see in every verdict.
- **One `Feature:` per `.feature` file.** The parser refuses multiple.

---

## 8. Where to go deeper

[`docs/OPERATORS.md`](OPERATORS.md) — every operator, with traces ·
[`SEMANTICS.md`](../SEMANTICS.md) — formal trace semantics ·
[`STABILITY.md`](../STABILITY.md) — the code-change defenses ·
[`MUTATION.md`](../MUTATION.md) — how the suite itself is validated ·
[`demo/README.md`](../demo/README.md) — three complete demo apps with
interactive boards and a stability panel.
