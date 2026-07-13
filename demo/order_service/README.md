# Order Service demo

A mock e-commerce order pipeline (`service.py`) monitored live by 11 behave_rv
policies. Orders move through `created -> authorized -> paid -> invoiced ->
shipped -> delivered`, with cancellation/refund and fraud-review side paths.
`order.done` is the terminal event that settles an order's pending policies.

Run it:

```
pip install -r demo/requirements.txt
python -m demo.order_service.app
```

Two pages, one engine:

- `http://127.0.0.1:5001` -- the scripted console: green buttons play correct
  flows, each red button plants exactly one bug and lights up exactly the
  policy written to catch it.
- `http://127.0.0.1:5001/board` -- "Shoply", the interactive board: create
  orders yourself and drive them by hand (authorize / pay / ship / deliver on
  the card, the rest -- invoice, cancel, refund, flag, review, charge again,
  close -- in the overflow menu). The shop never blocks an action; the
  policies judge each click. Three panes: the dark monitor console (event
  stream + verdicts, violations rendered as your scenario with the failing
  step marked), your orders, and all 11 policies as cards with per-order
  verdict badges flipping live. Closing an undelivered order settles its
  "eventually" obligations on the spot -- red, with the terminal as witness.

- `http://127.0.0.1:5001/stability` -- the stability panel: four real code
  changes (a pure rename, a payload-field rename, an app-side status-value
  rename, a helper-function change), each applied in a sandboxed copy and run
  through the REAL defense stack -- catalog diff, value-level liveness against
  the app's own stream, and a verdict replay of the seeded fault before and
  after. Absorption is shown as a positive, the break with its contract diff
  and affected policies, the value rename with the silent failure and the
  liveness alarm side by side, and the helper change as the documented blind
  spot. Backed by `stability.py` (tested in `test_stability_panel.py`);
  nothing is mocked. See `docs/STABILITY.md`.

## Policies

| # | Policy (scenario name) | Operator | Category | Fired by |
|---|------------------------|----------|----------|----------|
| 01 | an order may only be paid after it was authorized | `before` | triggerable | Trigger: pay without auth |
| 02 | a shipment may only follow payment | `before` (2nd role) | triggerable | Trigger: ship without pay |
| 03 | a refund requires a prior cancellation | `before` | triggerable | Trigger: refund without cancel |
| 04 | every order is eventually invoiced | `once` | long-pending | (settles at terminal) |
| 05 | every order is eventually delivered | `once` (2nd role) | long-pending | (settles at terminal) |
| 06 | a cancelled order is refunded within the window | `within "5"` | triggerable, wall timer | Trigger: cancel, never refund |
| 07 | an authorized order is paid within the window | `within "10"` (2nd role) | long-pending window | (satisfied in normal flows) |
| 08 | an order is never double charged | `never` | triggerable | Trigger: double charge |
| 09 | an order is never charged back | `never` | quiet (no-cry-wolf) | never fires |
| 10 | a cancelled order is never shipped | scoped `never` (Given) | triggerable | Trigger: ship after cancel |
| 11 | a flagged order is only reviewed afterwards | `since` | triggerable + quiet-ish | Trigger: pay after fraud flag |

`test_policies.py` replays every mock flow through the real engine with an
injected deterministic clock and asserts the exact verdict set, including the
deciding events behind each violation. Run with `pytest demo/order_service`.

## Evolution: the catalog surviving the agent's refactoring

`catalog.json` is the committed behavioral contract between this service's
code and the 11 policies. `evolution.py` plays five "the agent rewrote the
service" scenarios against it:

```
python -m demo.order_service.evolution        # narrated
pytest demo/order_service/test_evolution.py   # asserted (7 tests)
```

| Act | The agent's change | What catches it |
|---|---|---|
| 1 | function + wording renamed (old wording retained as an alias), contract identical | nothing, by design: diff says `renamed`, zero notifications, verdicts identical |
| 2 | event type + payload field move | signature diff -> 11 Breaks, scoped to exactly the policies using the step, with the contract diff in the message |
| 3 | status value `"paid"` -> `"charged"` | NOT the diff (contract unchanged, honestly silent); value-level liveness against an observed stream flags the three paid-dependent policies |
| 4 | the invoice emission silently dropped | NOT the diff; the same liveness check flags `every order is eventually invoiced` |
| 5 | new uncovered tap + an agent test asserting something weaker | the Suggestion and Weakening channels, never blurred with Breaks |

`test_act0_committed_catalog_matches_the_code` is the CI gate: change the
step's contract in `steps.py` without regenerating the catalog (a reviewable
diff plus notifications) and it fails. After an INTENDED contract change,
regenerate with
`python -c "from demo.order_service.evolution import write_catalog; write_catalog()"`
and commit the catalog diff alongside the code.
