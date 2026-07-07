# Todo App demo

A mock task manager (`service.py`) with TWO monitored entity types: tasks
(keyed `task_id`, event `task.status`) and a background sync channel (keyed
`session_id`, event `sync.status`). The sync channel is homogeneous, which is
what makes the `historically` policy well-defined. There is no terminal
event, so the "eventually" policies legitimately stay grey.

Run it:

```
pip install -r demo/requirements.txt
python -m demo.todo_app.app
```

Two pages, one engine:

- `http://127.0.0.1:5003` -- the scripted console: green/red buttons replay
  canned correct and buggy flows.
- `http://127.0.0.1:5003/board` -- the interactive board: a real-looking todo
  app where YOU create tasks and click start / checkpoint / complete / reopen /
  edit / block / unblock / archive / delete. Every click emits one event. The
  board deliberately never enforces the lifecycle -- any button works at any
  time -- because judging legality is the monitor's job, not the app's. The
  left-side console shows the live event stream and every verdict; violations
  arrive as the authored scenario rendered with the failing step marked, and
  the offending task's card is flagged red.

Things to try on the board: complete a task you never started; edit a task
after deleting or archiving it; or start a task and just walk away -- the
checkpoint window (3s) and the due window (5s) fire on the wall clock, in
order, with no further click. The scripted console's two-timer control
("Trigger: miss the checkpoint") demonstrates the same thing hands-free.

## Policies

| # | Policy (scenario name) | Operator | Category | Fired by |
|---|------------------------|----------|----------|----------|
| 01 | a task may only be completed after it was started | `before` | triggerable | Trigger: complete an unstarted task |
| 02 | an edit follows a create | `before` (2nd role) | triggerable | Trigger: edit an uncreated task |
| 03 | a reopen follows a completion | `previously` | triggerable | Trigger: reopen an uncompleted task |
| 04 | every task is eventually completed | `once` | long-pending | (satisfied when the flow completes) |
| 05 | every task is eventually archived | `once` (2nd role) | long-pending, quiet | never violates |
| 06 | a started task completes within the due window | `within "5"` | triggerable, wall timer | Trigger: miss the due window |
| 07 | a started task reaches a checkpoint promptly | `within "3"` (2nd role) | triggerable, wall timer | Trigger: miss the checkpoint |
| 08 | an archived task must never be edited | scoped `never` (Given) | triggerable | Trigger: edit an archived task |
| 09 | a deleted task must never be edited | scoped `never` (2nd scope) | triggerable | Trigger: edit a deleted task |
| 10 | a blocked task must not complete, until unblocked | `until` interval | triggerable | Trigger: blocked task completes |
| 11 | a task is never corrupted | `never` | quiet (no-cry-wolf) | never fires |
| 12 | sync always succeeds this session | `historically` | triggerable | Trigger: sync failure |

`test_policies.py` replays every mock flow through the real engine with an
injected deterministic clock and asserts the exact verdict set, including the
deciding events behind each violation. The board's manual path is verified the
same way: `run_manual` replays sequences of user clicks (one `service.act` per
click) and asserts that a clean hand-driven lifecycle stays green while
illegal clicks are caught per task. Run with `pytest demo/todo_app`.
