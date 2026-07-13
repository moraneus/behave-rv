"""Run the ticketing app LIVE with the monitor and the web dashboard attached.

    python examples/ticketing/live_monitor.py            # runs 60s
    python examples/ticketing/live_monitor.py --seconds 5

This is the standard live wiring, worth reading line by line:

  app thread(s) --push--> QueueSource --> Engine (its own single thread)
                                             |
        browser <-- Dashboard (http) <-- sink (records under a lock)

The app pushes events from wherever it runs (push is thread-safe); the engine
consumes on one thread; the dashboard's sink only records; the dashboard's
HTTP server serves snapshots on a daemon thread. Nothing blocks the app.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # examples are standalone

from app_service import TERMINAL_TYPE, TicketService                 # noqa: E402
from monitoring.steps import build_registry, load_policies           # noqa: E402

from behave_rv.dashboard import Dashboard                            # noqa: E402
from behave_rv.engine.loop import Engine                             # noqa: E402
from behave_rv.events.sources.subscription import QueueSource        # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60.0,
                        help="how long to keep serving before shutting down")
    args = parser.parse_args()

    policies = load_policies(build_registry())

    # live-mode convention: service-relative event times (see guide Gotchas)
    start = time.time()
    source = QueueSource()
    dashboard = Dashboard(policies)
    service = TicketService(lambda e: source.push(dashboard.tap(e)),
                            clock=lambda: time.time() - start)

    print("monitor:", dashboard.start(port=7007))
    engine = Engine(policies, terminal_event_types={TERMINAL_TYPE}, grace=0.5)
    engine_thread = threading.Thread(
        target=lambda: engine.run(source, sink=dashboard.sink), daemon=True)
    engine_thread.start()

    def traffic():
        # a healthy ticket...
        service.open_ticket("T-1", "printer on fire")
        time.sleep(0.3)
        service.assign("T-1", "dana")
        time.sleep(0.3)
        service.resolve("T-1")
        service.close("T-1")
        # ...a seeded bug: resolved without ever being assigned
        service.open_ticket("T-2", "cannot log in")
        time.sleep(0.3)
        service.resolve("T-2")
        # ...and an escalated ticket closed before resolution (the until rule)
        service.open_ticket("T-3", "data loss")
        time.sleep(0.2)
        service.assign("T-3", "omer")
        service.escalate("T-3")
        time.sleep(0.2)
        service.close("T-3")

    threading.Thread(target=traffic, daemon=True).start()

    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    source.close()
    engine_thread.join(timeout=5)
    dashboard.stop()
    print(f"done: {engine.verdicts_delivered} verdicts delivered "
          f"({dashboard.state()['counts']['violations']} violations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
