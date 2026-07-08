# Session Service demo

A mock authentication / access-control service (`service.py`) monitored live
by 10 behave_rv policies. The lockout logic is real: three consecutive failed
logins emit `locked`. `session.end` is the terminal event emitted by a clean
logout flow.

Run it:

```
pip install -r demo/requirements.txt
python -m demo.session_service.app
```

Two pages, one engine:

- `http://127.0.0.1:5002` -- the scripted console. The marquee contrast here
  is the two scope forms: "Play: lock, unlock, act" fires the latching scoped
  rule (04) but leaves the interval (`until`) rule (06) clean, while
  "Trigger: re-lock then act" fires both.
- `http://127.0.0.1:5002/board` -- "Authly", the interactive board: open
  sessions yourself and drive them (login ok / fail login / do action on the
  card; logout, review, unlock, flag, force lock, end session in the overflow
  menu). The third "fail login" click locks the account through the service's
  REAL lockout logic -- watch it emit `locked` by itself. Everything else is
  deliberately unenforced: act while locked, act after logout, or force a
  lock with no failed attempt (the `previously` rule catches it). Three
  panes: monitor console, sessions, and all 10 policies as cards with
  per-user verdict badges flipping live.

## Policies

| # | Policy (scenario name) | Operator | Category | Fired by |
|---|------------------------|----------|----------|----------|
| 01 | an action requires a prior successful login | `before` | triggerable | Trigger: action without login |
| 02 | a logout follows a login | `before` (2nd role) | triggerable | Trigger: logout without login |
| 03 | a lockout follows a failed attempt | `previously` | triggerable | Trigger: lock with no failed attempt |
| 04 | a locked user must never act | scoped `never` (latching) | triggerable | Trigger: locked user acts / re-lock |
| 05 | a logged-out user must never act | scoped `never` (2nd scope) | triggerable | Trigger: act after logout |
| 06 | a user must not act while locked, until unlocked | `until` interval | triggerable | Trigger: re-lock then act |
| 07 | a locked account is reviewed within the window | `within "8"` | triggerable, wall timer | Trigger: lock, never review |
| 08 | every session eventually logs out | `once` | long-pending | (settles at terminal) |
| 09 | a user is never deleted mid session | `never` | quiet (no-cry-wolf) | never fires |
| 10 | a flagged user is only reviewed afterwards | `since` | quiet (no-cry-wolf) | never fires |

`test_policies.py` replays every mock flow through the real engine with an
injected deterministic clock and asserts the exact verdict set, including the
deciding events behind each violation. Run with `pytest demo/session_service`.
