"""Check the policies against a recorded (or freshly simulated) trace, batch
style -- the shape you would run in CI or against last week's production:

    python examples/ticketing/replay_check.py             # exit 1 on violations

Demonstrates: deterministic replay (fake clock -> identical run every time),
``record_events``/``ReplaySource`` for trace files, ``emit_pending`` for the
honest end-of-run report, and reading verdicts programmatically.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # examples are standalone

from app_service import TERMINAL_TYPE, TicketService                 # noqa: E402
from monitoring.steps import build_registry, load_policies           # noqa: E402

from behave_rv.engine.loop import Engine                             # noqa: E402
from behave_rv.events.sources.replay import ReplaySource, record_events  # noqa: E402
from behave_rv.verdict.explain import explain_verdict                # noqa: E402


class FakeClock:
    """Deterministic time: sleep() just advances it. The same traffic always
    produces the same trace, so the check is reproducible byte for byte."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def tick(self, dt: float):
        self.now += dt


def simulate_traffic(path: Path) -> None:
    clock = FakeClock()
    events = []
    service = TicketService(events.append, clock=clock)

    # NOTE the clock.tick between every ordered action: with equal event
    # times the engine orders canonically (not by arrival), so actions whose
    # order matters must carry distinct timestamps -- see the guide's Gotchas.
    service.open_ticket("T-1", "printer on fire")
    clock.tick(5.0)
    service.assign("T-1", "dana")
    clock.tick(5.0)
    service.resolve("T-1")
    clock.tick(0.5)
    service.close("T-1")

    clock.tick(0.5)
    service.open_ticket("T-2", "cannot log in")     # never assigned...
    clock.tick(1.0)
    service.resolve("T-2")                          # ...but resolved: violation

    clock.tick(0.5)
    service.open_ticket("T-3", "slow dashboard")    # assigned too late:
    clock.tick(45.0)                                # the 30s SLA timer fires
    service.assign("T-3", "omer")

    record_events(path, events)


def main() -> int:
    trace = Path(__file__).parent / "trace.jsonl"
    simulate_traffic(trace)

    policies = load_policies(build_registry())
    engine = Engine(policies, terminal_event_types={TERMINAL_TYPE})
    verdicts = engine.run(ReplaySource(trace), emit_pending=True)

    by_id = {p.policy_id: p for p in policies}
    violations = 0
    for verdict in verdicts:
        print(f"{verdict.verdict:9}  {verdict.entity_key['ticket_id']:5} "
              f"{verdict.policy_id}")
        if verdict.verdict == "violated":
            violations += 1
            print(explain_verdict(verdict, by_id[verdict.policy_id].authored_scenario,
                                  by_id[verdict.policy_id].failing_step_index))
            print()

    print(f"\n{len(verdicts)} verdicts, {violations} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
