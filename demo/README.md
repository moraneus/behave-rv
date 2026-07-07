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

The todo demo additionally has an interactive board at
`http://127.0.0.1:5003/board`: a real-looking todo app where you drive the
events yourself (create, start, complete, edit, archive, delete, ...) and a
left-side monitor console shows each event you cause and every violation the
engine decides, live.

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
| `historically` (always holds) | — | — | 12 sync ok |
| `previously` | — | 03 lock/fail | 03 reopen/complete |
| `since` | 11 flagged/reviewed | 10 flagged/reviewed (quiet) | — |
| scoped `never` (Given, latching) | 10 cancelled/ship | 04 locked/act, 05 logout/act | 08 archived/edit, 09 deleted/edit |
| `until` (Given ... until) | — | 06 locked-until-unlocked | 10 blocked-until-unblocked |

Every operator appears in at least one policy that the tests fire and verify;
`within` deadlines in the live apps fire on the wall clock while the entity
sits idle, which is the point of the "never refund / never review / miss the
window" buttons.

Per-demo policy tables: [order_service](order_service/README.md),
[session_service](session_service/README.md), [todo_app](todo_app/README.md).
