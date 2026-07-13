"""The five-minute quickstart from docs/GUIDE.md -- complete and runnable:

    python examples/quickstart.py
"""

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
