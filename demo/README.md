# behave_rv demos

Three small web apps, each a mock service monitored live by real behave_rv
policies compiled from `.feature` files. Every demo wires the same pipeline:

```
mock service (own thread) --push--> QueueSource --> Engine (single-threaded)
                                                       |
                 browser <-- SSE <-- Broadcast <-- sink (enqueue only)
```

The mock produces on its own thread and pushes to the `QueueSource` (push is
thread-safe); the engine consumes single-threaded; the sink runs on the
engine's consumer thread and only enqueues onto the SSE queue.

## Run

```
pip install -r demo/requirements.txt      # Flask, demo-only; the core package stays dependency-light
python -m demo.order_service.app          # http://127.0.0.1:5001
python -m demo.session_service.app        # http://127.0.0.1:5002
python -m demo.todo_app.app               # http://127.0.0.1:5003
```

In each UI: green buttons play correct flows, red buttons plant one specific
bug each. Watch the chain: action -> event in the live log -> verdict badge
flips -> the authored scenario renders as a counterexample in the explanation
panel.

Every demo also has an interactive board at `/board` -- a real-looking app
where YOU drive the events, with a dark monitor console on the left (live
event stream plus every verdict, violations rendered as the authored scenario
with the failing step marked) and all policies as cards on the right, their
per-entity badges flipping green/red/grey live:

- `http://127.0.0.1:5001/board` -- **Shoply**: create and drive orders by
  hand; close an undelivered order and watch its "eventually" obligations
  settle red at the terminal.
- `http://127.0.0.1:5002/board` -- **Authly**: open sessions; the third
  failed-login click locks the account through the service's real lockout
  logic, and everything after that is between you and the policies.
- `http://127.0.0.1:5003/board` -- **Taskly**: a todo app; complete an
  unstarted task, edit a deleted one, or start a task and walk away until
  the wall-clock windows fire.

The boards never enforce their own rules -- any action works at any time.
Judging legality is the monitor's job, which is the whole demonstration.

## Verify without the UI

```
pytest demo/                              # replays every mock flow through the real engine
```

Each demo's `test_policies.py` injects a deterministic clock into the mock,
replays the exact traces the buttons would produce, and asserts the precise
verdict set (including deciding events) for every policy.

## Operator coverage

| Operator | Order Service | Session Service | Todo App |
|----------|:-:|:-:|:-:|
| `never` (self-contained) | 08 double-charge, 09 chargeback (quiet) | 09 deleted (quiet) | 11 corrupted (quiet) |
| `before` | 01 paid/auth, 02 ship/pay, 03 refund/cancel | 01 action/login, 02 logout/login | 01 complete/start, 02 edit/create |
| `within` (wall timer) | 06 refund "5"s, 07 payment "10"s | 07 review "8"s | 06 due "5"s, 07 checkpoint "3"s |
| `once` (has happened) | 04 invoiced, 05 delivered | 08 logout | 04 completed, 05 archived |
| `historically` (always holds) | - | - | 12 sync ok |
| `previously` | - | 03 lock/fail | 03 reopen/complete |
| `since` | 11 flagged/reviewed | 10 flagged/reviewed (quiet) | - |
| scoped `never` (Given, latching) | 10 cancelled/ship | 04 locked/act, 05 logout/act | 08 archived/edit, 09 deleted/edit |
| `until` (Given ... until) | - | 06 locked-until-unlocked | 10 blocked-until-unblocked |

Every operator appears in at least one policy that the tests fire and verify;
`within` deadlines in the live apps fire on the wall clock while the entity
sits idle, which is the point of the "never refund / never review / miss the
window" buttons.

## Specification stability under code change

The order demo also demonstrates the catalog mechanism (the "policies survive
the agent's refactoring" claim): a committed `catalog.json` contract, a pure
rename absorbed silently with identical verdicts (the old wording retained
as an alias), a contract change surfacing
as Breaks scoped to the using policies, and the two changes signatures cannot
see (a renamed status value, a dropped emission) caught by value-level
liveness instead. See
[order_service/README.md](order_service/README.md#evolution-the-catalog-surviving-the-agents-refactoring)
and run `python -m demo.order_service.evolution`.

Per-demo policy tables: [order_service](order_service/README.md),
[session_service](session_service/README.md), [todo_app](todo_app/README.md).

## The slice explorer

`python -m demo.slice_explorer` serves an interactive view of the app-surface
analysis (see `docs/STABILITY.md`, Path D): pick any of the demo applications,
click a source line, and see the backward dependency slice the real analyser
computes - the emissions that line can influence, the slice members,
constants, declared resolver holes, and the policies at risk.
